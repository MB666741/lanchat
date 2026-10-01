"""UDP 广播自动发现。

工作原理
--------
每个实例:
  1. 监听 **同一个** UDP 端口 (默认 50505) —— 这样同一台机器上开多个实例也能互相看见;
     绑定 0.0.0.0 并开启 SO_REUSEADDR/SO_REUSEPORT (Windows 下广播包会投递给所有绑定者)。
  2. 每隔 3 秒向本网段的**定向广播地址**发送一个 beacon::

         {"t":"lanchat","v":1,"id":"<随机ID>","name":"昵称","port":<TCP端口>,"ts":...}

  3. 收到别人的 beacon 就登记为"在线", 超过 12 秒没收到就标记为"离线"。

因为广播只在本网段传播, 所以"看得见"就等于"同一局域网" (无需手动填 IP)。
不同网段/跨路由时可用 "手动添加" (host:port) 兜底。
"""

from __future__ import annotations

from .i18n import t
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .constants import (
    BEACON_INTERVAL,
    DEFAULT_DISCOVERY_PORT,
    PEER_TIMEOUT,
    PROTOCOL_VERSION,
)

BEACON_TYPE = "lanchat"
PROBE_KIND = "who"        # 单播探测: "你在吗?" —— 对方会单播回一条 beacon (带它的 TCP 端口)

# ---------------------------------------------------------------------------
# 本机网卡 / 广播地址
# ---------------------------------------------------------------------------
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_MASK_WORDS = ("mask", "netmask", "子网掩码")
_GATEWAY_WORDS = ("gateway", "默认网关", "default")

# 网卡枚举的缓存。Windows 上要跑一次 `ipconfig` 子进程, 实测:
#   * 源码运行: 0.04 秒;
#   * **打包成窗口程序后: 2.5 秒** (没有控制台, 起子进程明显更慢)。
# 这个调用以前发生在 `DiscoveryService.__init__` 里, 也就是 **Tk 界面线程**上 ——
# 用户看到的就是"填完昵称点『开始使用』, 窗口先未响应几秒才进聊天界面"。
# 所以: 结果缓存 + 第一次丢到后台线程去跑 (见 warm_interfaces_async)。
IFACE_CACHE_SECONDS = 30.0          # 缓存有效期
IFACE_REFRESH_SECONDS = 120.0       # 后台发现线程每隔多久重新枚举一次 (VPN 后连上也能覆盖到)
_IFACE_LOCK = threading.RLock()             # 保护缓存
_IFACE_COMPUTE_LOCK = threading.Lock()      # 保证同一时刻只有一次真正的枚举
_IFACE_CACHE: Dict[str, object] = {"at": 0.0, "pairs": []}


def _iface_cache(max_age: float) -> List[Tuple[str, Optional[str]]]:
    with _IFACE_LOCK:
        cached = list(_IFACE_CACHE["pairs"])          # type: ignore[arg-type]
        fresh = bool(cached) and time.time() - float(_IFACE_CACHE["at"]) <= max_age
    return cached if fresh else []


def interface_pairs(max_age: float = IFACE_CACHE_SECONDS) -> List[Tuple[str, Optional[str]]]:
    """本机 (IPv4, 子网掩码) 列表, 带缓存。

    `max_age` 秒内直接复用上次结果; 这次枚举失败 (比如 ipconfig 起不来) 也退回上次的结果,
    免得突然退化成"假设 /24"瞎猜。多个线程同时要结果时只跑一次枚举, 其余的等缓存。
    """
    cached = _iface_cache(max_age)
    if cached:
        return cached

    with _IFACE_COMPUTE_LOCK:                 # 排队期间可能别人已经算好了
        cached = _iface_cache(max_age)
        if cached:
            return cached
        started = time.perf_counter()
        pairs = (_interfaces_via_ifconfig() if os.name == "posix"
                 else _interfaces_via_ipconfig_windows())
        elapsed = time.perf_counter() - started
        if elapsed > 0.5:   # 慢到会影响界面响应时, 在启动日志里留一条 (方便以后再查这类卡顿)
            try:
                from . import startup_log

                startup_log.step(t("⚠ 枚举网卡用了 {0:.2f} 秒 (ipconfig 慢成这样时不能在界面线程里做)").format(elapsed))
            except Exception:  # noqa: BLE001 - 记日志失败不能影响发现
                pass

        with _IFACE_LOCK:
            if pairs:
                _IFACE_CACHE["pairs"] = list(pairs)
                _IFACE_CACHE["at"] = time.time()
            elif cached:
                return cached
        return pairs


