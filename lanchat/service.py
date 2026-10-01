"""总控服务: 自动发现 + 监听 + 好友关系 + 多会话管理 + 文件传输。

对外接口 (GUI 与脚本都只用这一层)::

    svc = ChatService(on_event=...)
    svc.start()
    svc.set_name("小明")
    svc.lan_peers()                     # 局域网里看得见的人 (搜索/添加用)
    svc.send_friend_request(peer_id)    # 发送加好友请求
    svc.accept_request(peer_id)         # 同意加好友
    svc.reject_request(peer_id)         # 拒绝
    svc.block(peer_id) / unblock(...)   # 封禁 / 解禁
    svc.send_text("hi", peer_id)        # 加密聊天
    svc.send_file(path, peer_id)        # 加密传文件

线程模型:
    gui/main       : 调方法、处理事件回调
    tcp-accept     : 接受连接 -> 每个连接一个握手线程
    discovery      : UDP 广播收发
    conn-manager   : 给好友维持加密连接 (带退避重连) + 重发未处理的好友请求
    rx/tx/heartbeat: 每条会话的收发线程
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from . import crypto, protocol
from .constants import (
    ACCEPT_RETRY_SECONDS,
    CONNECT_TIMEOUT,
    CONTACTS_FILE,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_MAX_ANSWER_ATTEMPTS,
    DEFAULT_TCP_PORT,
    DOWNLOAD_DIR_NAME,
    HANDSHAKE_TIMEOUT,
    HISTORY_BATCH,
    HISTORY_JITTER,
    HISTORY_LIMIT,
    IDENTITY_FILE,
    PROTOCOL_VERSION,
    PROBE_ATTEMPTS,
    PROBE_TIMEOUT,
    RECONNECT_DELAYS,
    REKEY_EVERY_MESSAGES,
    REQUEST_ACK_TIMEOUT,
    REQUEST_MAX_RETRIES,
    SETTINGS_FILE,
    UNBLOCK_RETRY_SECONDS,
)
from .connection import Connection, OutgoingTransfer, TransferError
from .console import configure_console
from .crypto import HandshakeRejected, SecurityError
from .discovery import DiscoveredPeer, DiscoveryService, local_ipv4_addresses
from .identity import (
    IdentityCard,
    LocalIdentity,
    data_dir_writable_warning,
    fingerprint_of,
    resolve_app_dir,
)

# 服务层的事件文案里带 ✅ ⚠ 🔒 等字符: 命令行场景下先保证控制台是 UTF-8
configure_console()


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
class EventKind(str, Enum):
    READY = "ready"                    # 服务就绪
    STOPPED = "stopped"
    CONTACTS = "contacts"              # 好友/请求/黑名单/未读发生变化
    LAN_PEERS = "lan-peers"            # 局域网可见列表变化
    PRESENCE = "presence"              # 某人上线/离线/改名
    MESSAGE = "message"                # 收到或发出文本
    CONNECTED = "connected"            # 加密会话建立 / 好友确认
    DISCONNECTED = "disconnected"
    STATUS = "status"                  # 连接状态变化 (连接中/失败)
    FILE_OFFER = "file-offer"
    FILE_PROGRESS = "file-progress"
    FILE_DONE = "file-done"
    FILE_FAILED = "file-failed"
    QUESTION = "question"              # 别人出了验证问题, 需要界面提示作答
    QUESTION_RESULT = "question-result"  # 对方对"我提交的答案"的判定结果 (对/错)
    HISTORY = "history"                # 对方补发了一段历史对话 (重启/重连后)
    REKEY = "rekey"                    # 会话密钥轮换完成
    INFO = "info"
    ERROR = "error"


@dataclass
class ServiceEvent:
    kind: EventKind
    text: str = ""
    peer_id: str = ""
    name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class ContactState(str, Enum):
    FRIEND = "friend"            # 已是好友 (可加密聊天)
    REQUEST_IN = "request-in"    # 别人请求加我, 待我处理
    REQUEST_OUT = "request-out"  # 我请求加别人, 待对方处理
    BLOCKED = "blocked"          # 被我封禁
    STRANGER = "none"            # 只是见过, 没有关系


@dataclass
class Contact:
    peer_id: str
    name: str
    card: Optional[Dict[str, Any]] = None       # 对方身份卡 (含公钥, 用于校验身份)
    state: str = ContactState.FRIEND.value
    blocked: bool = False                       # 我封禁了对方
    blocked_by_peer: bool = False               # 对方封禁了我 (对方解禁后可以重新申请)
    friend_before_block: bool = False           # 封禁之前是好友: 解禁后恢复好友关系 (不删人)
    unblock_notice: bool = False                # 解禁后要主动告诉对方 (对方离线就等他上线/下次申请时补发)
    accept_pending: bool = False                # 我同意了对方, 但还没收到对方的"收到"回执 (要重发)
    request_acked: bool = True                  # 我发的加好友请求对方有没有回音 (没回音要重发)
    request_sent_at: float = 0.0                # 运行时: 上次发出请求的时间
    request_tries: int = 0                      # 运行时: 已经重发了几次
    manual_address: str = ""                    # 手动添加时填的 "IP:端口" (要落盘: 广播补不上)
    message: str = ""                           # 好友请求附言
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    unread: int = 0
    last_text: str = ""
    last_ts: float = 0.0
    last_seen: float = 0.0
    # 运行时状态 (不落盘)
    connection: Optional[Connection] = None
    address: str = ""
    dial_state: str = "idle"                    # idle / connecting / failed
    last_error: str = ""
    attempts: int = 0
    next_try: float = 0.0
    # 加好友验证问题 (可选): 只存"答案的哈希", 答案本身不上网
    question: str = ""
    answer_salt: str = ""
    answer_hash: str = ""
    max_attempts: int = 0                       # 0 = 用默认值
    attempts_left: int = 0                      # 运行时: 还允许错几次

    @property
    def has_question(self) -> bool:
        return bool(self.question and self.answer_hash)

    @property
    def fingerprint(self) -> str:
        if not self.card:
            return fingerprint_of(self.peer_id)
        try:
            return IdentityCard.from_dict(self.card).fingerprint
        except ValueError:
            return fingerprint_of(self.peer_id)

    @property
    def is_friend(self) -> bool:
        return self.state == ContactState.FRIEND.value and not self.blocked

    @property
    def connected(self) -> bool:
        return bool(self.connection and self.connection.is_alive)

    @property
    def encrypted(self) -> bool:
        return bool(self.connection and self.connection.is_alive and self.connection.authenticated)

    def to_storage(self) -> Dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "card": self.card,
            "state": self.state,
            "blocked": self.blocked,
            "blocked_by_peer": self.blocked_by_peer,
            "friend_before_block": self.friend_before_block,
            "unblock_notice": self.unblock_notice,
            "accept_pending": self.accept_pending,
            "manual_address": self.manual_address,
            "message": self.message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "unread": self.unread,
            "last_text": self.last_text,
            "last_ts": self.last_ts,
            "question": self.question,
            "answer_salt": self.answer_salt,
            "answer_hash": self.answer_hash,
            "max_attempts": self.max_attempts,
        }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class ChatService:
    """聊天/传输核心。GUI 只通过事件回调与方法使用它。"""

    def __init__(
        self,
        on_event: Optional[Callable[[ServiceEvent], None]] = None,
        name: str = "",
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        tcp_port: int = DEFAULT_TCP_PORT,
        download_dir: Optional[str] = None,
        auto_accept_files: bool = False,
        auto_connect_friends: bool = True,
        data_dir: Optional[str] = None,
        enable_discovery: bool = True,
        reconnect_cooldown: float = 3.0,
        rekey_every: int = REKEY_EVERY_MESSAGES,
        discoverable: bool = True,
    ) -> None:
        self.on_event = on_event
        self.discovery_port = discovery_port
        self.tcp_port = tcp_port
        # 命令行**明确**给了端口就以它为准; 没给 (0) 时才用设置里存的固定端口
        self.tcp_port_from_cli = bool(tcp_port)
        # 隐身: 不广播、不响应单播探测 -> 别人搜不到我, 只能手填我的 IP:TCP端口 加我
        self.discoverable = bool(discoverable)
        self.auto_accept_files = auto_accept_files
        self.auto_connect_friends = auto_connect_friends
        self.enable_discovery = enable_discovery
        self.reconnect_cooldown = reconnect_cooldown
        self.rekey_every = max(0, int(rekey_every))
        self.protocol_version = PROTOCOL_VERSION

        self.data_dir = resolve_app_dir(data_dir)
        self.data_dir_warning = data_dir_writable_warning()
        # 默认接收目录 = 系统下载目录; `download_dir` 参数 (或设置里选过) 才算"用户自己定的",
        # 只有用户定过的才记进 settings.json 并在升级后继续沿用 —— 否则老版本自动算出来的
        # 路径会把新默认值顶掉 (用户报过: 改了默认目录但还是老路径)。
        self.download_dir_chosen = bool(download_dir)
        self.download_dir, self.download_dir_warning = _prepare_dir(
            download_dir or default_download_dir()
        )
        boot_name = name.strip() or "未命名用户"
        self.identity, created = LocalIdentity.load_or_create(
            os.path.join(self.data_dir, IDENTITY_FILE), boot_name
        )
        self.identity_created = created
        self.peer_id = self.identity.peer_id

        self._lock = threading.RLock()
        self.saved_tcp_port = 0                     # 设置里固定的本机端口 (落盘, 重启生效)
        self._contacts: Dict[str, Contact] = {}
        self._lan_peers: Dict[str, DiscoveredPeer] = {}
        self._connections: Dict[str, Connection] = {}
        # 同一对端可能同时存在两条会话 (双方同时拨号)。`_connections` 只留一条作为"主用",
        # 被换下来的那条**不丢引用**: 去重时对端留下的可能正好是它, 丢掉引用就再也发不出
        # 消息/请求了 (踩过: 解禁后对方申请请求一直送不到, 要等 30 秒重拨才可能碰上)。
        self._spare_connections: Dict[str, List[Connection]] = {}
        self._dial_locks: Dict[str, threading.Lock] = {}
        self._incoming_questions: Dict[str, Dict[str, Any]] = {}  # 别人向我提问, 待作答
        self._challenge_state: Dict[str, Dict[str, Any]] = {}     # 我提问后等待校验的状态
        self._peer_questions: Dict[str, Dict[str, Any]] = {}      # 我给谁出过题 (按 peer_id 存)
        # 从**发现层**(广播/单播探测回复)真正学到的人。手动登记的地址不算 ——
        # "探测到了"必须意味着"对方真的回应了", 否则本地登记过就成了假成功
        # (自测模式下就踩过: 明明对方开了隐身, 探测却"成功"了)。
        self._discovered_ids: set = set()
        # ---- 会话内聊天记录 (只在内存, 关掉程序就没了; 故意不落盘) ----
        # 为什么要有: 以前聊天内容只活在界面那个文本框里 (关掉/重连就没了, 连切换联系人都清空)。
        # 现在按人记一份, 对方重启或重连上来时把缺的部分补发给他; 两边都退出就彻底没了。
        self._chat_log: Dict[str, List[Dict[str, Any]]] = {}
        self._out_seq: Dict[str, int] = {}       # 我给这个人的消息序号 (从 1 开始)
        self._peer_seq: Dict[str, int] = {}      # 我收到他的最高序号
        self._stop = threading.Event()
        self._started = False
        self._simulating = False           # 自测: 正在"模拟掉线" (见 simulate_offline)
        self._active_peer = ""             # 界面正开着谁的对话 (新消息不攒未读, 见 set_active_peer)
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._manager_thread: Optional[threading.Thread] = None
        self._discovery: Optional[DiscoveryService] = None

        self._load_contacts()
        self._load_settings()

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> None:
        if self._started:
            return
        from . import startup_log

        startup_log.step("服务: 绑定 TCP 监听…")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("", self.tcp_port))
        self._listener.listen(32)
        self._listener.settimeout(0.5)
        self.tcp_port = self._listener.getsockname()[1]

        if self.enable_discovery:
            # 这一步里会枚举本机网卡 (Windows 上要跑一次 `ipconfig`), 在打包后的窗口版里
            # 可能花掉好几秒 —— 放在主线程上就是"未响应" (用户报过: 设置完昵称卡几秒)。
            startup_log.step("服务: 建发现层 (枚举网卡 / 广播地址)…")
            self._discovery = DiscoveryService(
                peer_id=self.peer_id,
                name=self.name,
                tcp_port=self.tcp_port,
                discovery_port=self.discovery_port,
                on_peer_found=self._on_lan_peer_found,
                on_peer_lost=self._on_lan_peer_lost,
                on_peer_updated=self._on_lan_peer_updated,
                discoverable=self.discoverable,
            )
            startup_log.step("服务: 发现层就绪")
        self._stop.clear()
        startup_log.step("服务: 启动接收/管理线程…")
        self._accept_thread = threading.Thread(target=self._accept_loop, name="tcp-accept", daemon=True)
        self._accept_thread.start()
        self._manager_thread = threading.Thread(target=self._manager_loop, name="conn-manager", daemon=True)
        self._manager_thread.start()
        if self._discovery is not None:
            self._discovery.start()
        self._started = True

        startup_log.step("服务: 整理本机地址/指纹…")
        tips = [f"我是「{self.name}」", f"TCP 端口 {self.tcp_port}",
                "本机地址 " + (", ".join(local_ipv4_addresses()) or "未知"),
                f"身份指纹 {self.fingerprint}"]
        if self.identity_created:
            tips.append("已生成新的身份密钥对")
        for warning in (self.data_dir_warning, self.download_dir_warning):
            if warning:
                tips.append(warning)
        self._emit(EventKind.READY, " | ".join(tips))
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.LAN_PEERS)
        startup_log.step("服务: 启动完成")

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._discovery:
            self._discovery.stop(goodbye=True)
            self._discovery = None
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
        for conn in conns:
            conn.close("本机退出")
        self._save_contacts()
        self._save_settings()
        self._started = False
        self._emit(EventKind.STOPPED, "服务已停止")

    def simulate_offline(self, seconds: float = 6.0) -> bool:
        """自测专用: 真的把本机"下线"几秒再回来 (不是装样子)。

        做的都是真事:
          * 断开所有会话 → 对方立刻显示"对方不在线";
          * 停掉发现层 (会发一条 bye) → 对方**马上**把我标成离线, 不用等 12 秒超时;
          * 关掉 TCP 监听 → 这几秒里谁也连不上我;
          * 到点重新监听 (尽量还用原端口) + 重开发现层, 让两边的自动重连把我接回来。

        回来之后对方还会把这期间缺的聊天记录补发给我 (见 :meth:`conversation`),
        所以这一个按钮能把"掉线 → 离线提示 → 自动重连 → 历史补发"整条链路验一遍。
        """
        if not self._started or self._simulating:
            return False
        self._simulating = True
        keep_port = self.tcp_port

        def worker() -> None:
            try:
                self._emit(EventKind.INFO, f"🧪 自测: 模拟掉线 {float(seconds):.0f} 秒 "
                                           f"(对方会看到我离线, 之后自动重连)…")
                with self._lock:
                    conns = list(self._connections.values())
                for conn in conns:
                    try:
                        conn.close("自测: 模拟掉线")
                    except Exception:  # noqa: BLE001 - 模拟掉线时出错不该影响后续
                        pass
                self.stop()
                time.sleep(max(0.5, float(seconds)))
                self.tcp_port = keep_port          # 尽量还用原来那个端口
                try:
                    self.start()
                except OSError:
                    self.tcp_port = 0              # 端口被占了就退回随机端口
                    self.start()
                self._emit(EventKind.INFO, "🧪 自测: 已恢复上线 (对方的自动重连会把我接回来)")
                self._emit(EventKind.STATUS)
            except Exception as exc:  # noqa: BLE001 - 自测功能也要如实报错, 不能静默
                self._emit(EventKind.ERROR, f"模拟掉线失败: {type(exc).__name__}: {exc}")
            finally:
                self._simulating = False

        threading.Thread(target=worker, name="simulate-offline", daemon=True).start()
        return True

    # ==================================================================
    # 基本属性
    # ==================================================================
    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def fingerprint(self) -> str:
        return self.identity.fingerprint

    def set_name(self, name: str, persist: bool = True) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("昵称不能为空")
        self.identity.set_name(name)
        if persist:
            self.identity.save(os.path.join(self.data_dir, IDENTITY_FILE))
            if self._discovery:
                self._discovery.update_identity(name, self.tcp_port)
            self._emit(EventKind.INFO, f"昵称已改为「{name}」")
            self._emit(EventKind.CONTACTS)
        return name

    def set_auto_accept_files(self, enabled: bool) -> None:
        self.auto_accept_files = bool(enabled)
        for conn in self._live_connections():
            conn.auto_accept_files = self.auto_accept_files
        self._save_settings()

    def set_auto_connect_friends(self, enabled: bool) -> None:
        self.auto_connect_friends = bool(enabled)
        self._save_settings()

    def set_rekey_every(self, count: int) -> None:
        """设置密钥轮换频率 (每收发多少条消息换一次; 0 = 关闭)。

        新连接在握手时就和对方**协商**(见 crypto.negotiate_rekey: 有 0 就关闭,
        否则取更严格的那个); 已经在跑的会话会**通知对方**一起重新协商, 所以两边
        随时保持一致, 不用重连。
        """
        self.rekey_every = max(0, int(count))
        for conn in self._live_connections():
            effective = conn.set_local_rekey(self.rekey_every)
            conn.send_message({"t": "rekey-pref", "rekey": self.rekey_every})
            self._emit(EventKind.INFO,
                       f"与 {conn.peer_name} 协商后的轮换频率: "
                       + (f"每 {effective} 条消息" if effective else "关闭"))
        self._save_settings()
        self._emit(EventKind.INFO,
                   f"密钥轮换: 每 {self.rekey_every} 条消息一次" if self.rekey_every
                   else "密钥轮换已关闭")

    def _handle_rekey_pref(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方改了他的轮换设置 -> 重新协商 (双方结果一致)。"""
        try:
            value = max(0, int(message.get("rekey", 0)))
        except (TypeError, ValueError):
            return
        effective = conn.set_peer_rekey(value)
        self._emit(EventKind.INFO,
                   f"{conn.peer_name} 把密钥轮换设为每 {value} 条; "
                   f"协商后这条会话: "
                   + (f"每 {effective} 条消息换一次" if effective else "不自动轮换"),
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"notice": True, "level": "info", "silent": True})

    # ==================================================================
    # 通讯录
    # ==================================================================
    def contacts(self, state: Optional[str] = None) -> List[Contact]:
        with self._lock:
            items = list(self._contacts.values())
        items.sort(key=lambda c: (-(c.last_ts or c.created_at), c.name.lower()))
        if state:
            return [c for c in items if c.state == state]
        return items

    def friends(self) -> List[Contact]:
        return [c for c in self.contacts() if c.is_friend]

    def pending_requests(self) -> List[Contact]:
        return [c for c in self.contacts() if c.state == ContactState.REQUEST_IN.value]

    def blocked_contacts(self) -> List[Contact]:
        return [c for c in self.contacts() if c.blocked]

    def get_contact(self, peer_id: str) -> Optional[Contact]:
        if not peer_id:
            return None
        with self._lock:
            contact = self._contacts.get(peer_id)
        if contact is not None:
            return contact
        key = peer_id.strip().lower()
        for candidate in self.contacts():
            if candidate.name.lower() == key or candidate.fingerprint.lower().startswith(key):
                return candidate
        return None

    def register_manual_peer(self, peer_id: str, name: str, host: str, port: int,
                             manual: bool = True) -> None:
        """手工登记一个已知地址的对端。

        用于两种情况: ① 广播跨不过路由时手动填 IP:端口; ② 自动化测试里跳过发现层。

        `manual=True` 表示"这个地址是我自己塞进去的" —— 它**不代表**对方能被搜到,
        所以隐身是否生效只能看发现层学到的记录 (:meth:`is_discovered`)。
        """
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            # 端口 0 = "让系统随机分配", 对**对方**来说不是有效地址。
            # 踩过一次: 服务还没 start() (端口还没绑定) 就先登记, 结果列表里能看到人、
            # 请求却永远发不出去, 只在状态栏写一句"对方不在线", 很难查。
            self._emit(EventKind.ERROR, f"登记 {name} 失败: 端口无效 ({port})")
            return
        peer = DiscoveredPeer(peer_id=peer_id, name=name, host=host, port=port, manual=manual)
        with self._lock:
            previous = self._lan_peers.get(peer_id)
            if previous is not None and not getattr(previous, "manual", False):
                # 发现层已经自己学到这个人了 (信息更新鲜): 不要用本地登记把它顶掉,
                # 否则"隐身"又变成看不出效果的老问题。
                return
            self._lan_peers[peer_id] = peer
        self._emit(EventKind.LAN_PEERS)
        self._emit(EventKind.PRESENCE, f"已手动登记 {name} ({host}:{port})",
                   peer_id=peer_id, name=name)

    def is_discovered(self, peer_id: str) -> bool:
        """这个人真的是**发现层**(广播 / 单播探测)自己学到的吗?

        本地 :meth:`register_manual_peer` 登记的不算 —— 那只是我们自己塞进去的地址。
        隐身 / "别人搜不到我" 这类判断只能看这个。
        """
        with self._lock:
            return peer_id in self._discovered_ids

    def add_manual_peer(self, host: str, port: int = 0, name: str = "",
                        probe_port: int = 0) -> Optional[Contact]:
        """手动添加对端, 并立刻发起加好友请求 (手动模式)。

        什么时候需要: 对方和我之间**广播不通** —— Tailscale / WireGuard / OpenVPN(tun)
        这类三层组网里压根没有广播, 跨网段/跨 VLAN 也一样。那种情况下自动发现永远看不到
        对方, 只能把地址填进来。

        两种填法:
          * **只填 IP** (port=0): 先发一个 **UDP 单播探测**问对方"你在吗", 对方(没开隐身时)
            会单播回一条 beacon, 里面带着它**当前随机的 TCP 端口** -> 直接连过去。
            这样双方都不用固定端口, 也不用知道对方端口是多少。
            探测打在对方的**发现端口**上: 默认试本机配置的那个 + 标准 50505
            (双方都改了 `--discovery-port` 时也能对上; `probe_port` 可显式指定)。
          * 填 `IP:TCP端口`: 跳过探测, 直接连过去。用于对方开了隐身 (不回应探测),
            或者你把发现层关了的情况。
        """
        host = normalize_address_input(host)     # 中文冒号/全角数字/空格等一律先归一化
        try:
            port = int(port or 0)
        except (TypeError, ValueError):
            port = 0
        if not port and ":" in host:
            # 也允许调用方把 "IP:端口" 整串塞进 host (GUI 会自己拆, 但脚本/调用方可能偷懒)
            host, port, err = split_manual_address(host)
            if err:
                self._emit(EventKind.ERROR, f"{err} —— 请填对方 IP (或 IP:TCP端口)")
                return None
        if not host:
            self._emit(EventKind.ERROR, "地址不能为空: 请填对方 IP (或 IP:TCP端口)")
            return None
        try:
            socket.gethostbyname(host)          # 允许主机名, 但必须能解析
        except OSError:
            self._emit(EventKind.ERROR, f"解析不了这个地址: {host}")
            return None

        if not port:
            # 只给了 IP -> 单播探一次对方的发现端口, 问出它真实的 TCP 端口
            peer = self._probe_peer(host, probe_port)
            if peer is None:
                return None
            label = name.strip()
            if label:
                self._rename_peer(peer.peer_id, label)
            self._emit(EventKind.INFO,
                       f"探测到 {peer.name} ({peer.host}:{peer.port}), 正在发送加好友请求…",
                       peer_id=peer.peer_id, name=peer.name)
            self.send_friend_request(peer.peer_id)
            return self.get_contact(peer.peer_id)

        if not (0 < port < 65536):
            self._emit(EventKind.ERROR, f"端口超出范围: {port} (或者只填 IP, 让程序自动探测)")
            return None
        placeholder = manual_peer_id(host, port)
        label = name.strip() or f"手动添加 {host}:{port}"
        with self._lock:
            self._lan_peers[placeholder] = DiscoveredPeer(
                peer_id=placeholder, name=label, host=host, port=port,
                # manual=True: 这个地址是我自己填的, 不是搜到的人 —— 界面上要能看出来
                # (它在握手后会被迁移成真实身份, 届时会被发现层记录取代)
                manual=True)
        self._emit(EventKind.LAN_PEERS)
        self._emit(EventKind.INFO, f"已手动登记 {host}:{port}, 正在发送加好友请求…")
        self.send_friend_request(placeholder)
        return self.get_contact(placeholder)

    def _probe_ports(self) -> List[int]:
        """只填 IP 时, 单播探测该打在哪个端口上。

        先试本机配置的发现端口 (双方都改成同一个自定义端口时能对上), 再补一个标准 50505
        (对方用默认端口时能对上)。对方的发现端口如果跟我们完全不一样, 就探不到 ——
        那种情况只能填 `IP:TCP端口`。
        """
        ports: List[int] = []
        for candidate in (self.discovery_port, DEFAULT_DISCOVERY_PORT):
            value = int(candidate or 0)
            if 0 < value < 65536 and value not in ports:
                ports.append(value)
        return ports

    def _probe_peer(self, host: str, port: int = 0,
                    name: str = "") -> Optional[DiscoveredPeer]:
        """单播探测: 只给一个 IP, 问出对方的 TCP 端口 (对方隐身时会失败)。"""
        with self._lock:
            discovery = self._discovery
        if discovery is None:
            self._emit(EventKind.ERROR,
                       "本机没开自动发现, 不能自动探测端口 —— 请填 `IP:TCP端口` 直接连")
            return None
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            self._emit(EventKind.ERROR, f"解析不了这个地址: {host}")
            return None
        try:
            explicit = int(port or 0)
        except (TypeError, ValueError):
            explicit = 0
        candidates = [explicit] if 0 < explicit < 65536 else self._probe_ports()
        self._emit(EventKind.INFO,
                   f"正在探测 {ip} (端口 {'/'.join(str(p) for p in candidates)})…")
        for candidate in candidates:
            for _ in range(max(1, PROBE_ATTEMPTS)):
                discovery.probe(ip, candidate)
                deadline = time.time() + max(0.3, PROBE_TIMEOUT)
                while time.time() < deadline:
                    with self._lock:
                        found = [p for p in self._lan_peers.values()
                                 if p.host == ip and p.peer_id in self._discovered_ids
                                 and not p.peer_id.startswith(MANUAL_PREFIX)]
                    if found:
                        found.sort(key=lambda p: p.last_seen, reverse=True)
                        return found[0]
                    time.sleep(0.1)
        self._emit(EventKind.ERROR,
                   f"探测不到 {ip} —— 对方可能没开程序、开了『隐身(不允许被搜索)』、"
                   f"改了发现端口, 或者防火墙挡住了 UDP。\n"
                   f"这种情况请改用『填 IP:TCP端口』直接连。")
        return None

    def _rename_peer(self, peer_id: str, name: str) -> None:
        with self._lock:
            peer = self._lan_peers.get(peer_id)
            if peer is not None:
                peer.name = name
        self._emit(EventKind.LAN_PEERS)

    def _adopt_real_identity(self, contact: Contact, real_peer_id: str) -> Contact:
        """手动添加的对端握手之后才知道真身份: 把联系人从占位 id 迁到真身份上。

        不迁移的话, 联系人的 key 是 `manual:1.2.3.4:50606`、会话的 key 是 `lc-xxx`,
        发消息/收消息/保存/重连全都会错位。
        """
        placeholder = contact.peer_id
        if placeholder == real_peer_id or not placeholder.startswith(MANUAL_PREFIX):
            return contact
        with self._lock:
            existing = self._contacts.get(real_peer_id)
            if existing is not None and existing is not contact:
                # 真身已经在通讯录里 (既手动加过、又被广播发现过): 合并到真身
                existing.address = existing.address or contact.address
                existing.manual_address = existing.manual_address or contact.manual_address
                existing.message = existing.message or contact.message
                existing.card = existing.card or contact.card
                self._contacts.pop(placeholder, None)
                contact = existing
            else:
                self._contacts.pop(placeholder, None)
                contact.peer_id = real_peer_id
                self._contacts[real_peer_id] = contact
            self._lan_peers.pop(placeholder, None)
            if placeholder in self._peer_questions:
                # 我给他出的题也要跟着换 key, 否则加回来时题目就"丢了"
                if real_peer_id not in self._peer_questions:
                    self._peer_questions[real_peer_id] = self._peer_questions[placeholder]
                self._peer_questions.pop(placeholder, None)
        self._emit(EventKind.LAN_PEERS)
        self._emit(EventKind.INFO, f"已确认手动添加的 {contact.name} 身份 ({contact.fingerprint})",
                   peer_id=contact.peer_id, name=contact.name)
        self._save_contacts()
        return contact

    def lan_peers(self) -> List[Dict[str, Any]]:
        """局域网里看得见的人 (含是否已是好友/待处理/已封禁)。"""
        with self._lock:
            peers = list(self._lan_peers.values())
            contacts = dict(self._contacts)
        out = []
        for peer in peers:
            if peer.peer_id == self.peer_id:
                continue
            contact = contacts.get(peer.peer_id)
            out.append({
                "peer_id": peer.peer_id,
                "name": peer.name,
                "host": peer.host,
                "port": peer.port,
                "address": peer.address,
                "last_seen": peer.last_seen,
                # manual=True: 地址是本地登记进去的 (不代表对方能被搜到)。
                # 界面上标成『手动登记』, 避免把"广播发现"和"自己填的地址"混为一谈。
                "manual": bool(getattr(peer, "manual", False)),
                "fingerprint": fingerprint_of(peer.peer_id),
                "state": contact.state if contact else ContactState.STRANGER.value,
                "blocked": contact.blocked if contact else False,
                "connected": contact.connected if contact else False,
            })
        out.sort(key=lambda item: item["name"].lower())
        return out

    # ==================================================================
    # 好友请求 / 封禁
    # ==================================================================
    def send_friend_request(self, peer_id: str, message: str = "") -> bool:
        """对搜索到的人发起加好友请求 (对方同意后才会建立加密聊天)。

        注意"验证问题"是**对方**给我们出的: 对方设了问题, 我们发请求后会在界面上
        收到提问, 答对了对方才会把请求放进『新朋友』; 答错到上限会被对方自动封禁。
        所以这里没有"问题/答案"参数 —— 出题永远由被加的一方做 (见 :meth:`set_question`)。
        """
        peer_id = (peer_id or "").strip()
        contact = self.get_contact(peer_id)
        if contact and contact.blocked:
            # contact.blocked = **我**封了他。这个不分方向会搞错: 对方封我时只记
            # blocked_by_peer, 不该拦着我"重新申请"。
            self._emit(EventKind.ERROR, f"{contact.name} 已被你封禁, 请先解禁")
            return False
        if contact and contact.blocked_by_peer:
            # 对方之前封了我: 清掉标记再试一次。如果对方还没解禁, 他会用
            # code=blocked 或一条 blocked 消息明确回绝, 我这边会自动把标记加回来
            # (以前是静默丢弃, 用户完全不知道发生了什么)。
            # 注意**不要**清 friend_before_block: 那记着"我们本来是不是好友",
            # 清了的话对方解禁后我就变成陌生人了 (报过: 解禁后好友没了)。
            contact.blocked_by_peer = False
            contact.state = ContactState.REQUEST_OUT.value
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            self._emit(EventKind.INFO, f"正在试探 {contact.name} 是否已解除封禁…")
        if contact and contact.is_friend:
            self._emit(EventKind.INFO, f"{contact.name} 已经是你的好友了")
            return True
        if contact and contact.state == ContactState.REQUEST_IN.value:
            self.accept_request(contact.peer_id)   # 互相同意
            return True

        if contact is None:
            discovered = self._lan_peers.get(peer_id)
            name = discovered.name if discovered else peer_id
            contact = Contact(peer_id=peer_id, name=name, state=ContactState.REQUEST_OUT.value,
                              message=message)
            with self._lock:
                self._contacts[peer_id] = contact
        else:
            contact.state = ContactState.REQUEST_OUT.value
            contact.message = message
            contact.updated_at = time.time()
            contact.next_try = 0.0
        manual = manual_address(peer_id)
        if manual is not None:
            # 手动填的地址要**落盘**: 这种网络里没有广播, 发现层永远补不上这个地址,
            # 不存下来重启后就再也连不上了。
            contact.manual_address = f"{manual[0]}:{manual[1]}"
            contact.address = contact.manual_address
        # 这次是"刚发出": 还没收到对方任何回应, 等回音 (超时就重发)
        contact.request_acked = False
        contact.request_sent_at = 0.0
        contact.request_tries = 0

        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO, f"已向 {contact.name} 发出加好友请求, 等待对方确认…")
        threading.Thread(target=self._dial_and_send_request, args=(contact.peer_id,),
                         name=f"req-{contact.name}", daemon=True).start()
        return True

    def cancel_friend_request(self, peer_id: str, notify: bool = True) -> bool:
        """取消自己发出的加好友请求 (对方还没同意时), 同时放弃回答对方出的验证问题。

        用户要的场景: 对方已经下线/不想答那道题了, 得有个"算了, 不加了"的出口。
        """
        contact = self.get_contact(peer_id)
        if contact is None:
            with self._lock:
                self._incoming_questions.pop(peer_id, None)
            self._emit(EventKind.INFO, "没有待取消的加好友请求")
            return False
        if contact.is_friend:
            self._emit(EventKind.INFO, f"{contact.name} 已经是好友了, 不需要取消请求")
            return False
        name = contact.name
        with self._lock:
            self._incoming_questions.pop(peer_id, None)
            self._challenge_state.pop(peer_id, None)
        conn = self._live_connection(contact)
        if conn is not None:
            if notify:
                conn.send_message({"t": "friend-cancel", "name": self.name, "ts": time.time()})
                time.sleep(0.15)             # 给写线程一点时间把这条通知发出去
            conn.close("已取消加好友请求")
        if contact.state == ContactState.REQUEST_OUT.value:
            with self._lock:
                self._contacts.pop(peer_id, None)
        else:
            contact.state = ContactState.STRANGER.value
            contact.updated_at = time.time()
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO, f"已取消发给 {name} 的加好友请求")
        return True

    def _contact_for_settings(self, peer_id: str) -> Optional[Contact]:
        """取联系人; 如果只是"局域网里见过但还没建立关系", 也允许先建一条空记录。

        这样"提前给某人设好验证问题"就成立 —— 对方之后发请求时直接就要答题。
        (仅限通过广播发现过的真实身份; 测试用的手工地址记录不会被建成联系人。)
        """
        contact = self.get_contact(peer_id)
        if contact is not None:
            return contact
        with self._lock:
            discovered = self._lan_peers.get(peer_id)
        if discovered is None:
            return None
        contact = Contact(peer_id=peer_id, name=discovered.name,
                          state=ContactState.STRANGER.value)
        with self._lock:
            self._contacts[peer_id] = contact
        return contact

    # -- 加好友验证问题 ----------------------------------------------------
    #
    # 题目**按 peer_id 单独存** (`_peer_questions`, 落在 settings.json), 不挂在联系人记录上:
    # 否则"删掉好友"会把题目一起删掉, 对方再加回来就不用答题了 —— 等于验证问题形同虚设
    # (用户报过这个)。
    def _question_for(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """取我给这个人出的题 (没有就返回 None)。"""
        with self._lock:
            item = self._peer_questions.get(peer_id)
            if item and item.get("question") and item.get("hash"):
                return item
            contact = self._contacts.get(peer_id)
        if contact is not None and contact.has_question:
            # 兼容旧数据: 题目还挂在联系人记录上
            return {"question": contact.question, "salt": contact.answer_salt,
                    "hash": contact.answer_hash,
                    "max_attempts": contact.max_attempts or DEFAULT_MAX_ANSWER_ATTEMPTS,
                    "attempts_left": contact.max_attempts or DEFAULT_MAX_ANSWER_ATTEMPTS}
        return None

    def has_question_for(self, peer_id: str) -> bool:
        return self._question_for(peer_id) is not None

    def max_attempts_for(self, peer_id: str) -> int:
        item = self._question_for(peer_id)
        if not item:
            return DEFAULT_MAX_ANSWER_ATTEMPTS
        return max(1, int(item.get("max_attempts") or DEFAULT_MAX_ANSWER_ATTEMPTS))

    def set_question(self, peer_id: str, question: str, answer: str,
                     max_attempts: int = DEFAULT_MAX_ANSWER_ATTEMPTS) -> bool:
        """给某个人设置"加好友验证问题": 对方答对才允许把请求送进『新朋友』。

        本机只保存 盐 + 派生密钥 K = PBKDF2(答案, 盐); 答案明文既不落盘也不留在内存,
        校验时直接用 K 算证明 (所以重启之后依然有效)。题目按 peer_id 保存, 不看联系人
        是否还在 —— 删了好友再加回来, 照样要重新答题。
        """
        contact = self._contact_for_settings(peer_id)
        question = (question or "").strip()
        if not question or not (answer or "").strip():
            if contact is None and not self.has_question_for(peer_id):
                self._emit(EventKind.ERROR, "找不到这个人 (对方可能不在局域网了)")
                return False
            return self.clear_question(peer_id)
        salt = crypto.new_answer_salt()
        attempts = max(1, int(max_attempts))
        with self._lock:
            self._peer_questions[peer_id] = {
                "question": question, "salt": salt, "hash": crypto.answer_hash(answer, salt),
                "max_attempts": attempts, "attempts_left": attempts,
            }
        if contact is not None:                   # 有联系人就顺手同步一份 (旧数据兼容/可读)
            contact.question = question
            contact.answer_salt = salt
            contact.answer_hash = self._peer_questions[peer_id]["hash"]
            contact.max_attempts = attempts
            contact.attempts_left = attempts
            contact.updated_at = time.time()
        self._forget_question_state(peer_id)      # 改了题, 之前答对过的人也要重答
        self._save_contacts()
        self._save_settings()
        self._emit(EventKind.CONTACTS)
        name = contact.name if contact else peer_id
        self._emit(EventKind.INFO, f"已为 {name} 设置加好友验证问题 (答错 {attempts} 次自动封禁)")
        return True

    def clear_question(self, peer_id: str) -> bool:
        """取消出题: 对方以后再申请就不用答题了。"""
        with self._lock:
            had = bool(self._peer_questions.pop(peer_id, None))
            contact = self._contacts.get(peer_id)
        if contact is not None and contact.has_question:
            had = True
            contact.question = ""
            contact.answer_salt = ""
            contact.answer_hash = ""
            contact.attempts_left = 0
            contact.updated_at = time.time()
        if not had:
            return False
        self._forget_question_state(peer_id)
        self._save_contacts()
        self._save_settings()
        self._emit(EventKind.CONTACTS)
        name = contact.name if contact is not None else peer_id
        self._emit(EventKind.INFO, f"已取消 {name} 的加好友验证问题")
        return True

    def question_of(self, peer_id: str) -> str:
        item = self._question_for(peer_id)
        return str(item.get("question", "")) if item else ""

    def _forget_question_state(self, peer_id: str) -> None:
        """忘掉"这个人答对过我的题"的记忆。

        删好友 / 拒绝请求 / 封禁 / 改题 时都要清掉: 否则删了他再加回来时,
        因为还留着 `solved` 标记, 直接就放行了 —— 等于验证问题形同虚设。
        """
        with self._lock:
            self._challenge_state.pop(peer_id, None)
            self._incoming_questions.pop(peer_id, None)

    def answer_challenge(self, peer_id: str, answer: str) -> bool:
        """回答对方的验证问题 (由界面在收到提问时调用)。"""
        with self._lock:
            pending = self._incoming_questions.get(peer_id)
        if not pending:
            self._emit(EventKind.ERROR, "这条验证问题已经失效 (对方重新提问后才能作答)")
            return False
        return self._send_answer(peer_id, answer, pending)

    def pending_challenge(self, peer_id: str) -> Optional[Dict[str, Any]]:
        """返回"别人向我提问、还没回答"的问题。"""
        with self._lock:
            pending = self._incoming_questions.get(peer_id)
        return dict(pending) if pending else None

    def pending_challenges(self) -> List[Dict[str, Any]]:
        """所有"别人向我提问、还没回答"的问题 (供界面列出并作答)。"""
        with self._lock:
            items = [(peer_id, dict(state)) for peer_id, state in self._incoming_questions.items()]
        out = []
        for peer_id, state in items:
            contact = self.get_contact(peer_id)
            with self._lock:
                discovered = self._lan_peers.get(peer_id)
            name = (contact.name if contact else "") or (discovered.name if discovered else peer_id)
            out.append({
                "peer_id": peer_id,
                "name": name,
                "question": str(state.get("question", "")),
                "nonce": str(state.get("nonce", "")),
                "max_attempts": int(state.get("max_attempts", DEFAULT_MAX_ANSWER_ATTEMPTS) or 1),
                "connected": bool(contact and contact.connected),
            })
        out.sort(key=lambda item: item["name"].lower())
        return out

    def _send_answer(self, peer_id: str, answer: str, pending: Dict[str, Any]) -> bool:
        contact = self.get_contact(peer_id)
        conn = contact.connection if contact else None
        if conn is None or not conn.is_alive:
            self._emit(EventKind.ERROR, "和对方的连接已断开, 无法提交答案 (请重新发送加好友请求)")
            return False
        salt = str(pending.get("salt", ""))
        nonce = str(pending.get("nonce", ""))
        try:
            proof = crypto.answer_proof(answer, salt, nonce)
        except crypto.SecurityError as exc:
            self._emit(EventKind.ERROR, f"答案计算出错: {exc}")
            return False
        if not conn.send_message({"t": "question-answer", "proof": proof}):
            self._emit(EventKind.ERROR, "答案发送失败, 请重新发送加好友请求")
            return False
        # 注意: 这里**不**清掉待答问题 —— 答错了要能直接在同一个弹窗里重试
        # (对方那边同一个随机数还留着, 直到答对才作废)
        self._emit(EventKind.INFO, "已提交答案, 等待对方校验…")
        return True

    def _ask_question(self, conn: Connection, question: Dict[str, Any]) -> None:
        """自己设了验证问题 -> 给请求方发一个随机挑战 (答案本身不上网)。

        同一道题如果对方还没答对就重复申请, **沿用同一个随机数**: 这样对方界面上那道题
        一直有效, 不会出现"我照着屏幕上的题答了却算错"(对方换了随机数)的情况。
        """
        text = str(question.get("question", ""))
        attempts = max(1, int(question.get("max_attempts") or DEFAULT_MAX_ANSWER_ATTEMPTS))
        with self._lock:
            state = self._challenge_state.get(conn.peer_id) or {}
            nonce = ""
            if (state.get("nonce") and not state.get("solved")
                    and state.get("question") == text):
                nonce = str(state["nonce"])
            if not nonce:
                nonce = crypto.new_answer_nonce()
            self._challenge_state[conn.peer_id] = {"query": True, "nonce": nonce,
                                                   "question": text}
            question["attempts_left"] = question.get("attempts_left") or attempts
        conn.send_message({
            "t": "question-challenge",
            "question": text,
            "salt": str(question.get("salt", "")),
            "nonce": nonce,
            "max_attempts": attempts,
        })
        allowed = int(question.get("attempts_left") or attempts)
        self._emit(EventKind.INFO,
                   f"已向 {conn.peer_name} 发出验证问题 (还剩 {allowed} 次机会)",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"question": text, "attempts_left": allowed})

    def _verify_answer(self, conn: Connection, question: Dict[str, Any], proof: str) -> bool:
        """校验对方提交的答案证明; 答错到上限自动封禁。

        校验只用盘上已经保存的派生密钥 (question["hash"]), 不需要答案明文,
        所以程序重启之后照样能验、不会把答对的人误判成答错。
        """
        with self._lock:
            state = self._challenge_state.get(conn.peer_id) or {}
        nonce = str(state.get("nonce", ""))
        key = str(question.get("hash", ""))
        if not nonce or not key:
            return False
        attempts = max(1, int(question.get("max_attempts") or DEFAULT_MAX_ANSWER_ATTEMPTS))
        if int(question.get("attempts_left") or 0) <= 0:
            question["attempts_left"] = attempts

        if crypto.answer_proof_matches_key(key, nonce, proof):
            question["attempts_left"] = attempts
            with self._lock:
                self._challenge_state.pop(conn.peer_id, None)
            self._emit(EventKind.INFO, f"{conn.peer_name} 答对了验证问题 ✓",
                       peer_id=conn.peer_id, name=conn.peer_name)
            return True

        question["attempts_left"] = int(question.get("attempts_left") or attempts) - 1
        self._save_settings()
        if int(question["attempts_left"]) <= 0:
            self.block(conn.peer_id, message="验证问题连续答错")
            self._emit(EventKind.ERROR,
                       f"{conn.peer_name} 连续答错 {attempts} 次验证问题, 已自动封禁",
                       peer_id=conn.peer_id, name=conn.peer_name)
        else:
            self._emit(EventKind.ERROR,
                       f"{conn.peer_name} 答错了验证问题, 还剩 {question['attempts_left']} 次机会",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"attempts_left": question["attempts_left"]})
        return False

    def accept_request(self, peer_id: str) -> bool:
        """同意加好友 -> 已有会话立即升级为加密好友会话。"""
        contact = self.get_contact(peer_id)
        if contact is None:
            return False
        contact.state = ContactState.FRIEND.value
        contact.blocked = False
        contact.blocked_by_peer = False
        contact.updated_at = time.time()
        contact.attempts = 0
        contact.next_try = 0.0
        contact.accept_pending = True          # 等对方回执才清; 在此之前会一直补发
        conn = self._live_connection(contact)      # 记录上的引用丢了也能找回来
        if conn is not None:
            conn.mark_authenticated()
            if not self._send_friend_accept(contact, conn):
                conn = None                 # 发送失败: 退回"没有通道", 立即重连
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO, f"已同意 {contact.name} 的加好友请求, 可以开始加密聊天了")
        self._emit(EventKind.CONNECTED, f"与 {contact.name} 的加密会话已建立",
                   peer_id=contact.peer_id, name=contact.name)
        if conn is None:
            # 双方都已是好友: 用"好友"身份立刻重连, 立刻就能聊
            self._connect_once(contact.peer_id, force=True)
        return True

    def _send_friend_accept(self, contact: Contact,
                            conn: Optional[Connection] = None) -> bool:
        """"我同意加你"这条要送到**并**拿到对方回执才算完。

        以前只是"排进发送队列"就当成功了 —— 会话紧接着被拆掉(断线/去重)的话这条就没了,
        对方一直停在"等待对方同意", 连好友关系都没确认 (用户报过"对面不知道我同意了")。
        """
        conn = conn or self._live_connection(contact)
        if conn is None:
            return False
        if not conn.send_message({"t": "friend-accept", "name": self.name, "ts": time.time()}):
            return False
        contact.accept_pending = True
        contact.next_try = time.time() + max(3.0, ACCEPT_RETRY_SECONDS)
        self._save_contacts()
        return True

    def reject_request(self, peer_id: str, message: str = "") -> bool:
        """拒绝加好友请求 (对方之后还能再发)。已加为好友的则等同删除好友。"""
        contact = self.get_contact(peer_id)
        if contact is None:
            return False
        if contact.state == ContactState.FRIEND.value:
            return self.remove_friend(peer_id)
        conn = self._live_connection(contact)
        if conn is not None:
            conn.send_message({"t": "friend-reject", "reason": message or "对方拒绝", "ts": time.time()})
            # 稍等一下再关, 保证"拒绝"这条消息先发出去 (否则对方只会看到连接断开)
            threading.Timer(0.4, conn.close, args=("已拒绝对方的好友请求",)).start()
        with self._lock:
            self._contacts.pop(contact.peer_id, None)
        self._forget_question_state(contact.peer_id)   # 下次再来还得答题
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO, f"已拒绝 {contact.name} 的加好友请求")
        return True

    def block(self, peer_id: str, message: str = "") -> bool:
        """封禁: 断开连接, 之后对方的请求/连接一律拒收 (可随时解禁)。

        注意**封禁 ≠ 删除好友**: 封禁前是好友的, 解禁后还是好友 (只把好友关系"挂起")。
        以前解禁后会被塞回『新朋友』待处理列表, 看起来就像好友被删了, 而且对方没答题
        也照样出现在列表里 —— 用户报过这个。
        """
        contact = self.get_contact(peer_id)
        if contact is None:
            discovered = self._lan_peers.get(peer_id)
            name = discovered.name if discovered else peer_id
            contact = Contact(peer_id=peer_id, name=name, state=ContactState.BLOCKED.value,
                              blocked=True, message=message)
            with self._lock:
                self._contacts[peer_id] = contact
        else:
            contact.friend_before_block = bool(
                contact.friend_before_block or contact.state == ContactState.FRIEND.value)
            contact.blocked = True
            contact.blocked_by_peer = False
            contact.state = ContactState.BLOCKED.value
            contact.updated_at = time.time()
        contact.unblock_notice = False       # 又封上了: 之前那条"已解禁"就别再补发了
        contact.accept_pending = False       # 封禁状态下也不用再补发"我同意"
        conn = self._live_connection(contact)
        if conn is not None:
            conn.send_message({"t": "blocked", "reason": message or "已被对方封禁", "ts": time.time()})
            threading.Timer(0.4, conn.close, args=("已封禁此人",)).start()
        self._forget_question_state(contact.peer_id)   # 解禁后也要重新答题
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO,
                   f"已封禁 {contact.name}: 对方无法再向你发请求或消息"
                   + (" (解禁后你们仍然是好友)" if contact.friend_before_block else ""))
        return True

    def unblock(self, peer_id: str) -> bool:
        """解除封禁: 恢复原来的关系, 并**主动通知对方**。

        用户要的语义 (报过两次问题):
          * 封禁好友 -> 解禁后**还是好友**, 不能变成"删好友"或凭空冒出一条待处理请求;
          * 对方必须知道"你解禁了", 否则他那边永远显示"对方封禁了你", 以为发不出消息;
          * 解禁后对方要想再加回来, 我设的验证问题照样要答 (以前会因为残留的
            "答对过" 记忆或 REQUEST_IN 状态直接放行)。
        """
        contact = self.get_contact(peer_id)
        if contact is None or not (contact.blocked or contact.blocked_by_peer):
            return False
        was_friend = bool(contact.friend_before_block)
        had_my_block = bool(contact.blocked)           # 是我封的他 (才需要通知他)
        name = contact.name
        contact.blocked = False
        contact.blocked_by_peer = False
        contact.friend_before_block = False
        # 恢复关系: 曾经是好友就回到好友; 否则回到"陌生人", 让对方重新申请。
        # 绝不回 REQUEST_IN —— 那等于替对方发了一条没答过题的请求 (用户报过:
        # "题目还没答, 对面就看到请求了")。
        contact.state = (ContactState.FRIEND.value if was_friend
                         else ContactState.STRANGER.value)
        contact.updated_at = time.time()
        contact.next_try = 0.0
        if had_my_block:
            self._forget_question_state(contact.peer_id)   # 再加回来必须重新答题
            contact.unblock_notice = True                  # 让对方知道"解禁了"
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        if not had_my_block:
            # 只是清掉"对方封了我"的本地标记: 他到底解没解禁, 发一次请求就知道
            self._emit(EventKind.INFO,
                       f"已清除『{name} 封禁了你』的本地标记; "
                       f"再发一次加好友请求就能确认对方是否真的解禁了")
            if was_friend and contact.card is not None:
                threading.Thread(target=self._connect_once, args=(contact.peer_id, True),
                                 name=f"unblock-{name}", daemon=True).start()
            return True
        if was_friend:
            self._emit(EventKind.INFO, f"已解除对 {name} 的封禁, 你们仍然是好友, 正在重新连接…")
        else:
            self._emit(EventKind.INFO,
                       f"已解除对 {name} 的封禁 (对方可以重新发送加好友请求)")
        if was_friend:
            threading.Thread(target=self._connect_once, args=(contact.peer_id, True),
                             name=f"unblock-{name}", daemon=True).start()
            return True
        # 不是好友: 可能根本没有通道。**立刻**试着送一次 (有会话就发, 没有就拨一次),
        # 送不到就先挂着, 由连接管理器每几秒重试一次 —— 不能让用户"非得重新加一次
        # 才知道对方解禁了"(报过)。
        threading.Thread(target=self._deliver_unblock_notice, args=(peer_id, True),
                         name=f"unblock-{name}", daemon=True).start()
        return True

    def _send_unblock_notice(self, contact: Contact) -> bool:
        """把"我解禁了你"告诉对方。

        注意**不**在这里清掉 `unblock_notice`: 消息排进发送队列 ≠ 对方真的收到了,
        会话可能在下一瞬间被拆掉 (去重/断线)。要等对方回一条 `friend-unblocked-ack`
        才算数 —— 这样重试是安全且幂等的 (原来一乐观清除, 通知就永远丢了,
        用户看到的现象就是"对面完全不知道我解禁了")。
        """
        conn = self._live_connection(contact)
        if conn is None:
            return False
        return conn.send_message({"t": "friend-unblocked", "name": self.name, "ts": time.time()})

    def _live_connection(self, contact: Contact) -> Optional[Connection]:
        """取这个人**现在活着**的会话。

        联系人记录上的 `connection` 有可能丢 (例如"删好友"的瞬间对方刚好重连上来,
        新会话登记在 `_connections` 里、而联系人记录随后被删) —— 只看
        `contact.connection` 就会以为"没通道", 于是"已解禁"这类通知永远发不出去
        (用户报过: 解禁了对面完全不知道)。这里顺手把引用补回去。
        """
        conn = contact.connection
        if conn is not None and conn.is_alive:
            return conn
        with self._lock:
            conn = self._connections.get(contact.peer_id)
            if conn is None or not conn.is_alive:
                # 主用会话没了/被拆了 -> 看看"被换下来的"那条是否还活着
                for spare in self._spare_connections.get(contact.peer_id, []):
                    if spare.is_alive:
                        conn = spare
                        break
        if conn is not None and conn.is_alive:
            contact.connection = conn
            return conn
        return None

    def _deliver_unblock_notice(self, peer_id: str, allow_dial: bool = False) -> None:
        """把"已解禁"通知送到对方 (送不到就留着, 下次再试)。"""
        contact = self.get_contact(peer_id)
        if contact is None or not contact.unblock_notice:
            return
        if self._send_unblock_notice(contact):       # 已经有通道: 直接发
            return
        if not allow_dial:
            return
        # 没有通道: 主动拨一次, 连上之后 _register() 会把待发通知补上
        self._dial_raw(contact)

    def _dial_raw(self, contact: Contact) -> Optional[Connection]:
        """只建通道、不改关系 (给"补发通知"这类一次性投递用)。"""
        lock = self._contact_lock(contact.peer_id)
        if not lock.acquire(blocking=False):
            return self._live_connection(contact)
        sock: Optional[socket.socket] = None
        try:
            address = self._resolve_address(contact)
            if address is None:
                return None                          # 对方地址都还不知道, 等发现层
            host, port = address
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect((host, port))
            result = crypto.handshake_initiate(
                sock, self.identity, expected_peer=contact.peer_id,
                purpose="friend" if contact.is_friend else "request",
                timeout=HANDSHAKE_TIMEOUT, rekey_every=self.rekey_every,
            )
            self._register(contact, sock, result, host, initiated_by_us=True)
            return self._live_connection(contact)
        except (OSError, SecurityError, HandshakeRejected, protocol.ProtocolError) as exc:
            if sock is not None:
                _close_quietly(sock)
            # 如果对方说"你还在我的黑名单里", 记下来 (方向: 他封我) 并停止空转
            self._note_blocked_rejection(contact, exc)
            return None
        finally:
            lock.release()

    def remove_friend(self, peer_id: str) -> bool:
        """删除好友: 先告诉对方, 再断开连接。

        只断开不通知的话, 对方那边还留着"好友+能连上", 会一直重连并以为消息发出去了 ——
        实际一条也收不到 (用户报过这个现象)。
        """
        contact = self.get_contact(peer_id)
        if contact is None:
            return False
        conn = self._live_connection(contact)
        if conn is not None:
            conn.send_message({"t": "friend-removed", "name": self.name, "ts": time.time()})
            time.sleep(0.15)                 # 给写线程一点时间把这条通知发出去
            conn.close("已删除好友")
        with self._lock:
            self._contacts.pop(contact.peer_id, None)
        # 关键: 删好友时要清掉"他答对过我的题"的记忆。否则他再加回来会直接放行,
        # 验证问题就形同虚设了 (用户明确要求: 删了再加必须重新答题)。
        self._forget_question_state(contact.peer_id)
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.INFO, f"已删除好友 {contact.name} (已通知对方)")
        return True

    # ==================================================================
    # 发消息
    # ==================================================================
    def send_text(self, text: str, peer_id: Optional[str] = None) -> int:
        """发送文本; peer_id 为空表示群发给所有在线好友。

        每条消息都会记进**本会话的内存聊天记录** (:meth:`conversation`) 并带一个序号:
        对方重启/重连上来时, 由 :meth:`_handle_history_state` 把缺的部分补发回去。
        所以对方**离线时**给他发的消息也不会丢 —— 前提是**我这边**程序还开着
        (记录不落盘, 关掉就没了)。
        """
        text = (text or "").strip()
        if not text:
            return 0
        if peer_id:
            contact = self.get_contact(peer_id)
            if contact is None or not contact.is_friend:
                self._emit(EventKind.ERROR, "没有可发送的对象 (对方不在线或还不是好友)")
                return 0
            if contact.blocked:
                # contact.blocked = "我封了他"。封禁期间不该继续往他那边发东西
                self._emit(EventKind.ERROR, f"{contact.name} 已被你封禁, 先解禁再发")
                return 0
            targets = [contact]
        else:
            # 群发只发给**当前连着**的好友 (离线的人不用一个个排队)
            targets = [c for c in self.friends() if self._live_connection(c) and c.encrypted]
        if not targets:
            self._emit(EventKind.ERROR, "没有可发送的对象 (对方不在线或还不是好友)")
            return 0

        sent = 0
        for contact in targets:
            seq = self._next_out_seq(contact.peer_id)
            entry = self._record_chat(contact.peer_id, own=True, seq=seq, text=text,
                                      ts=time.time())
            conn = self._live_connection(contact)
            delivered = bool(conn is not None and conn.send_chat(text, seq=seq))
            # pending=True: 这条当时**没送出去** (对方离线)。对方上线后补发时, 他那边的
            # 未读提示要按"新消息"算, 所以这个标记要跟着历史一起传过去。
            entry["pending"] = not delivered
            if delivered:
                sent += 1
                contact.last_text = text
                contact.last_ts = time.time()
                self._emit(EventKind.MESSAGE, text, peer_id=contact.peer_id, name=contact.name,
                           data={"direction": "out", "text": text, "seq": seq,
                                 "to_all": peer_id is None})
                continue
            if peer_id:
                # 对方现在不在线: 消息已经进了本会话记录, 他一上线就会补发过去
                contact.last_text = text
                contact.last_ts = time.time()
                self._emit(EventKind.MESSAGE, text, peer_id=contact.peer_id, name=contact.name,
                           data={"direction": "out", "text": text, "seq": seq, "queued": True})
                self._emit(EventKind.INFO,
                           f"{contact.name} 现在不在线: 这条消息先存在本机, "
                           f"他一上线自动补发 (本程序关掉就没了)",
                           peer_id=contact.peer_id, name=contact.name)
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        return sent

    # ==================================================================
    # 会话内聊天记录 (内存里, 关掉程序就没了) + 重连后补发
    # ==================================================================
    def conversation(self, peer_id: str) -> List[Dict[str, Any]]:
        """我和这个人的会话内聊天记录 (只有本会话有效, 关掉程序就没了)。

        每条形如 `{"id", "own", "seq", "text", "ts"}`; own=True 表示是我发的。
        """
        with self._lock:
            return [dict(item) for item in self._chat_log.get(peer_id, [])]

    def _next_out_seq(self, peer_id: str) -> int:
        with self._lock:
            seq = int(self._out_seq.get(peer_id, 0)) + 1
            self._out_seq[peer_id] = seq
            return seq

    def _record_chat(self, peer_id: str, own: bool, seq: int, text: str,
                     ts: float) -> Dict[str, Any]:
        entry = {"id": f"{'me' if own else peer_id}:{int(seq)}", "own": bool(own),
                 "seq": int(seq), "text": text, "ts": float(ts)}
        with self._lock:
            log = self._chat_log.setdefault(peer_id, [])
            log.append(entry)
            if len(log) > HISTORY_LIMIT:
                del log[:-HISTORY_LIMIT]        # 只留最近 HISTORY_LIMIT 条
        return entry

    def history_state(self, peer_id: str) -> Dict[str, int]:
        """"我手上有多少": have_in = 收到他的最高序号, have_out = 我自己发到第几条。"""
        with self._lock:
            return {"have_in": int(self._peer_seq.get(peer_id, 0)),
                    "have_out": int(self._out_seq.get(peer_id, 0))}

    def send_history_state(self, peer_id: str) -> bool:
        """通道建立后主动报一次"我手上有什么", 让对方把缺的补给我。

        对方回一条 `history-replay`(可能分几帧); 两边都会做一次, 所以双向都能补齐。
        """
        contact = self.get_contact(peer_id)
        if contact is None or not contact.is_friend:
            return False
        conn = self._live_connection(contact)
        if conn is None or not conn.authenticated:
            return False
        state = self.history_state(peer_id)
        return conn.send_message({"t": "history-state", "in": state["have_in"],
                                  "out": state["have_out"], "ts": time.time()})

    def _handle_history_state(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方报了他手上有什么 -> 我把我这边他缺的那部分补发给他 (可能分几帧)。"""
        if not conn.authenticated:
            return
        contact = self.get_contact(conn.peer_id)
        if contact is None or not contact.is_friend:
            return
        try:
            have_in = max(0, int(message.get("in", 0) or 0))    # 他手上我的消息到第几条
            have_out = max(0, int(message.get("out", 0) or 0))  # 他手上他自己的消息到第几条
        except (TypeError, ValueError):
            return
        with self._lock:
            mine = [dict(item) for item in self._chat_log.get(conn.peer_id, [])]
        # 我发的: 超过 have_in 的都要补; 他发的: 超过 have_out 的也要补
        # (他重启之后自己那份就没了, 只有我这儿还留着)
        entries: List[Dict[str, Any]] = []
        for item in mine:
            limit = have_in if item["own"] else have_out
            if item["seq"] > limit:
                entries.append(item)
        if not entries:
            return
        entries.sort(key=lambda item: (item["ts"], item["seq"]))
        frames: List[Dict[str, Any]] = []
        for start in range(0, len(entries), HISTORY_BATCH):
            chunk = entries[start:start + HISTORY_BATCH]
            frames.append({
                "t": "history-replay",
                "entries": [{"from": self.peer_id if item["own"] else conn.peer_id,
                             "seq": item["seq"], "ts": item["ts"], "text": item["text"],
                             # 当时没送出去的 (对方离线): 他该按"新消息"弹未读, 不是"历史"
                             **({"pending": True} if item.get("pending") else {})}
                            for item in chunk],
            })
        self._send_history_frames(conn, frames)
        self._emit(EventKind.INFO,
                   f"已把 {len(entries)} 条历史对话补发给 {contact.name} (本会话内有效)")

    @staticmethod
    def _send_history_frames(conn: Connection, frames: List[Dict[str, Any]]) -> None:
        """把补发帧发出去; 多帧时**带随机间隔**慢慢发。

        两个原因:
          * 一次几十帧连发会长时间占住这条连接 (发送队列), 对方正好发消息就会被拖延;
          * 抓包的人看到"一大串密集的大包"就知道在同步历史了 —— 加上随机抖动 (而且每帧
            本身已经被 `pad_message` 补齐成固定档位), 只能看到一段普通的流量。
        放在后台线程里发, 不阻塞当前这套收包/处理流程。
        """
        if len(frames) <= 1:
            for frame in frames:
                conn.send_message(frame)
            return

        def relay() -> None:
            for index, frame in enumerate(frames):
                if index:
                    time.sleep(random.uniform(*HISTORY_JITTER))
                try:
                    conn.send_message(frame)
                except Exception:  # noqa: BLE001 - 发送失败就停, 连接自己会走重连/补发
                    return

        threading.Thread(target=relay, name="history-relay", daemon=True).start()

    def _handle_history_replay(self, conn: Connection, message: Dict[str, Any]) -> None:
        """收到对方补发的历史 -> 记回本机记录并显示出来。

        两种情况区别对待:
          * **历史** (对方以前正常发给我的): 只是把记录取回来, 不重复算未读、不弹通知;
          * **当时没送出去的** (`pending`, 即我离线期间对方发的): 对我是**新消息**,
            正常算未读并在通知里留一笔。
        """
        raw = message.get("entries")
        if not isinstance(raw, list):
            return
        contact = self.get_contact(conn.peer_id)
        name = contact.name if contact else conn.peer_name
        accepted: List[tuple] = []           # (own, seq, ts, text, pending)
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", ""))
            try:
                seq = int(item.get("seq", 0) or 0)
                ts = float(item.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not text or seq <= 0:
                continue
            sender = str(item.get("from", conn.peer_id))
            own = (sender == self.peer_id)
            with self._lock:
                known = (int(self._out_seq.get(conn.peer_id, 0)) if own
                         else int(self._peer_seq.get(conn.peer_id, 0)))
                if seq <= known:
                    continue                     # 我本来就有 (重连补发会重复, 这里去重)
                if own:
                    self._out_seq[conn.peer_id] = seq
                else:
                    self._peer_seq[conn.peer_id] = seq
            accepted.append((own, seq, ts, text, bool(item.get("pending"))))
        if not accepted:
            return
        accepted.sort(key=lambda item: (item[2], item[1]))
        history = [it for it in accepted if not (it[4] and not it[0])]
        fresh = [it for it in accepted if it[4] and not it[0]]
        if history:
            self._emit(EventKind.HISTORY,
                       f"从 {name} 那里取回了 {len(history)} 条历史对话 "
                       f"(本会话内有效, 关掉程序还是会没)",
                       peer_id=conn.peer_id, name=name, data={"count": len(history)})
        for own, seq, ts, text, pending in accepted:
            self._record_chat(conn.peer_id, own=own, seq=seq, text=text, ts=ts)
            is_new = bool(pending and not own)
            if is_new:
                with self._lock:
                    if contact:
                        contact.last_text = text
                        contact.last_ts = ts or time.time()
                        # 对话开着的时候, 补过来的新消息同样不该攒未读
                        if conn.peer_id == self._active_peer:
                            contact.unread = 0
                        else:
                            contact.unread += 1
            self._emit(EventKind.MESSAGE, text, peer_id=conn.peer_id, name=name,
                       data={"direction": "out" if own else "in", "text": text,
                             "seq": seq, "ts": ts, "restored": not is_new,
                             "queued_delivery": is_new})
        if fresh:
            self._emit(EventKind.INFO,
                       f"{name} 在你离线期间发的 {len(fresh)} 条消息已送达 "
                       f"(对方程序一直开着才补得回来)",
                       peer_id=conn.peer_id, name=name,
                       data={"notice": True, "level": "info", "silent": True})
        self._save_contacts()
        self._emit(EventKind.CONTACTS)

    def _message_targets(self, peer_id: Optional[str]) -> List[Contact]:
        if peer_id:
            contact = self.get_contact(peer_id)
            if contact and self._live_connection(contact) and contact.encrypted:
                return [contact]
            return []
        return [c for c in self.friends() if self._live_connection(c) and c.encrypted]

    def set_discoverable(self, value: bool) -> None:
        """隐身开关: 关掉后不广播、不响应单播探测 —— 别人搜不到我。

        别人仍然可以**手填我的 IP:TCP端口** 直接加我 (所以设置里会同时显示当前实际端口)。
        我这边照旧能收到别人的广播, 所以"我能搜别人、别人搜不到我"。
        """
        value = bool(value)
        self.discoverable = value
        with self._lock:
            discovery = self._discovery
        if discovery is not None:
            discovery.set_discoverable(value)
        self._save_settings()
        if value:
            self._emit(EventKind.INFO, "已允许被自动搜索 (广播和单播探测都会回应)")
        else:
            self._emit(EventKind.INFO,
                       f"已开启隐身: 别人在局域网里搜不到你, 只能用『手动添加』填 "
                       f"你的 IP:{self.tcp_port} 来加你")
        self._emit(EventKind.STATUS)

    def set_tcp_port(self, port: int) -> bool:
        """固定"本机聊天端口" (0 = 每次随机)。**重启后生效**。

        为什么需要它: 手动模式 (Tailscale / WireGuard / 跨网段) 要让对方填 `你的IP:端口`,
        而随机端口每次启动都变, 对方填的地址下一次就失效了。固定一个口, 对方填一次就够。
        运行中改端口要先解绑再重绑, 会打断正在进行的连接, 所以这里只存设置、下次启动用。
        """
        try:
            value = int(port)
        except (TypeError, ValueError):
            self._emit(EventKind.ERROR, f"端口得是数字: {port!r}")
            return False
        if value != 0 and not (1024 <= value < 65536):
            self._emit(EventKind.ERROR, f"端口要在 1024~65535 之间 (或填 0 = 自动): {value}")
            return False
        if value and value == self.discovery_port:
            self._emit(EventKind.ERROR, f"{value} 是 UDP 自动发现端口, 换个别的")
            return False
        self.saved_tcp_port = value
        self._save_settings()
        if value:
            self._emit(EventKind.INFO,
                       f"本机端口已固定为 {value}, 重启后生效 (手动添加时让对方填 你的IP:{value})")
        else:
            self._emit(EventKind.INFO, "本机端口改回自动分配 (每次启动随机), 重启后生效")
        self._emit(EventKind.STATUS)
        return True

    def set_download_dir(self, path: str) -> bool:
        """设置"我的接收目录" (统一用这个, 不再区分什么默认目录)。

        目录不存在会自动创建; 已经在跑的会话也会立刻跟着改, 不用重连。
        """
        path = (path or "").strip()
        if not path:
            return False
        path = os.path.abspath(os.path.expanduser(path))
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".lanchat-write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            self._emit(EventKind.ERROR, f"这个目录不能用: {path} ({exc})")
            return False
        self.download_dir = path
        self.download_dir_chosen = True
        self.download_dir_warning = ""
        with self._lock:
            conns = list(self._connections.values())
        for conn in conns:
            conn.set_incoming_dir(path)
        self._save_settings()
        self._emit(EventKind.INFO, f"接收目录已改为: {path}")
        self._emit(EventKind.STATUS)
        return True

    def send_file(self, path: str, peer_id: Optional[str] = None) -> List[OutgoingTransfer]:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            self._emit(EventKind.ERROR, f"文件不存在: {path}")
            return []
        targets = self._message_targets(peer_id)
        if not targets:
            self._emit(EventKind.ERROR, "没有可发送的对象 (对方不在线或还不是好友)")
            return []
        size = os.path.getsize(path)
        transfers = []
        for contact in targets:
            assert contact.connection is not None
            try:
                transfer = contact.connection.send_file(path, on_progress=self._on_send_progress)
            except TransferError as exc:
                self._emit(EventKind.ERROR, f"发送给 {contact.name} 失败: {exc}")
                continue
            transfers.append(transfer)
            self._emit(EventKind.FILE_OFFER, os.path.basename(path), peer_id=contact.peer_id,
                       name=contact.name,
                       data={"transfer_id": transfer.transfer_id, "name": transfer.name,
                             "size": size, "direction": "out"})
        return transfers

    def accept_file(self, transfer_id: Optional[str] = None, peer_id: Optional[str] = None,
                    save_path: Optional[str] = None) -> bool:
        """同意接收一个文件。

        save_path 非空时用「另存为…」指定的位置保存, 否则存到默认接收目录。
        """
        pending = self.pending_offers(peer_id)
        if transfer_id:
            pending = [item for item in pending if item[1] == transfer_id]
        if not pending:
            self._emit(EventKind.ERROR, "没有待确认的文件")
            return False
        ok = False
        for conn, tid, name, size in pending:
            if conn.accept_file(tid, save_path=save_path if len(pending) == 1 else None):
                ok = True
                # 报出**实际**落盘路径 (可能因为重名被自动改名), 不让用户猜存到哪了
                transfer = conn.incoming.get(tid)
                final = transfer.path if transfer is not None else (save_path or self.download_dir)
                self._emit(EventKind.INFO,
                           f"已同意接收 {name} ({human_size(size)}) -> {final}",
                           peer_id=conn.peer_id, name=conn.peer_name,
                           data={"notice": True, "level": "info", "silent": True,
                                 "file_path": final, "direction": "in"})
        return ok

    def reject_file(self, transfer_id: Optional[str] = None, peer_id: Optional[str] = None) -> bool:
        pending = self.pending_offers(peer_id)
        if transfer_id:
            pending = [item for item in pending if item[1] == transfer_id]
        if not pending:
            return False
        ok = False
        for conn, tid, _name, _size in pending:
            if conn.reject_file(tid):
                ok = True
        return ok

    def pending_offers(self, peer_id: Optional[str] = None) -> List[tuple]:
        out: List[tuple] = []
        for conn in self._live_connections():
            if peer_id and conn.peer_id != peer_id:
                continue
            for tid, transfer in conn.pending_offers.items():
                out.append((conn, tid, transfer.name, transfer.size))
        return out

    def _on_send_progress(self, transfer: OutgoingTransfer) -> None:
        self._emit(EventKind.FILE_PROGRESS, data={
            "transfer_id": transfer.transfer_id, "name": transfer.name,
            "sent": transfer.sent, "size": transfer.size,
            "progress": transfer.progress, "direction": "out",
        })

    # ==================================================================
    # 连接管理
    # ==================================================================
    def _live_connections(self) -> List[Connection]:
        with self._lock:
            return [c for c in self._connections.values() if c.is_alive]

    def connect_now(self, peer_id: str) -> bool:
        contact = self.get_contact(peer_id)
        if contact is None or not contact.is_friend:
            return False
        contact.next_try = 0.0
        return self._connect_once(contact.peer_id, force=True)

    def disconnect(self, peer_id: str, reason: str = "主动断开") -> bool:
        contact = self.get_contact(peer_id)
        if contact and contact.connection:
            contact.connection.close(reason)
            return True
        return False

    def _contact_lock(self, peer_id: str) -> threading.Lock:
        with self._lock:
            lock = self._dial_locks.get(peer_id)
            if lock is None:
                lock = threading.Lock()
                self._dial_locks[peer_id] = lock
            return lock

    def _connect_once(self, peer_id: str, force: bool = False) -> bool:
        contact = self.get_contact(peer_id)
        if contact is None or not contact.is_friend or contact.blocked:
            return False
        lock = self._contact_lock(contact.peer_id)
        if not lock.acquire(blocking=False):
            return False
        try:
            if self._stop.is_set() or contact.connected:
                return False
            if contact.dial_state == "connecting" and not force:
                return False
            with self._lock:
                if contact.peer_id in self._connections:
                    return False
            address = self._resolve_address(contact)
            if address is None:
                contact.dial_state = "failed"
                contact.last_error = "还没发现对方的地址"
                contact.next_try = time.time() + self.reconnect_cooldown
                self._emit(EventKind.STATUS)
                return False
            host, port = address
            contact.dial_state = "connecting"
            contact.last_error = ""
            contact.attempts += 1
            self._emit(EventKind.STATUS)

            # 握手用途: 只要"我这边的状态"说明我愿意和对方建立会话 (已是好友, 或我正等着
            # 对方确认我的请求), 就用 friend; 对方收到 friend 后只要能认出我 (我确实是它
            # 认识的人) 就会接受。这样两侧的用途声明永远一致, 不会出现"用途不符"的死循环。
            intent = "friend" if contact.state in (ContactState.FRIEND.value,
                                                   ContactState.REQUEST_IN.value) else "request"
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            try:
                sock.connect((host, port))
                result = crypto.handshake_initiate(
                    sock, self.identity, expected_peer=contact.peer_id, purpose=intent,
                    timeout=HANDSHAKE_TIMEOUT, rekey_every=self.rekey_every,
                )
            except (OSError, SecurityError, HandshakeRejected, protocol.ProtocolError) as exc:
                if not self._note_blocked_rejection(contact, exc):
                    self._dial_failed(contact, str(exc))
                try:
                    sock.close()
                except OSError:
                    pass
                return False
            self._register(contact, sock, result, host, initiated_by_us=True)
            # 双方都已是好友的连接直接升级为可信通道 (授权来自好友名单, 不依赖某条消息)
            live = contact.connection
            if live is not None and live.is_alive and contact.is_friend:
                live.mark_authenticated()
            return True
        finally:
            # 关键: 无论成功/失败/提前返回, 都不能把状态留在 "connecting",
            # 否则连接管理器会认为"正在连"而永远不再重试 (踩过一次: 断线后不再重连)。
            if contact.dial_state == "connecting" and not contact.connected:
                contact.dial_state = "idle"
            lock.release()

    def _dial_failed(self, contact: Contact, reason: str) -> None:
        contact.dial_state = "failed"
        contact.last_error = reason
        delay = RECONNECT_DELAYS[min(contact.attempts, len(RECONNECT_DELAYS) - 1)]
        contact.next_try = time.time() + max(delay, self.reconnect_cooldown)
        self._emit(EventKind.STATUS)
        self._emit(EventKind.INFO, f"暂时连不上 {contact.name}: {reason}")

    def _note_blocked_rejection(self, contact: Contact, exc: BaseException) -> bool:
        """握手被对方以"你被我封禁了"拒绝 -> 本地也标记出来, 别再空转重连。

        以前这条拒绝只是一句普通错误 ("暂时连不上 xxx"), 用户看不出到底是
        对方封了我、还是对方没开程序 —— 也就无从知道对方什么时候解禁。
        """
        if str(getattr(exc, "code", "")) != "blocked":
            return False
        contact.blocked_by_peer = True
        contact.friend_before_block = bool(
            contact.friend_before_block or contact.state == ContactState.FRIEND.value)
        contact.state = ContactState.BLOCKED.value
        contact.dial_state = "idle"
        contact.updated_at = time.time()
        contact.next_try = time.time() + max(30.0, self.reconnect_cooldown)
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.ERROR,
                   f"{contact.name} 仍然把你封禁着 (对方解禁后会通知你, 你也能重新申请)",
                   peer_id=contact.peer_id, name=contact.name,
                   data={"notice": True, "level": "warn"})
        return True

    def _resolve_address(self, contact: Contact) -> Optional[tuple]:
        with self._lock:
            peer = self._lan_peers.get(contact.peer_id)
            fallback = contact.address or contact.manual_address
        if peer and peer.host and peer.port:
            return peer.host, peer.port
        if fallback and ":" in fallback:
            host, _, port = fallback.rpartition(":")
            try:
                return host, int(port)
            except ValueError:
                return None
        return None

    def _register(self, contact: Contact, sock: socket.socket, result: crypto.HandshakeResult,
                  host: str, initiated_by_us: bool) -> None:
        # 手动添加的对端: 这里才拿到真身份 (占位 id -> 真实 peer_id)
        if contact.peer_id != result.card.peer_id:
            contact = self._adopt_real_identity(contact, result.card.peer_id)
        try:
            peer_port = sock.getpeername()[1]
        except OSError:
            peer_port = 0
        conn = Connection(
            sock=sock,
            handshake=result,
            host=host,
            initiated_by_us=initiated_by_us,
            on_message=self._on_session_message,
            on_close=self._on_session_closed,
            incoming_dir=self.download_dir,
            auto_accept_files=self.auto_accept_files,
            rekey_every=self.rekey_every,
        )
        with self._lock:
            old = self._connections.get(conn.peer_id)
            if old is not None and old is not conn and old.is_alive:
                spares = self._spare_connections.setdefault(conn.peer_id, [])
                if old not in spares:
                    spares.append(old)
                del spares[:-2]                     # 最多留两条备用, 别越堆越多
            self._connections[conn.peer_id] = conn
            contact.connection = conn
            contact.dial_state = "idle"
            contact.last_error = ""
            contact.attempts = 0
            contact.next_try = 0.0
            if host and peer_port and initiated_by_us:
                # 只有**我们主动拨出去**时, 对方地址里的端口才是它真正的监听端口;
                # 对方拨进来的连接, getpeername() 给的是它的**临时源端口**, 存下来是错的
                # (手动模式下会一直往一个不存在的端口重拨)。
                contact.address = f"{host}:{peer_port}"
            if result.card.name:
                contact.name = result.card.name
            contact.card = result.card.to_dict()
            if result.verified:
                contact.last_seen = time.time()
        # 双方都已经是好友时, 即使这条是"加好友请求"建立的通道也直接升级为可信通道:
        # 授权来自"对方在我的好友名单里", 而不是来自 friend-accept 这条消息 (可能还没收到)
        if contact.is_friend:
            conn.mark_authenticated()
        conn.start()
        self._save_contacts()
        is_friend_now = contact.is_friend
        self._emit(EventKind.CONNECTED,
                   f"与 {contact.name} 的加密通道已建立 (会话 {conn.session_id}"
                   f"{', 身份已确认' if conn.authenticated else ', 待好友确认'})",
                   peer_id=conn.peer_id, name=contact.name,
                   data={"session": conn.session_id,
                         "direction": "out" if initiated_by_us else "in",
                         "authenticated": conn.authenticated,
                         # 好友上线了: 记一条通知 (不弹窗)
                         "notice": is_friend_now, "level": "info", "silent": True,
                         "online": is_friend_now})
        if contact.unblock_notice:
            # 解禁通知之前没送到 (对方离线) -> 通道一建好就补发
            self._send_unblock_notice(contact)
        if contact.accept_pending:
            # "我同意加你"之前没拿到回执 -> 新通道上再发一次 (幂等)
            self._send_friend_accept(contact, conn)
        if contact.is_friend and conn.authenticated:
            # 好友的通道建好了: 互相报一下"手上有什么", 把对方重启期间缺的对话补过去
            self.send_history_state(contact.peer_id)
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.STATUS)

    def _on_session_closed(self, conn: Connection, reason: Optional[str]) -> None:
        with self._lock:
            was_primary = self._connections.get(conn.peer_id) is conn
            if was_primary:
                self._connections.pop(conn.peer_id, None)
            spares = self._spare_connections.get(conn.peer_id)
            if spares and conn in spares:
                spares.remove(conn)
                if not spares:
                    self._spare_connections.pop(conn.peer_id, None)
            contact = self._contacts.get(conn.peer_id)
            if contact and contact.connection is conn:
                # 断的这条正好是当前用的 -> 还有备用会话就立刻切过去, 别白白断线重连
                keep = next((c for c in self._spare_connections.get(conn.peer_id, [])
                             if c.is_alive), None)
                contact.connection = keep
                contact.dial_state = "idle"
                if was_primary and keep is not None:
                    # 把备用会话提升为主用, 免得连接管理器以为"没连上"又去重拨
                    self._connections[conn.peer_id] = keep
            if contact and contact.state == ContactState.REQUEST_OUT.value:
                contact.next_try = time.time() + max(30.0, self.reconnect_cooldown)
            # 连接断了, 对方出的那道验证问题也没法回答了 (答案必须发过去),
            # 留着只会让"✋ 回答问题"里挂着一道永远答不出的题。
            stale_question = self._incoming_questions.pop(conn.peer_id, None)
        if stale_question:
            self._emit(EventKind.INFO,
                       f"对方的验证问题已失效 (连接断开), 对方上线后重新发一次加好友请求即可",
                       peer_id=conn.peer_id, name=conn.peer_name)
        self._emit(EventKind.DISCONNECTED, f"与 {conn.peer_name} 的连接已断开: {reason or '未知原因'}",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"notice": True, "level": "info", "silent": True,
                         "offline": True})
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.STATUS)

    def _manager_loop(self) -> None:
        while not self._stop.wait(0.5):
            now = time.time()
            with self._lock:
                friends = [c for c in self._contacts.values()
                           if c.is_friend and not c.connected and c.next_try <= now]
                outgoing = [c for c in self._contacts.values()
                            if c.state == ContactState.REQUEST_OUT.value and not c.blocked
                            and c.next_try <= now and not c.connected]
                # "已解禁"通知还没被对方确认收到: 对方只要在线就再送一次
                # (不这样用户就得"重新加一次"才知道对方解禁了 —— 报过)
                notices = [c for c in self._contacts.values()
                           if c.unblock_notice and c.next_try <= now
                           and (c.connected or c.peer_id in self._lan_peers)]
                # "我同意加你"还没拿到回执: 同样补发 (对方那边可能一直停在"等待同意")
                accepts = [c for c in self._contacts.values()
                           if c.accept_pending and c.next_try <= now]
                # 我发的加好友请求对方**一点回应都没有** (连"收到"都没回): 重发一次
                # (会话去重/断线都可能把请求悄悄吃掉, 不能只靠 30 秒后的例行重拨)
                unacked = [c for c in self._contacts.values()
                           if c.state == ContactState.REQUEST_OUT.value and not c.blocked
                           and not c.request_acked and c.request_sent_at
                           and now - c.request_sent_at >= REQUEST_ACK_TIMEOUT]
            if self.auto_connect_friends:
                for contact in friends:
                    # 冷却期内不重拨, 避免和对方的入站连接"撞车"造成反复重连
                    contact.next_try = now + max(2.0, self.reconnect_cooldown)
                    self._connect_once(contact.peer_id)
            for contact in outgoing:
                contact.next_try = now + max(30.0, self.reconnect_cooldown)
                threading.Thread(target=self._dial_and_send_request, args=(contact.peer_id,),
                                 name=f"req-{contact.name}", daemon=True).start()
            for contact in notices:
                contact.next_try = now + max(3.0, UNBLOCK_RETRY_SECONDS)
                threading.Thread(target=self._deliver_unblock_notice,
                                 args=(contact.peer_id, True),
                                 name=f"unblock-{contact.name}", daemon=True).start()
            for contact in accepts:
                if contact.connected:
                    self._send_friend_accept(contact)
                else:
                    contact.next_try = now + max(3.0, ACCEPT_RETRY_SECONDS)
            for contact in unacked:
                contact.request_sent_at = 0.0            # 先清掉, 免得下个 tick 又看一遍
                if contact.request_tries >= REQUEST_MAX_RETRIES:
                    contact.next_try = now + max(30.0, self.reconnect_cooldown)
                    self._emit(EventKind.INFO,
                               f"{contact.name} 一直没回应 (重试了 {contact.request_tries} 次), "
                               f"先不打扰了, 之后会自动再试",
                               peer_id=contact.peer_id, name=contact.name,
                               data={"notice": True, "level": "info", "silent": True})
                    continue
                contact.request_tries += 1
                threading.Thread(target=self._dial_and_send_request, args=(contact.peer_id,),
                                 name=f"req-again-{contact.name}", daemon=True).start()

    # ==================================================================
    # 接受连接
    # ==================================================================
    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_incoming, args=(sock, addr),
                             name=f"hs-{addr[0]}", daemon=True).start()

    def _handle_incoming(self, sock: socket.socket, addr: Any) -> None:
        host = str(addr[0])
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            reader = protocol.FrameReader(sock)
            first = reader.read_frame(timeout=HANDSHAKE_TIMEOUT)
        except (OSError, protocol.ProtocolError):
            _close_quietly(sock)
            return

        # 先看对方是谁、想干什么, 再决定握手策略
        try:
            card = crypto.check_hello(first)
        except SecurityError as exc:
            _reject(sock, f"握手报文非法: {exc}")
            return

        with self._lock:
            contact = self._contacts.get(card.peer_id)
        if contact and contact.blocked:
            # code="blocked": 对方据此能明确显示"你还在被封禁", 而不是一句含糊的"连不上"
            self._emit(EventKind.INFO, f"已拦截被封禁用户 {card.name} 的连接",
                       peer_id=card.peer_id, name=card.name,
                       data={"notice": True, "level": "info", "silent": True})
            _reject(sock, "你已被对方封禁, 无法发送请求或消息", code="blocked")
            return

        is_friend = bool(contact and contact.is_friend)
        # 对方声明 friend: 它愿意和我建立会话。只要我认得这个人 (不是陌生人/被封禁),
        # 就接受 —— 这样"我等着对方确认请求"时也能建立通道, 避免双方用途声明不一致。
        accept_as_friend = is_friend or bool(contact and contact.state in (
            ContactState.REQUEST_IN.value, ContactState.REQUEST_OUT.value))
        if is_friend and contact is not None and contact.card:
            if card.ed_pub != contact.card.get("ed_pub"):
                self._emit(EventKind.ERROR, f"拒绝了一个冒充 {contact.name} 的连接")
                _reject(sock, "你的密钥与好友记录不符")
                return
        try:
            result = crypto.handshake_respond(
                sock, self.identity,
                expected_peer=card.peer_id if accept_as_friend else None,
                purpose="friend" if accept_as_friend else "request",
                timeout=HANDSHAKE_TIMEOUT, first_frame=first,
                # 用途字段只是"意图提示": 两边状态机可能不同步 (例如对方被我解禁后
                # 重新申请), 容忍不一致才不会出现"永久连不上"的死结。
                purpose_strict=False,
                rekey_every=self.rekey_every,
            )
        except (SecurityError, HandshakeRejected, protocol.ProtocolError, OSError) as exc:
            self._emit(EventKind.INFO, f"拒绝了来自 {host} 的连接: {exc}")
            # 握手里失败前可能已经给对方回过一条 error 帧 (crypto 里发的),
            # 所以这里也要"读干净再关", 否则那条帧会被 RST 冲掉
            _half_close(sock)
            return

        # 同一个人可能同时发起两条连接: 按 peer_id 大小对称地保留一条
        with self._lock:
            existing = self._connections.get(result.card.peer_id)
        if existing is not None and existing.is_alive:
            if self.peer_id < result.card.peer_id:
                _close_quietly(sock)   # 保留我主动发起的那条
                return
            existing.close("改用对方发起的新连接")

        if contact is None:
            contact = Contact(peer_id=result.card.peer_id, name=result.card.name,
                              state=ContactState.STRANGER.value)
            with self._lock:
                self._contacts[result.card.peer_id] = contact
        self._register(contact, sock, result, host, initiated_by_us=False)

    # ==================================================================
    # 会话消息
    # ==================================================================
    def _on_session_message(self, conn: Connection, message: Dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "chat":
            if not conn.authenticated:
                return
            text = str(message.get("text", ""))
            try:
                seq = int(message.get("seq", 0) or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                # 记进本会话聊天记录 (对方重启/重连后我要能把它补回去)
                with self._lock:
                    if seq > int(self._peer_seq.get(conn.peer_id, 0)):
                        self._peer_seq[conn.peer_id] = seq
                self._record_chat(conn.peer_id, own=False, seq=seq, text=text,
                                  ts=float(message.get("ts", 0) or time.time()))
            with self._lock:
                contact = self._contacts.get(conn.peer_id)
                if contact:
                    contact.last_text = text
                    contact.last_ts = time.time()
                    # 正开着这个人的对话: 消息直接显示在眼前, 不该再攒未读
                    # (踩过: 双方都开着聊天窗口, 未读还是 +1, 得再点一次才清)
                    if conn.peer_id == self._active_peer:
                        contact.unread = 0
                    else:
                        contact.unread += 1
                    contact.last_seen = time.time()
            self._save_contacts()
            self._emit(EventKind.MESSAGE, text, peer_id=conn.peer_id, name=conn.peer_name,
                       data={"direction": "in", "text": text, "seq": seq})
            self._emit(EventKind.CONTACTS)
            return

        if kind == "history-state":
            # 对方上线/重连后报"他手上有什么" -> 我把缺的补发给他
            self._handle_history_state(conn, message)
            return
        if kind == "history-replay":
            # 对方补发给我的历史对话 (我重启过, 记录空了)
            self._handle_history_replay(conn, message)
            return

        if kind == "friend-request":
            self._handle_friend_request(conn, message)
            return
        if kind == "friend-request-ack":
            self._handle_friend_request_ack(conn, message)
            return
        if kind == "question-challenge":
            self._handle_question_challenge(conn, message)
            return
        if kind == "question-answer":
            self._handle_question_answer(conn, message)
            return
        if kind == "question-result":
            self._handle_question_result(conn, message)
            return
        if kind == "rekey-pref":
            self._handle_rekey_pref(conn, message)
            return
        if kind == "friend-accept":
            self._handle_friend_accept(conn, message)
            return
        if kind == "friend-accept-ack":
            self._handle_friend_accept_ack(conn, message)
            return
        if kind == "friend-reject":
            self._handle_friend_reject(conn, message)
            return
        if kind == "friend-cancel":
            # 对方主动取消了加好友请求: 出的题作废, 联系人退回"陌生人"
            with self._lock:
                contact = self._contacts.get(conn.peer_id)
                self._challenge_state.pop(conn.peer_id, None)
                had_question = bool(contact is not None and contact.state
                                    == ContactState.REQUEST_IN.value)
                if contact is not None and contact.state == ContactState.REQUEST_IN.value:
                    contact.state = ContactState.STRANGER.value
                    contact.updated_at = time.time()
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            self._emit(EventKind.INFO,
                       f"{conn.peer_name} 取消了加好友请求"
                       + (", 那道验证问题不用答了" if had_question else ""),
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"notice": True, "level": "info"})
            conn.close("对方取消了加好友请求")
            return
        if kind == "blocked":
            with self._lock:
                contact = self._contacts.get(conn.peer_id)
                if contact and not contact.blocked:
                    # 注意区分方向: 这是"对方封了我", 不是"我封了对方"。
                    # 所以**只**记 blocked_by_peer, 绝不能把 contact.blocked 也打开 ——
                    # 那是"我封了他"的意思, 会让本机反过来拒收对方的连接 (踩过这个坑:
                    # 被对方封禁过的人, 解禁后自己这边反而把对方挡在门外)。
                    contact.request_acked = True          # 这也是一种回应, 别再重发请求了
                    contact.request_sent_at = 0.0
                    contact.friend_before_block = bool(
                        contact.friend_before_block or contact.state == ContactState.FRIEND.value)
                    contact.blocked_by_peer = True
                    contact.state = ContactState.BLOCKED.value
                    contact.updated_at = time.time()
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            self._emit(EventKind.ERROR, f"{conn.peer_name} 已把你封禁: {message.get('reason', '')}",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"notice": True, "level": "err"})
            return

        if kind == "friend-unblocked":
            self._handle_friend_unblocked(conn, message)
            return
        if kind == "friend-unblocked-ack":
            self._handle_unblock_ack(conn, message)
            return

        if kind == "friend-removed":            # 对方把你从好友里删除了: 本地也要删掉, 否则会一直重连 + 发出去的消息石沉大海
            with self._lock:
                contact = self._contacts.pop(conn.peer_id, None)
            self._forget_question_state(conn.peer_id)   # 他再加回来要重新答题
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            name = contact.name if contact else conn.peer_name
            self._emit(EventKind.ERROR,
                       f"{name} 把你从好友里删除了, 已移除该联系人",
                       peer_id=conn.peer_id, name=name,
                       data={"notice": True, "level": "warn"})
            conn.close("对方已删除好友")
            return

        if kind == "file-offer":
            if not conn.authenticated:
                return
            transfer_id = str(message.get("id", ""))
            name = str(message.get("name", "文件"))
            size = int(message.get("size", 0) or 0)
            auto = self.auto_accept_files
            self._emit(EventKind.FILE_OFFER,
                       f"{conn.peer_name} 发来文件 {name} ({human_size(size)})"
                       + ("，已自动接收" if auto else "，等待确认"),
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"transfer_id": transfer_id, "name": name, "size": size,
                             "direction": "in", "auto_accepted": auto})
            return
        if kind == "file-end":
            transfer = conn.finish_transfer(str(message.get("id", "")))
            if transfer is None:
                return
            ok = bool(transfer.verified)
            self._emit(EventKind.FILE_DONE if ok else EventKind.FILE_FAILED,
                       (f"文件已接收: {transfer.path}" if ok else f"文件校验失败: {transfer.path}"),
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"transfer_id": transfer.transfer_id, "path": transfer.path,
                             "name": transfer.name, "size": transfer.received, "verified": ok,
                             "direction": "in"})
            return
        if kind == "file-accept":
            if conn.start_transfer(str(message.get("id", ""))):
                self._emit(EventKind.INFO, f"{conn.peer_name} 已同意接收, 开始发送…",
                           peer_id=conn.peer_id, name=conn.peer_name)
            return
        if kind == "file-reject":
            transfer_id = str(message.get("id", ""))
            conn.outgoing.pop(transfer_id, None)
            self._emit(EventKind.FILE_FAILED,
                       f"{conn.peer_name} 拒绝接收文件: {message.get('reason', '')}",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"direction": "out", "reason": str(message.get("reason", ""))})
            return
        if kind == "file-abort":
            transfer_id = str(message.get("id", ""))
            conn.outgoing.pop(transfer_id, None)
            pending = conn.incoming.pop(transfer_id, None)
            if pending:
                pending.close()
            self._emit(EventKind.FILE_FAILED,
                       f"文件传输被中止: {message.get('reason', '')}",
                       peer_id=conn.peer_id, name=conn.peer_name, data={"direction": "in"})
            return

    def _handle_friend_request(self, conn: Connection, message: Dict[str, Any]) -> None:
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact and contact.blocked:
            # 明确回一句"你还在我的黑名单里": 保持静默的话, 对方只会一直显示
            # "等待对方确认", 根本不知道是没上线还是被封了 (用户报过)。
            conn.send_message({"t": "blocked", "reason": "对方仍在封禁你", "ts": time.time()})
            self._emit(EventKind.INFO,
                       f"{conn.peer_name} 又发来加好友请求, 但他在你的黑名单里 (未转发给你)",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"notice": True, "level": "info", "silent": True})
            return
        # 之前解禁过、但通知还没送到 (对方离线) -> 现在有通道了, 补发一条
        if contact is not None and contact.unblock_notice:
            self._send_unblock_notice(contact)
        name = str(message.get("name") or conn.peer_name)
        note = str(message.get("msg", ""))
        try:
            ts = int(message.get("ts", time.time()))
        except (TypeError, ValueError):
            ts = int(time.time())
        if not crypto.verify_request_signature(conn.card, self.peer_id, ts, note,
                                              str(message.get("sig", ""))):
            self._emit(EventKind.ERROR, f"收到一条签名无效的好友请求 (来自 {name}), 已忽略")
            return

        # 先回一句"收到了": 对方靠它才知道请求真的送到了 (没有回执时它会重发,
        # 界面也一直停在"等待对方确认" —— 会话刚好被去重拆掉时消息就是这么丢的)。
        conn.send_message({"t": "friend-request-ack", "ts": time.time()})

        if contact is not None and contact.is_friend:
            # 已经是好友了 (对方只是重发/补发了一次请求): 刷新信息就行。
            # 千万别把它退回『待处理请求』—— 重复请求会把好友降级, 界面上看起来
            # 就是"好友突然变成陌生人/要重新同意" (补发机制会把这个坑放大)。
            with self._lock:
                contact.name = name
                contact.card = conn.card.to_dict()
                contact.updated_at = time.time()
                contact.connection = conn
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            # 他还在申请, 说明"我同意"那条他可能没收到 -> 再发一次 (幂等, 对方会回执)
            self._send_friend_accept(contact, conn)
            return

        # 我设了验证问题, 而对方还没答对 -> 先出题, 答对了才进"新朋友"列表。
        # 注意: 题目是按 peer_id 存的, 所以"删了好友再加回来"照样要重新答题。
        question = self._question_for(conn.peer_id)
        if question:
            with self._lock:
                solved = (self._challenge_state.get(conn.peer_id) or {}).get("solved")
            if not solved:
                self._ask_question(conn, question)
                return

        if contact is None:
            contact = Contact(peer_id=conn.peer_id, name=name, card=conn.card.to_dict(),
                              state=ContactState.REQUEST_IN.value, message=note)
            with self._lock:
                self._contacts[conn.peer_id] = contact
            is_new = True
        else:
            is_new = contact.state != ContactState.REQUEST_IN.value
            contact.state = ContactState.REQUEST_IN.value
            contact.message = note
            contact.name = name
            contact.card = conn.card.to_dict()
            contact.updated_at = time.time()
        contact.connection = conn
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        if is_new:
            self._emit(EventKind.INFO,
                       f"收到 {name} 的加好友请求 (指纹 {conn.card.fingerprint})",
                       peer_id=conn.peer_id, name=name,
                       data={"request": True, "fingerprint": conn.card.fingerprint, "note": note})

    def _handle_friend_request_ack(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方回音"你的加好友请求我收到了" -> 不用再重发, 也让界面给句准话。"""
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact is None or contact.request_acked:
            return
        contact.request_acked = True
        contact.request_sent_at = 0.0
        self._save_contacts()
        self._emit(EventKind.INFO,
                   f"{conn.peer_name} 已收到你的加好友请求, 正在等他处理…",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"notice": True, "level": "info", "silent": True})

    def _handle_question_challenge(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方给我出了一道验证问题 (界面据此弹窗让用户作答)。"""
        pending = {
            "question": str(message.get("question", "")),
            "salt": str(message.get("salt", "")),
            "nonce": str(message.get("nonce", "")),
            "max_attempts": int(message.get("max_attempts", DEFAULT_MAX_ANSWER_ATTEMPTS) or 1),
        }
        with self._lock:
            self._incoming_questions[conn.peer_id] = pending
        self._emit(EventKind.INFO,
                   f"{conn.peer_name} 的加好友验证问题: {pending['question']}",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"challenge": True, "question": pending["question"],
                         "nonce": pending["nonce"], "max_attempts": pending["max_attempts"]})
        self._emit(EventKind.QUESTION,
                   f"请回答 {conn.peer_name} 的验证问题: {pending['question']}",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"challenge": True, "question": pending["question"],
                         "nonce": pending["nonce"], "max_attempts": pending["max_attempts"]})

    def _handle_question_answer(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方提交了答案哈希 -> 校验; 答错到上限自动封禁。"""
        question = self._question_for(conn.peer_id)
        if question is None:
            return
        proof = str(message.get("proof", ""))
        passed = self._verify_answer(conn, question, proof)
        # 把判定结果告诉对方: 界面据此提示"答错了, 还剩 N 次机会"并让他直接在弹窗里重试,
        # 而不是提交完就关窗、用户不知道到底对不对。
        conn.send_message({"t": "question-result", "ok": passed,
                           "attempts_left": int(question.get("attempts_left") or 0)})
        if passed:
            with self._lock:
                state = self._challenge_state.setdefault(conn.peer_id, {})
                state["solved"] = True
                contact = self._contacts.get(conn.peer_id)
                if contact is None:
                    contact = Contact(peer_id=conn.peer_id, name=conn.peer_name)
                    self._contacts[conn.peer_id] = contact
            # 校验通过: 把请求正式记下来 (走一遍正常的请求处理)。
            # 注意必须记住这条连接, 否则用户点"同意"时会找不到通道, 只能等下一次重连。
            contact.state = ContactState.REQUEST_IN.value
            contact.card = conn.card.to_dict()
            contact.name = conn.peer_name or contact.name
            contact.updated_at = time.time()
            contact.connection = conn
            self._save_contacts()
            self._emit(EventKind.CONTACTS)
            self._emit(EventKind.INFO,
                       f"{conn.peer_name} 通过了验证问题的校验, 已加入『新朋友』等你处理",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"request": True, "fingerprint": conn.card.fingerprint,
                             "verified_by_question": True})

    def _mark_request_answered(self, conn: Connection) -> None:
        """收到了对方对我加好友请求的任何回应 -> 别再重发了。"""
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact is None or contact.request_acked:
            return
        contact.request_acked = True
        contact.request_sent_at = 0.0

    def _handle_question_result(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方对"我刚提交的答案"的判定: 让界面能当场提示对不对, 答错还能直接重试。"""
        ok = bool(message.get("ok"))
        left = int(message.get("attempts_left", 0) or 0)
        self._mark_request_answered(conn)
        if ok:
            with self._lock:
                self._incoming_questions.pop(conn.peer_id, None)   # 答对了, 这题不用再答
            self._emit(EventKind.QUESTION_RESULT,
                       f"验证问题答对了 ✓ 等待 {conn.peer_name} 处理你的加好友请求",
                       peer_id=conn.peer_id, name=conn.peer_name,
                       data={"ok": True, "attempts_left": left})
            return
        text = (f"答案不对, 还剩 {left} 次机会" if left > 0
                else "答案不对, 机会已用完 (对方已自动封禁)")
        self._emit(EventKind.QUESTION_RESULT, text,
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"ok": False, "attempts_left": left})

    def _handle_friend_unblocked(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方解除了对我的封禁: 清掉标记, 恢复原来的关系 (曾经是好友就还是好友)。

        对方那边也可能是"并未封禁过、只是补发一条解禁通知" —— 那就当成一次
        "他愿意重新联系你" 的提示, 不动我自己的封禁。
        无论认不认识这个人, 都要回一条 ack: 对方靠它才会停止重发 (通知是幂等的)。
        """
        restored = False
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
            if contact is not None and contact.blocked_by_peer:
                contact.blocked_by_peer = False
                restored = bool(contact.friend_before_block)
                contact.friend_before_block = False
                if restored:
                    contact.state = ContactState.FRIEND.value
                elif contact.state == ContactState.BLOCKED.value:
                    contact.state = ContactState.STRANGER.value
                # 注意: 如果他此刻正等着我处理他的加好友请求 (request-out), 就**别动**
                # 状态 —— 改成"陌生人"会把他刚发出的那条请求直接吞掉: 他那边界面回到
                # "还不是好友", 我这边也永远收不到请求 (踩过: 解禁后对方申请没反应)。
                contact.updated_at = time.time()
                contact.next_try = 0.0
        conn.send_message({"t": "friend-unblocked-ack", "ts": time.time()})
        if contact is None:
            return
        if restored:
            conn.mark_authenticated()
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        text = (f"✅ {conn.peer_name} 已解除对你的封禁, 你们仍然是好友"
                if restored else
                f"{conn.peer_name} 已解除对你的封禁, 现在可以重新发送加好友请求")
        self._emit(EventKind.INFO, text, peer_id=conn.peer_id, name=conn.peer_name,
                   data={"notice": True, "level": "info"})
        if restored:
            self._emit(EventKind.CONNECTED, f"与 {contact.name} 的加密会话已恢复",
                       peer_id=contact.peer_id, name=contact.name)

    def _handle_unblock_ack(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方确认收到了"已解禁"通知 -> 这块待发标记才算真的完成。"""
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact is None or not contact.unblock_notice:
            return
        contact.unblock_notice = False
        self._save_contacts()
        self._emit(EventKind.INFO, f"✅ {conn.peer_name} 已收到『已解禁』通知",
                   peer_id=conn.peer_id, name=conn.peer_name,
                   data={"notice": True, "level": "info", "silent": True})

    def _handle_friend_accept(self, conn: Connection, message: Dict[str, Any]) -> None:
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
            if contact is None:
                contact = Contact(peer_id=conn.peer_id, name=conn.peer_name)
                self._contacts[conn.peer_id] = contact
            already = contact.is_friend
            contact.state = ContactState.FRIEND.value
            contact.blocked = False
            contact.blocked_by_peer = False
            contact.friend_before_block = False
            contact.name = str(message.get("name") or conn.peer_name)
            contact.card = conn.card.to_dict()
            contact.updated_at = time.time()
            contact.next_try = 0.0
        conn.mark_authenticated()
        self._mark_request_answered(conn)
        # 回执: 对方"同意"的消息可能重发 (他没收到回执就一直发), 回执让它可以停下来
        conn.send_message({"t": "friend-accept-ack", "ts": time.time()})
        # 刚变成好友, 通道才是"可信"的: 这时才交换聊天记录 (未确认身份前不给)
        self.send_history_state(contact.peer_id)
        self._save_contacts()
        self._emit(EventKind.CONTACTS)
        if already:
            return                       # 重复的同意消息: 不用再弹一次"新好友"
        self._emit(EventKind.CONNECTED,
                   f"✅ {contact.name} 同意了你的好友请求, 现在可以加密聊天了",
                   peer_id=contact.peer_id, name=contact.name,
                   data={"new_friend": True, "fingerprint": conn.card.fingerprint})

    def _handle_friend_accept_ack(self, conn: Connection, message: Dict[str, Any]) -> None:
        """对方确认收到了"我同意加你" -> 不用再补发了。"""
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact is None or not contact.accept_pending:
            return
        contact.accept_pending = False
        self._save_contacts()
        self._emit(EventKind.CONTACTS)

    def _handle_friend_reject(self, conn: Connection, message: Dict[str, Any]) -> None:
        with self._lock:
            contact = self._contacts.get(conn.peer_id)
        if contact is None:
            return
        reason = str(message.get("reason", "对方拒绝"))
        with self._lock:
            self._contacts.pop(conn.peer_id, None)
        self._save_contacts()
        conn.close("对方拒绝了你的好友请求")
        self._emit(EventKind.CONTACTS)
        self._emit(EventKind.ERROR, f"{contact.name} 拒绝了你的好友请求 ({reason})",
                   peer_id=contact.peer_id, name=contact.name,
                   data={"notice": True, "level": "warn"})

    # ==================================================================
    # 已读
    # ==================================================================
    def set_active_peer(self, peer_id: str) -> None:
        """告诉服务"界面现在正开着谁的对话"(空字符串 = 谁都没开)。

        作用是**新消息不再攒未读**: 消息就显示在眼前, 再挂个 (1) 只会让人以为漏了
        (用户报过: 双方都开着聊天窗口, 未读还是 +1, 得再点一次才清)。
        退出程序/切换联系人时记得清掉, 否则没看的消息也会被当成已读。
        """
        peer_id = peer_id or ""
        if peer_id == self._active_peer:
            return
        self._active_peer = peer_id
        if peer_id:
            self.mark_read(peer_id)          # 打开对话时把之前的未读一起清掉

    def mark_read(self, peer_id: str) -> None:
        contact = self.get_contact(peer_id)
        if contact is None or not contact.unread:
            return
        contact.unread = 0
        self._save_contacts()
        self._emit(EventKind.CONTACTS)

    def total_unread(self) -> int:
        return sum(c.unread for c in self.friends())

    # ==================================================================
    # 发现层回调
    # ==================================================================
    def _on_lan_peer_found(self, peer: DiscoveredPeer) -> None:
        with self._lock:
            previous = self._lan_peers.get(peer.peer_id)
            self._lan_peers[peer.peer_id] = peer
            self._discovered_ids.add(peer.peer_id)
        if previous is not None and getattr(previous, "manual", False):
            # 之前只能靠本地登记兜底, 现在广播/单播真的能找到它了 -> 换成发现层记录
            self._emit(EventKind.INFO,
                       f"现在能自动搜索到 {peer.name} 了 (之前只是本地登记)",
                       peer_id=peer.peer_id, name=peer.name)
        self._emit(EventKind.LAN_PEERS)
        self._emit(EventKind.PRESENCE, f"{peer.name} 出现在局域网中", peer_id=peer.peer_id,
                   name=peer.name)

    def _on_lan_peer_lost(self, peer: DiscoveredPeer) -> None:
        with self._lock:
            self._lan_peers.pop(peer.peer_id, None)
            self._discovered_ids.discard(peer.peer_id)
        self._emit(EventKind.LAN_PEERS)
        self._emit(EventKind.PRESENCE, f"{peer.name} 离开了局域网", peer_id=peer.peer_id,
                   name=peer.name)

    def _on_lan_peer_updated(self, old: DiscoveredPeer, new: DiscoveredPeer) -> None:
        with self._lock:
            self._lan_peers[new.peer_id] = new
            self._discovered_ids.add(new.peer_id)
        if old.name != new.name:
            self._emit(EventKind.PRESENCE, f"{old.name} 改名为 {new.name}",
                       peer_id=new.peer_id, name=new.name)
        self._emit(EventKind.LAN_PEERS)

    # ==================================================================
    # 发起好友请求
    # ==================================================================
    def _dial_and_send_request(self, peer_id: str) -> None:
        with self._lock:
            contact = self._contacts.get(peer_id)
        if contact is None or contact.blocked:
            return
        conn = contact.connection if (contact.connection and contact.connection.is_alive) else None
        if conn is None:
            address = self._resolve_address(contact)
            if address is None:
                contact.dial_state = "failed"
                contact.last_error = "对方不在线或还没发现地址"
                self._emit(EventKind.STATUS)
                self._emit(EventKind.ERROR, f"暂时联系不上 {contact.name} (对方不在线?)")
                return
            host, port = address
            contact.dial_state = "connecting"
            self._emit(EventKind.STATUS)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            try:
                sock.connect((host, port))
                result = crypto.handshake_initiate(
                    sock, self.identity, expected_peer=None, purpose="request",
                    timeout=HANDSHAKE_TIMEOUT, rekey_every=self.rekey_every,
                )
            except (OSError, SecurityError, HandshakeRejected, protocol.ProtocolError) as exc:
                contact.dial_state = "failed"
                contact.last_error = str(exc)
                if self._note_blocked_rejection(contact, exc):
                    contact.dial_state = "idle"
                    _close_quietly(sock)
                    return
                contact.next_try = time.time() + max(30.0, self.reconnect_cooldown)
                self._emit(EventKind.STATUS)
                self._emit(EventKind.ERROR, f"向 {contact.name} 发送好友请求失败: {exc}")
                _close_quietly(sock)
                return
            self._register(contact, sock, result, host, initiated_by_us=True)
            contact = self.get_contact(result.card.peer_id) or contact
            conn = contact.connection
            if conn is None:
                return

        # 签名里绑的是**对方的真实 peer_id**: 手动添加时 contact.peer_id 已经从占位 id
        # 换成真身份了 (见 _register -> _adopt_real_identity), 否则对方验签会失败。
        peer_id = contact.peer_id
        ts = int(time.time())
        signature = crypto.b64e(self.identity.sign(
            crypto.request_signature_payload(self.identity.card(), peer_id, ts, contact.message)
        ))
        payload = {
            "t": "friend-request",
            "name": self.name,
            "msg": contact.message,
            "ts": ts,
            "sig": signature,
        }
        sent = conn.send_message(payload)
        contact.next_try = time.time() + max(30.0, self.reconnect_cooldown)
        if not sent:
            contact.next_try = time.time() + 3.0
            self._emit(EventKind.STATUS)
            return
        # 记下"什么时候发的, 还没收到回音": 对方若一直没回应 (请求可能又丢了),
        # 连接管理器会在 REQUEST_ACK_TIMEOUT 秒后重发 (见 _manager_loop)。
        contact.request_acked = False
        contact.request_sent_at = time.time()
        # 双方同时发起连接时, 去重会拆掉其中一条 —— 如果正好拆掉刚发请求的那条, 请求就
        # 悄悄丢了: 对方一直"等待确认", 而重拨要等 30 秒 (用户会觉得"点了没反应")。
        # 这里稍等片刻确认会话还在; 被换掉了就在新会话上**立刻补发**一次。
        time.sleep(0.3)
        with self._lock:
            current = self._connections.get(peer_id)
        if conn.is_alive and (current is None or current is conn):
            return
        if current is not None and current.is_alive and current is not conn:
            current.send_message(payload)
            contact.request_sent_at = time.time()
            self._emit(EventKind.INFO, f"补发了一次加好友请求 ({contact.name} 那边换了会话)")
        contact.next_try = time.time() + 2.0
        self._emit(EventKind.STATUS)

    # ==================================================================
    # 事件 / 持久化
    # ==================================================================
    def _emit(self, kind: EventKind, text: str = "", peer_id: str = "", name: str = "",
              data: Optional[Dict[str, Any]] = None) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(ServiceEvent(kind=kind, text=text, peer_id=peer_id, name=name,
                                       data=data or {}))
        except Exception:  # noqa: BLE001 - UI 回调异常不能拖垮网络线程
            pass

    def _emit_contacts(self) -> None:
        self._emit(EventKind.CONTACTS)

    def _emit_lan_peers(self) -> None:
        self._emit(EventKind.LAN_PEERS)

    def _contacts_path(self) -> str:
        return os.path.join(self.data_dir, CONTACTS_FILE)

    def _settings_path(self) -> str:
        return os.path.join(self.data_dir, SETTINGS_FILE)

    def _load_contacts(self) -> None:
        path = self._contacts_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("contacts", []):
            try:
                contact = Contact(
                    peer_id=str(item["peer_id"]),
                    name=str(item.get("name", "")),
                    card=item.get("card"),
                    state=str(item.get("state", ContactState.FRIEND.value)),
                    blocked=bool(item.get("blocked", False)),
                    blocked_by_peer=bool(item.get("blocked_by_peer", False)),
                    friend_before_block=bool(item.get("friend_before_block", False)),
                    unblock_notice=bool(item.get("unblock_notice", False)),
                    accept_pending=bool(item.get("accept_pending", False)),
                    manual_address=str(item.get("manual_address", "")),
                    message=str(item.get("message", "")),
                    created_at=float(item.get("created_at", time.time())),
                    updated_at=float(item.get("updated_at", time.time())),
                    unread=int(item.get("unread", 0)),
                    last_text=str(item.get("last_text", "")),
                    last_ts=float(item.get("last_ts", 0.0)),
                    question=str(item.get("question", "")),
                    answer_salt=str(item.get("answer_salt", "")),
                    answer_hash=str(item.get("answer_hash", "")),
                    max_attempts=int(item.get("max_attempts", 0)),
                    attempts_left=int(item.get("max_attempts", 0)),
                )
                self._contacts[contact.peer_id] = contact
            except (KeyError, TypeError, ValueError):
                continue

    def _save_contacts(self) -> None:
        with self._lock:
            payload = {"version": PROTOCOL_VERSION,
                       "contacts": [c.to_storage() for c in self._contacts.values()]}
        tmp = self._contacts_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._contacts_path())
        except OSError:
            pass

    def _load_settings(self) -> None:
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.auto_accept_files = bool(data.get("auto_accept_files", self.auto_accept_files))
            self.auto_connect_friends = bool(data.get("auto_connect_friends",
                                                      self.auto_connect_friends))
            self.rekey_every = max(0, int(data.get("rekey_every", self.rekey_every)))
            saved_port = int(data.get("tcp_port", 0) or 0)
            self.saved_tcp_port = saved_port if 0 < saved_port < 65536 else 0
            if saved_port and not self.tcp_port_from_cli:
                # 设置里固定了本机端口 (手动模式要用): 命令行没指定就用它
                self.tcp_port = saved_port if 0 < saved_port < 65536 else 0
            if "discoverable" in data:
                self.discoverable = bool(data.get("discoverable", True))
            saved_dir = str(data.get("download_dir", "") or "")
            chosen = bool(data.get("download_dir_chosen", False))
            if saved_dir and chosen and os.path.isdir(saved_dir):
                self.download_dir = saved_dir      # 用户**自己挑过**的接收目录才优先
                self.download_dir_chosen = True
            elif saved_dir and os.path.isdir(saved_dir):
                # 没有 download_dir_chosen 标记 -> 这份 settings.json 是"还不能自己选目录"
                # 的老版本写的, 里面的路径一定是它自动算出来的 (老版本每次启动都会写进去)。
                # 一律忽略, 用系统下载目录。用户报过两次:"明明说改了默认目录, 升级后还是老路径"
                # —— 根因就是这里把老版本自动写的值当成了"用户设置"。
                note = f"已忽略旧版本自动写入的接收目录: {saved_dir}"
                self.download_dir_warning = (f"{self.download_dir_warning}; {note}"
                                             if self.download_dir_warning else note)
            questions = data.get("questions", {})
            if isinstance(questions, dict):
                for peer_id, item in questions.items():
                    if isinstance(item, dict) and item.get("question") and item.get("hash"):
                        self._peer_questions[str(peer_id)] = {
                            "question": str(item.get("question", "")),
                            "salt": str(item.get("salt", "")),
                            "hash": str(item.get("hash", "")),
                            "max_attempts": max(1, int(item.get(
                                "max_attempts", DEFAULT_MAX_ANSWER_ATTEMPTS))),
                            "attempts_left": max(1, int(item.get(
                                "max_attempts", DEFAULT_MAX_ANSWER_ATTEMPTS))),
                        }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _save_settings(self) -> None:
        with self._lock:
            questions = {peer_id: {"question": item["question"], "salt": item["salt"],
                                   "hash": item["hash"],
                                   "max_attempts": item.get("max_attempts",
                                                            DEFAULT_MAX_ANSWER_ATTEMPTS)}
                         for peer_id, item in self._peer_questions.items()}
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as fh:
                json.dump({"auto_accept_files": self.auto_accept_files,
                           "auto_connect_friends": self.auto_connect_friends,
                           "rekey_every": self.rekey_every,
                           "tcp_port": getattr(self, "saved_tcp_port", 0),
                           "discoverable": bool(getattr(self, "discoverable", True)),
                           "download_dir": self.download_dir,
                           "download_dir_chosen": self.download_dir_chosen,
                           "questions": questions},
                          fh, ensure_ascii=False, indent=2)
        except OSError:
            pass


# ---------------------------------------------------------------------------
def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _half_close(sock: socket.socket, grace: float = 0.5) -> None:
    """半关闭 + 把对端可能已经发来的数据读干净, 最后才真正 close。

    直接 close 时如果还有未读数据, 内核会发 RST, 把**刚写出去但还没被对方读走**的帧
    一起丢掉 —— 对方就只看到"对方提前断开连接", 完全不知道原因 (这就是"被封了"
    和"对方没开程序"看起来一模一样的真凶)。SHUT_WR 之后我们还能读, 读到 EOF/超时再关。
    """
    try:
        sock.shutdown(socket.SHUT_WR)      # "我说完了"
    except OSError:
        _close_quietly(sock)
        return
    deadline = time.time() + max(0.05, grace)
    try:
        sock.settimeout(0.15)
        while time.time() < deadline:
            if not sock.recv(4096):        # 对端读到我们的帧后会关闭 -> EOF
                break
    except OSError:
        pass
    _close_quietly(sock)


def _send_error(sock: socket.socket, reason: str, code: str = "") -> None:
    frame: Dict[str, Any] = {"type": "error", "reason": reason}
    if code:
        frame["code"] = code
    try:
        protocol.send_frame(sock, frame)
    except OSError:
        pass


def _reject(sock: socket.socket, reason: str, code: str = "") -> None:
    """明确告诉对方"我拒绝你, 原因是 X", 然后**干净地**断开。"""
    _send_error(sock, reason, code=code)
    _half_close(sock)


def default_download_dir() -> str:
    """默认接收目录: 系统下载目录。"""
    return os.path.join(os.path.expanduser("~"), "Downloads")


MANUAL_PREFIX = "manual:"          # 手动添加时的占位身份前缀 (握手后会被真实身份取代)

# 中文输入法下打出来的字符和 ASCII 长得几乎一样, 但程序认不出来:
# "：" 是 U+FF1A (中文冒号), 数字也可能是全角的 "５０６０６"。用户手填地址时
# 极容易踩到 (报过: 想填 `IP:端口`, 打成了 `IP：端口`, 结果提示"端口不是数字"),
# 所以输入一律先归一化再用。
_FULLWIDTH_TABLE = str.maketrans({
    "：": ":", "；": ";", "，": ",", "、": ",", "。": ".", "．": ".",
    "／": "/", "＼": "\\", "＠": "@", "－": "-", "＿": "_", "　": " ",
    "（": "(", "）": ")", "【": "[", "】": "]", "“": '"', "”": '"',
    **{chr(0xFF10 + i): str(i) for i in range(10)},      # ０-９
    **{chr(0xFF21 + i): chr(ord("A") + i) for i in range(26)},   # Ａ-Ｚ
})


def normalize_address_input(raw: str) -> str:
    """把用户手打的地址整理成 `IP` / `IP:端口`。

    处理这些"看着一样但不是同一个字符"的情况:
      * 中文冒号 `：`、全角数字 `５０６０６`、全角句点 `．`、全角空格;
      * 前后空格 / 反引号 (有人把 README 里的 `IP:端口` 连壳一起粘进来);
      * `http://1.2.3.4:50606/` 这种带协议前缀和路径的写法;
      * `192.168.1.5 50606` 这种用空格分开的写法。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.translate(_FULLWIDTH_TABLE).strip()
    text = text.strip("`").strip()
    lowered = text.lower()
    for prefix in ("http://", "https://", "tcp://", "lanchat://"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("/")[0].strip()          # 去掉路径部分
    if ":" not in text:
        # "192.168.1.5 50606" -> "192.168.1.5:50606"; 其余空格一律去掉
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            text = f"{parts[0]}:{parts[1]}"
        else:
            text = "".join(parts)
    else:
        text = text.replace(" ", "")
    return text.strip()


def split_manual_address(raw: str) -> Tuple[str, int, str]:
    """解析手动添加里填的地址 -> (host, port, 错误说明)。

    port=0 表示"只填了 IP"(交给单播探测去问端口)。错误说明为空 = 解析成功。
    """
    text = normalize_address_input(raw)
    if not text:
        return "", 0, "地址不能为空"
    host, sep, port_text = text.rpartition(":")
    if not sep:
        return text, 0, ""
    if not host:
        return "", 0, f"地址不完整: {text}"
    try:
        port = int(port_text)
    except ValueError:
        return "", 0, f"端口不是数字: {port_text}"
    if not (0 < port < 65536):
        return "", 0, f"端口超出范围: {port}"
    return host, port, ""


def manual_peer_id(host: str, port: int) -> str:
    return f"{MANUAL_PREFIX}{host}:{int(port)}"


def manual_address(peer_id: str) -> Optional[Tuple[str, int]]:
    """把 `manual:IP:端口` 解析回 (host, port); 不是手动地址就返回 None。"""
    if not peer_id.startswith(MANUAL_PREFIX):
        return None
    host, _, port = peer_id[len(MANUAL_PREFIX):].rpartition(":")
    try:
        value = int(port)
    except ValueError:
        return None
    if not host or not (0 < value < 65536):
        return None
    return host, value


def _prepare_dir(preferred: str) -> tuple:
    """确保接收目录可用; 首选目录不可写时自动降级。

    默认就是系统的**下载目录** (`~/Downloads`), 收到的文件直接在下载里, 不用到处找。
    只读环境 / 没有下载目录时按顺序降级。
    """
    import tempfile

    candidates = [preferred,
                  os.path.join(os.path.expanduser("~"), "Downloads"),
                  os.path.join(os.path.expanduser("~"), "Downloads", DOWNLOAD_DIR_NAME),
                  os.path.join(os.getcwd(), DOWNLOAD_DIR_NAME),
                  os.path.join(tempfile.gettempdir(), DOWNLOAD_DIR_NAME)]
    seen: set = set()
    errors: List[str] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".lanchat-write-test")
            with open(probe, "wb") as fh:
                fh.write(b"ok")
            os.remove(probe)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        return path, (f"首选接收目录不可用 ({errors[0]}), 已改用: {path}" if errors else "")
    return preferred, f"找不到可写目录: {'; '.join(errors)}"