def warm_interfaces_async() -> None:
    """后台先把网卡枚举跑一遍。

    这样等真正要用 (建发现层 / 显示本机地址) 时缓存已经热了, 界面线程一秒都不用等。
    已经有缓存时不重复起线程。
    """
    with _IFACE_LOCK:
        if _IFACE_CACHE["pairs"]:
            return
    threading.Thread(target=interface_pairs, name="iface-warm", daemon=True).start()


def local_ipv4_addresses() -> List[str]:
    """返回本机所有 IPv4 地址 (不含 127.x)。"""
    found: List[str] = []
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        found.extend(addrs)
    except OSError:
        pass
    # 通过"到公网的默认路由"再拿一个 (VPN/多网卡时很有效)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    result: List[str] = []
    for ip in found:
        if ip.startswith("127.") or ip == "0.0.0.0":
            continue
        if ip not in result:
            result.append(ip)
    return result


def _broadcast_of(ip: str, netmask: str) -> Optional[str]:
    try:
        ip_i = int.from_bytes(socket.inet_aton(ip), "big")
        mask_i = int.from_bytes(socket.inet_aton(netmask), "big")
    except OSError:
        return None
    bcast = (ip_i & mask_i) | (~mask_i & 0xFFFFFFFF)
    addr = socket.inet_ntoa(bcast.to_bytes(4, "big"))
    return None if addr == ip else addr


def _interfaces_via_ipconfig_windows() -> List[Tuple[str, Optional[str]]]:
    """解析 `ipconfig` 输出, 得到 (IPv4, 子网掩码) 列表。

    ipconfig 的中文输出形如::

        以太网适配器 以太网:
           IPv4 地址 . . . . . . . . . . . . : 192.168.31.223
           子网掩码  . . . . . . . . . . . . : 255.255.255.0
           默认网关. . . . . . . . . . . . . : 192.168.31.1
    """
    try:
        completed = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            timeout=8,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        out = completed.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return []

    pairs: List[Tuple[str, Optional[str]]] = []
    current: Optional[str] = None
    mask: Optional[str] = None

    def flush() -> None:
        nonlocal current, mask
        if current and not current.startswith("127."):
            pairs.append((current, mask))
        current, mask = None, None

    for raw in out.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if not line[:1].isspace():  # 适配器标题行
            flush()
            continue
        lowered = line.lower()
        if any(word in lowered for word in _GATEWAY_WORDS):
            continue
        match = _IPV4_RE.search(line)
        if not match:
            continue
        ip = match.group(1)
        try:
            socket.inet_aton(ip)
        except OSError:
            continue
        if any(word in lowered for word in _MASK_WORDS):
            if current:
                mask = ip
        elif current is None:
            current = ip
    flush()
    return pairs


def _interfaces_via_ifconfig() -> List[Tuple[str, Optional[str]]]:
    try:
        out = subprocess.run(
            ["ifconfig"],
            capture_output=True,
            timeout=8,
            check=False,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pairs: List[Tuple[str, Optional[str]]] = []
    for block in re.split(r"\n(?=\S)", out):
        ip_match = re.search(r"inet(?: addr:)?\s*(\d+(?:\.\d+){3})", block)
        if not ip_match:
            continue
        ip = ip_match.group(1)
        if ip.startswith("127."):
            continue
        mask_match = re.search(r"(?:netmask|Mask:)\s*(\d+(?:\.\d+){3}|0x[0-9a-fA-F]{8})", block)
        mask = None
        if mask_match:
            raw = mask_match.group(1)
            if raw.lower().startswith("0x"):
                mask = socket.inet_ntoa(int(raw, 16).to_bytes(4, "big"))
            else:
                mask = raw
        pairs.append((ip, mask))
    return pairs


def broadcast_addresses(max_age: float = IFACE_CACHE_SECONDS) -> List[str]:
    """计算本机所有网段的广播地址, 兜底使用 255.255.255.255。

    `max_age` 秒内的结果直接复用缓存 —— 枚举网卡要跑 `ipconfig` (见 :func:`interface_pairs`)。
    """
    pairs = interface_pairs(max_age)
    targets: List[str] = []
    for ip, mask in pairs:
        if mask:
            bcast = _broadcast_of(ip, mask)
            if bcast and bcast not in targets:
                targets.append(bcast)
    if not targets:
        # 退一步: 假设 /24 网段
        for ip in local_ipv4_addresses():
            bcast = _broadcast_of(ip, "255.255.255.0")
            if bcast and bcast not in targets:
                targets.append(bcast)
    if not targets:
        targets.append("255.255.255.255")
    return targets


# ---------------------------------------------------------------------------
# 发现服务
# ---------------------------------------------------------------------------
@dataclass
class DiscoveredPeer:
    peer_id: str
    name: str
    host: str
    port: int
    last_seen: float = field(default_factory=time.time)
    version: int = PROTOCOL_VERSION
    # 这条记录是怎么来的: 发现层自己学到的 (False) 还是本地硬塞进来的 (True)。
    # 必须区分 —— 本地登记只说明"我知道这个地址", 跟"我能不能被搜到"毫无关系,
    # 混在一起会让隐身 (别人搜不到我) 看起来完全没效果。
    manual: bool = False

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class DiscoveryService:
    """UDP 广播发现。事件回调都在内部线程里触发, 需自行保证线程安全。"""

    def __init__(
        self,
        peer_id: str,
        name: str,
        tcp_port: int,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        on_peer_found: Optional[Callable[[DiscoveredPeer], None]] = None,
        on_peer_lost: Optional[Callable[[DiscoveredPeer], None]] = None,
        on_peer_updated: Optional[Callable[[DiscoveredPeer, DiscoveredPeer], None]] = None,
        discoverable: bool = True,
    ) -> None:
        self.peer_id = peer_id
        self.name = name
        self.tcp_port = tcp_port
        self.discovery_port = discovery_port
        self.on_peer_found = on_peer_found
        self.on_peer_lost = on_peer_lost
        self.on_peer_updated = on_peer_updated
        # 隐身模式: 不广播 beacon, 也不响应单播探测 —— 别人搜不到我, 只能手填我的 IP:TCP端口。
        # (我**仍然**收别人的广播, 所以我自己还能看到并主动添加别人)
        self.discoverable = bool(discoverable)

        self._lock = threading.RLock()
        self._peers: Dict[str, DiscoveredPeer] = {}      # peer_id -> peer
        self._by_endpoint: Dict[Tuple[str, int], str] = {}  # (host,port) -> peer_id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recv_sock: Optional[socket.socket] = None
        self._send_sock: Optional[socket.socket] = None
        self._own_ips = set(local_ipv4_addresses())
        # 广播目标**不在这里算**: 枚举网卡要跑 `ipconfig` (打包后 2.5 秒), 这里是在 Tk
        # 界面线程上被调用的 —— 卡住就是"设置完昵称先未响应几秒"。改成后台线程里做,
        # 已经有缓存就直接用 (见 warm_interfaces_async / _refresh_targets)。
        self._targets: List[str] = []
        self._refresh_targets(blocking=False)
        self._name_dirty = threading.Event()  # 昵称变化时立即重播

    # -- 网卡 / 广播目标 ---------------------------------------------------
    def _refresh_targets(self, blocking: bool = True) -> None:
        """更新广播目标列表 (空列表 = 还没算出来, 先不发广播)。

        `blocking=False`: 有缓存就直接用; 没有就丢给后台线程, 绝不阻塞调用者。
        """
        with _IFACE_LOCK:
            fresh = (bool(_IFACE_CACHE["pairs"])
                     and time.time() - float(_IFACE_CACHE["at"]) <= IFACE_CACHE_SECONDS)
        if fresh or blocking:
            self._targets = broadcast_addresses()
            return
        threading.Thread(target=self._warm_targets, name="iface-warm", daemon=True).start()

    def _warm_targets(self) -> None:
        try:
            self._targets = broadcast_addresses()
        except Exception:  # noqa: BLE001 - 后台线程里绝不能让异常冒出去
            pass

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lanchat-discovery", daemon=True)
        self._thread.start()

    def stop(self, goodbye: bool = True) -> None:
        self._stop.set()
        if goodbye and self.discoverable:
            try:
                self._send_beacon(kind="bye")
            except OSError:
                pass
        for sock in (self._recv_sock, self._send_sock):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def update_identity(self, name: str, tcp_port: int) -> None:
        with self._lock:
            self.name = name
            self.tcp_port = tcp_port
        self._name_dirty.set()

    def set_discoverable(self, value: bool) -> None:
        """隐身开关: 关掉后不发广播、不响应单播探测 (别人搜不到我)。"""
        changed = bool(value) != self.discoverable
        self.discoverable = bool(value)
        if changed:
            self._name_dirty.set()          # 立刻按新状态处理 (开=马上广播一次)

    # -- 查询 --------------------------------------------------------------
    def peers(self, include_stale: bool = False) -> List[DiscoveredPeer]:
        now = time.time()
        with self._lock:
            values = list(self._peers.values())
        out = [p for p in values if include_stale or now - p.last_seen <= PEER_TIMEOUT]
        out.sort(key=lambda p: (p.name.lower(), p.host))
        return out

    def get(self, peer_id: str) -> Optional[DiscoveredPeer]:
        with self._lock:
            return self._peers.get(peer_id)

    def add_manual(self, host: str, port: int, name: str = "") -> DiscoveredPeer:
        """手动登记一个对端 (跨网段/广播不可达时使用)。"""
        name = name or t("手动添加")
        peer = DiscoveredPeer(peer_id=f"manual:{host}:{port}", name=name, host=host, port=port,
                              manual=True)
        with self._lock:
            self._peers[peer.peer_id] = peer
            self._by_endpoint[(host, port)] = peer.peer_id
        if self.on_peer_found:
            self.on_peer_found(peer)
        return peer

    # -- 内部 --------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            self._recv_sock.bind(("", self.discovery_port))
            self._recv_sock.settimeout(0.4)

            self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._send_sock.settimeout(1.0)
        except OSError:
            self._cleanup_sockets()
            return

        # 网卡枚举放这里做 (后台线程): 打包后 `ipconfig` 要 2.5 秒, 在界面线程上就是"未响应"
        self._refresh_targets(blocking=True)

        last_beacon = 0.0
        last_iface = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now - last_iface >= IFACE_REFRESH_SECONDS:
                # VPN 是启动后才连上的情况: 定期重新枚举, 否则广播目标一直是旧的
                self._refresh_targets(blocking=True)
                last_iface = now
            # 隐身模式下不发广播 (别人在"局域网中的人"里看不到我)
            if self.discoverable and (now - last_beacon >= BEACON_INTERVAL
                                      or self._name_dirty.is_set()):
                self._send_beacon(kind="beacon")
                last_beacon = now
                self._name_dirty.clear()
            elif self._name_dirty.is_set():
                self._name_dirty.clear()
            self._poll(now)
            self._expire(now)

    def _cleanup_sockets(self) -> None:
        for sock in (self._recv_sock, self._send_sock):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        self._recv_sock = self._send_sock = None

    def _beacon_payload(self, kind: str = "beacon") -> bytes:
        with self._lock:
            payload = {
                "t": BEACON_TYPE,
                "v": PROTOCOL_VERSION,
                "k": kind,
                "id": self.peer_id,
                "name": self.name,
                "port": self.tcp_port,
                # 我的发现端口: 单播探测的回包要发到这里 (双方可能用了不同的 --discovery-port)
                "dport": self.discovery_port,
                "ts": int(time.time()),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _send_beacon(self, kind: str = "beacon") -> None:
        if not self._send_sock:
            return
        data = self._beacon_payload(kind)
        for target in self._targets:
            try:
                self._send_sock.sendto(data, (target, self.discovery_port))
            except OSError:
                pass

    def probe(self, host: str, port: Optional[int] = None) -> bool:
        """单播探测: 直接问某个 IP "你在吗", 对方会**单播**回一条 beacon。

        用途: 广播不通的组网 (Tailscale / WireGuard / 跨网段) 里只要填一个 IP 就能拿到
        对方**当前**的 TCP 端口, 于是双方都不用固定端口。
        对方如果在隐身模式 (设置里关掉了"允许被自动搜索"), 就不会回应 —— 那时只能手填
        `IP:TCP端口` 直接连。
        """
        if not self._send_sock:
            return False
        target_port = int(port or self.discovery_port)
        if not (0 < target_port < 65536):
            target_port = self.discovery_port
        try:
            self._send_sock.sendto(self._beacon_payload(PROBE_KIND), (host, target_port))
            return True
        except OSError:
            return False

    def _poll(self, now: float) -> None:
        if not self._recv_sock:
            return
        for _ in range(64):  # 一次最多处理 64 个包, 避免饿死超时检测
            try:
                data, addr = self._recv_sock.recvfrom(4096)
            except socket.timeout:
                return
            except OSError:
                return
            self._handle_datagram(data, addr, now)

    def _handle_datagram(self, data: bytes, addr: Tuple[str, int], now: float) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("t") != BEACON_TYPE:
            return
        if msg.get("id") == self.peer_id:
            return  # 自己的回声
        if msg.get("v") != PROTOCOL_VERSION:
            return
        host, _src_port = addr[0], addr[1]
        if host in self._own_ips:
            # 同一台机器上的其它实例: 源端口不是 TCP 端口, 直接用 beacon 里的 port
            pass
        try:
            port = int(msg.get("port", 0))
        except (TypeError, ValueError):
            return
        if not (0 < port < 65536):
            return
        peer_id = str(msg.get("id"))
        name = str(msg.get("name") or t("匿名"))

        if msg.get("k") == "bye":
            self._mark_lost(peer_id)
            return

        is_probe = msg.get("k") == PROBE_KIND
        if is_probe and not self.discoverable:
            # 隐身模式: 连"你在吗"都不回 (别人自然也就拿不到我的 TCP 端口)
            return

        with self._lock:
            existing = self._peers.get(peer_id)
            if existing is None:
                peer = DiscoveredPeer(peer_id=peer_id, name=name, host=host, port=port, last_seen=now)
                self._peers[peer_id] = peer
                self._by_endpoint[(host, port)] = peer_id
                new_peer = peer
                updated = None
            else:
                old = DiscoveredPeer(**vars(existing))
                existing.last_seen = now
                existing.name = name
                if existing.host != host or existing.port != port:
                    self._by_endpoint.pop((existing.host, existing.port), None)
                existing.host = host
                existing.port = port
                self._by_endpoint[(host, port)] = peer_id
                new_peer = None
                updated = (old, existing)

        if new_peer is not None:
            if self.on_peer_found:
                self.on_peer_found(new_peer)
        elif updated is not None and (updated[0].name != updated[1].name or updated[0].port != updated[1].port):
            if self.on_peer_updated:
                self.on_peer_updated(updated[0], updated[1])

        if is_probe:
            # 有人单播来问"你在吗" -> 单播回一条 beacon 给他 (带上我的真实 TCP 端口)。
            # 回包里带上 dport, 因为双方可能用了不同的发现端口。
            try:
                dport = int(msg.get("dport") or 0)
            except (TypeError, ValueError):
                dport = 0
            if not (0 < dport < 65536):
                dport = self.discovery_port
            try:
                if self._send_sock:
                    self._send_sock.sendto(self._beacon_payload("beacon"), (host, dport))
            except OSError:
                pass

    def _expire(self, now: float) -> None:
        with self._lock:
            stale = [p for p in self._peers.values() if now - p.last_seen > PEER_TIMEOUT and not p.peer_id.startswith("manual:")]
        for peer in stale:
            self._mark_lost(peer.peer_id)

    def _mark_lost(self, peer_id: str) -> None:
        with self._lock:
            peer = self._peers.pop(peer_id, None)
            if peer:
                self._by_endpoint.pop((peer.host, peer.port), None)
        if peer and self.on_peer_lost:
            self.on_peer_lost(peer)


def discover_once(
    duration: float = 3.0,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
) -> List[DiscoveredPeer]:
    """独立的小工具: 在 duration 秒内监听广播, 列出看得见的对端。"""
    found: Dict[str, DiscoveredPeer] = {}
    done = threading.Event()

    def on_found(peer: DiscoveredPeer) -> None:
        found[peer.peer_id] = peer

    svc = DiscoveryService(
        peer_id=f"probe-{os.getpid()}-{time.time_ns()}",
        name="probe",
        tcp_port=1,
        discovery_port=discovery_port,
        on_peer_found=on_found,
    )
    svc.start()
    done.wait(duration)
    svc.stop(goodbye=False)
    return sorted(found.values(), key=lambda p: p.name)
