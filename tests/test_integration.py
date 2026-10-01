"""联调测试: 在本机同时启动多个实例, 模拟"局域网里的多个人"。

覆盖:
  1. 加好友流程: A 发请求 -> B 收到 -> B 同意 -> 双方建立加密会话
  2. 拒绝加好友: 请求方被清掉, 不会建立好友关系
  3. 封禁 / 解禁: 封禁后对方无法发请求、无法建立连接; 解禁后可以重新申请
  4. 多人同时在线: 三人两两加密聊天 + 群发 + 定向
  5. 加密文件传输 (SHA256 校验) + 拒绝接收
  6. 抓包检查: 网络上看不到明文 (消息/文件名/昵称)
  7. 自动重连: 断开后好友通道自动恢复
  8. 身份持久化: 重启后身份与好友关系都还在
  9. 封禁/解禁语义: 好友关系保留 + 通知对方 + 验证问题照答
 10. 手动添加: 完全没有广播 (Tailscale/WireGuard 式组网) 时靠 IP:端口 也能加好友

直接运行:  python tests/test_integration.py
分组运行:  python tests/test_integration.py --only 1,2 --discovery-port 50700
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat.console import configure_console  # noqa: E402

configure_console()  # Windows 中文控制台默认 GBK, 打印 ✅/❌ 会直接崩

from lanchat import protocol  # noqa: E402
from lanchat.service import ChatService, ContactState, EventKind, ServiceEvent  # noqa: E402

DISCOVERY_PORT = 50701
FAILURES: list[str] = []
_log_lock = threading.Lock()


def check(name: str, ok: bool, detail: str = "") -> None:
    with _log_lock:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


class Collected:
    """收集某个实例的事件。"""

    def __init__(self) -> None:
        self.events: list[ServiceEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: ServiceEvent) -> None:
        with self._lock:
            self.events.append(event)

    def texts(self, kind: EventKind | None = None) -> list[str]:
        with self._lock:
            return [e.text for e in self.events if kind is None or e.kind is kind]

    def messages(self) -> list[tuple[str, str]]:
        return [(e.name, e.data.get("text", "")) for e in self.events
                if e.kind is EventKind.MESSAGE]

    def has(self, kind: EventKind, contains: str = "") -> bool:
        with self._lock:
            return any(e.kind is kind and contains in e.text for e in self.events)

    def by_kind(self, kind: EventKind) -> list[ServiceEvent]:
        with self._lock:
            return [e for e in self.events if e.kind is kind]

    def dump(self, tail: int = 12) -> str:
        with self._lock:
            return "\n".join(f"        {e.kind.value}: {e.text[:70]}" for e in self.events[-tail:])


class _Fake:
    """占位对象: 让 `a.get_contact(x) or _Fake()` 这种写法读起来顺一点。"""

    is_friend = False
    encrypted = False
    connected = False
    blocked_by_peer = False
    unblock_notice = False
    state = "?"
    name = "?"


def told_still_blocked(events: "Collected") -> bool:
    """被封的人有没有被**明确**告知"你还被封着"?

    两条路都算:
      * 会话还活着: 对方用一条 `blocked` 消息回绝 (文案 "已把你封禁: ...仍在封禁...");
      * 会话已断: 握手阶段直接回 code=blocked, 本地文案 "仍然把你封禁着"。
    """
    with events._lock:                      # noqa: SLF001 - 测试内部使用
        items = list(events.events)
    return any(e.kind is EventKind.ERROR and "封禁" in e.text
               and ("仍在" in e.text or "仍然" in e.text) for e in items)


def make_service(name: str, tmpdir: str, data_dir: str | None = None, **kwargs) -> tuple:
    collected = Collected()
    data_dir = data_dir or os.path.join(tmpdir, f"data-{name}-{uuid.uuid4().hex[:6]}")
    svc = ChatService(
        on_event=collected,
        name=name,
        data_dir=data_dir,
        download_dir=kwargs.pop("download_dir", os.path.join(data_dir, "dl")),
        enable_discovery=kwargs.pop("enable_discovery", False),
        discovery_port=kwargs.pop("discovery_port", DISCOVERY_PORT),
        tcp_port=kwargs.pop("tcp_port", 0),
        auto_accept_files=kwargs.pop("auto_accept_files", False),
        auto_connect_friends=kwargs.pop("auto_connect_friends", True),
        reconnect_cooldown=kwargs.pop("reconnect_cooldown", 0.5),
        **kwargs,
    )
    svc.start()
    return svc, collected


def pair(a: ChatService, b: ChatService, name_b: str = "对方") -> None:
    """让两个实例互相"看见"对方地址 (代替 UDP 广播发现, 让测试不依赖网卡)。"""
    a.register_manual_peer(b.peer_id, name_b, "127.0.0.1", b.tcp_port)
    b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)


def make_friends(a: ChatService, b: ChatService, timeout: float = 20.0) -> bool:
    """完整流程: A 发加好友请求 -> B 同意 -> 双方成为好友。"""
    a.send_friend_request(b.peer_id)
    if not wait_until(lambda: any(c.peer_id == a.peer_id for c in b.pending_requests()), timeout):
        return False
    b.accept_request(a.peer_id)
    return wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).is_friend
                              and (b.get_contact(a.peer_id) or _Fake()).is_friend, timeout)


def stop_all(*services: ChatService) -> None:
    for svc in services:
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass


def _free_port() -> int:
    """随便找一个当前空闲的端口 (测试用; 拿到之后立刻关掉, 有小概率被别人抢走)。"""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


# ===========================================================================
def test_friend_flow(tmpdir: str) -> None:
    print("① 加好友: 请求 -> 同意 -> 建立加密会话")
    a, ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir)
    pair(a, b, "小红")
    try:
        check("一开始不是好友", not a.friends() and not b.friends())

        a.send_friend_request(b.peer_id)
        got = wait_until(lambda: any(c.peer_id == a.peer_id for c in b.pending_requests()))
        check("对方收到加好友请求", got, ev_b.dump(6))
        request_events = [e for e in ev_b.events if e.data.get("request")]
        check("请求里带上了身份指纹",
              bool(request_events) and "fingerprint" in request_events[0].data,
              str(request_events[0].data) if request_events else "无事件")
        if request_events:
            check("指纹与请求方一致", request_events[0].data["fingerprint"] == a.fingerprint,
                  f"{request_events[0].data['fingerprint']} vs {a.fingerprint}")

        contact_a = a.get_contact(b.peer_id)
        check("请求方状态为『等待验证』",
              contact_a is not None and contact_a.state == ContactState.REQUEST_OUT.value,
              contact_a.state if contact_a else "无")
        check("未成为好友时发不出消息", a.send_text("偷偷发一条", b.peer_id) == 0)

        b.accept_request(a.peer_id)
        ok = wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).encrypted
                                and (b.get_contact(a.peer_id) or _Fake()).encrypted)
        check("双方都建立了加密会话", ok,
              f"A={[(c.name, c.encrypted) for c in a.contacts()]} "
              f"B={[(c.name, c.encrypted) for c in b.contacts()]}")
        if not ok:
            print(ev_a.dump())
            print(ev_b.dump())
            return
        check("同意方收到『加密通道已建立』事件",
              any("加密" in t for t in ev_b.texts(EventKind.CONNECTED)), ev_b.dump(6))

        a.send_text("你好, 我是小明", b.peer_id)
        check("B 收到加密消息",
              wait_until(lambda: any("你好, 我是小明" in t for _n, t in ev_b.messages())),
              str(ev_b.messages()))
        b.send_text("收到! 我是小红", a.peer_id)
        check("A 收到加密回复",
              wait_until(lambda: any("收到! 我是小红" in t for _n, t in ev_a.messages())),
              str(ev_a.messages()))
    finally:
        stop_all(a, b)


def test_reject_flow(tmpdir: str) -> None:
    print("② 拒绝加好友")
    a, ev_a = make_service("小明", tmpdir)
    b, _ev_b = make_service("小红", tmpdir)
    pair(a, b, "小红")
    try:
        a.send_friend_request(b.peer_id)
        check("请求到达对方", wait_until(lambda: len(b.pending_requests()) == 1))
        b.reject_request(a.peer_id)
        check("拒绝后请求方记录被清理",
              wait_until(lambda: not any(c.peer_id == a.peer_id for c in a.contacts())),
              str([c.name for c in a.contacts()]))
        check("请求方收到被拒绝提示",
              wait_until(lambda: any("拒绝" in t for t in ev_a.texts(EventKind.ERROR))),
              str(ev_a.texts(EventKind.ERROR)))
        check("双方都不是好友", not a.friends() and not b.friends())
        check("拒绝后无法发消息", a.send_text("还能发吗", b.peer_id) == 0)
    finally:
        stop_all(a, b)


def test_block_flow(tmpdir: str) -> None:
    print("③ 封禁 / 解禁")
    a, _ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir)
    pair(a, b, "小红")
    try:
        b.block(a.peer_id)
        check("封禁后出现在黑名单", any(c.peer_id == a.peer_id for c in b.blocked_contacts()))

        ev_b.events.clear()
        a.send_friend_request(b.peer_id)
        time.sleep(3.0)
        check("被封禁后收不到好友请求", not b.pending_requests(),
              str([(c.name, c.state) for c in b.contacts()]))
        check("封禁方明确拦截了连接",
              ev_b.has(EventKind.INFO, "拦截") or ev_b.has(EventKind.INFO, "拒绝"), ev_b.dump(8))

        b.unblock(a.peer_id)
        check("解禁后黑名单为空", not b.blocked_contacts())
        # 被封期间请求方的重拨被拒过, next_try 可能还在冷却里; 并行跑测试时更慢,
        # 所以这里等久一点, 别把"慢"当成"坏"
        wait_until(lambda: a.get_contact(b.peer_id) is not None
                           and a.get_contact(b.peer_id).dial_state != "failed", timeout=20.0)
        a.send_friend_request(b.peer_id)
        check("解禁后可以重新收到请求",
              wait_until(lambda: len(b.pending_requests()) >= 1, timeout=45.0), ev_b.dump(8))
        b.accept_request(a.peer_id)
        check("解禁后可以正常成为好友并加密聊天",
              # 解禁 -> 重新握手 -> 升级为好友会话, 多个测试进程并行跑时机器会很忙,
              # 这里的超时给宽一点, 免得把"慢"误判成"坏"
              wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).encrypted
                                and (b.get_contact(a.peer_id) or _Fake()).encrypted, timeout=45.0),
              ev_b.dump(8))
    finally:
        stop_all(a, b)


def test_group_chat(tmpdir: str) -> None:
    print("④ 多人同时在线 (三人两两加密聊天)")
    people: list = []
    for name in ("小明", "小红", "阿强"):
        svc, ev = make_service(name, tmpdir)
        people.append((name, svc, ev))
    try:
        for i in range(3):
            for j in range(i + 1, 3):
                pair(people[i][1], people[j][1], people[j][0])
        for i in range(3):
            for j in range(i + 1, 3):
                _ni, svc_i, _ei = people[i]
                _nj, svc_j, _ej = people[j]
                svc_i.send_friend_request(svc_j.peer_id)
                wait_until(lambda sj=svc_j, si=svc_i: any(c.peer_id == si.peer_id
                                                          for c in sj.pending_requests()), 10)
                svc_j.accept_request(svc_i.peer_id)
        meshed = wait_until(lambda: all(len([c for c in s.friends() if c.encrypted]) >= 2
                                       for _n, s, _e in people), 25)
        check("三人两两都建立了加密会话", meshed,
              ", ".join(f"{n}:{len([c for c in s.friends() if c.encrypted])}人"
                        for n, s, _e in people))
        if not meshed:
            for _n, _s, ev in people:
                print(ev.dump(8))
            return

        for name, svc, _ev in people:
            svc.send_text(f"来自{name}的群发")
        check("群发: 每个人都收到另外两人的消息",
              wait_until(lambda: all(len([t for t in ev.texts() if "的群发" in t]) >= 2
                                     for _n, _s, ev in people), 12),
              "; ".join(f"{n}收到{len([t for t in ev.texts() if '的群发' in t])}条"
                        for n, _s, ev in people))

        _n0, ming, _e0 = people[0]
        _n1, _hong, ev_hong = people[1]
        _n2, qiang, ev_qiang = people[2]
        ming.send_text("只发给阿强", qiang.peer_id)
        got = wait_until(lambda: any("只发给阿强" in t for _n, t in ev_qiang.messages()), 8)
        time.sleep(0.8)
        received_texts = [t for _n, t in ev_hong.messages()]
        leaked = [t for t in received_texts if "只发给阿强" in t]
        check("定向消息只被目标收到", got and not leaked,
              f"目标收到={got}, 旁人收到的消息={received_texts}")
    finally:
        stop_all(*[s for _n, s, _e in people])


def test_file_transfer(tmpdir: str) -> None:
    print("⑤ 加密文件传输 (含 SHA256 校验) 与拒绝接收")
    a, ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir, auto_accept_files=True)
    c, ev_c = make_service("阿强", tmpdir)
    pair(a, b, "小红")
    pair(a, c, "阿强")
    try:
        check("与小红成为好友", make_friends(a, b), ev_b.dump(8))
        payload = os.urandom(1_500_000)
        src = os.path.join(tmpdir, "测试文件.bin")
        with open(src, "wb") as fh:
            fh.write(payload)
        digest = hashlib.sha256(payload).hexdigest()

        a.send_file(src, b.peer_id)
        epochs = {"before": 0, "during": 0, "after": 0}

        def session_epochs() -> tuple:
            contact_a = a.get_contact(b.peer_id)
            contact_b = b.get_contact(a.peer_id)
            conn_a = contact_a.connection if contact_a else None
            conn_b = contact_b.connection if contact_b else None
            return (conn_a.cipher.epoch if conn_a else -1,
                    conn_b.cipher.epoch if conn_b else -1)

        epochs["before"] = session_epochs()
        check("接收方完成接收", wait_until(lambda: ev_b.has(EventKind.FILE_DONE), 60), ev_b.dump(8))
        # 传文件的密钥是"单独开一把, 用完退役": 开始前 / 传输中 / 结束后应该看到 epoch 变化
        deadline = time.time() + 10
        while time.time() < deadline:
            now = session_epochs()
            if now[0] > epochs["before"][0] and now[1] > epochs["before"][1]:
                epochs["after"] = now
                break
            time.sleep(0.2)
        check("文件开始和结束时各换了一次密钥 (文件不用聊天那把密钥)",
              epochs["after"][0] >= epochs["before"][0] + 2
              and epochs["after"][1] >= epochs["before"][1] + 2,
              f"之前 {epochs['before']} -> 之后 {epochs['after']}")
        check("换密钥没有把传输搞断 (没有认证失败)",
              not any("密文认证失败" in e.text or "校验" in e.text and "失败" in e.text
                      for e in ev_a.events + ev_b.events),
              ev_b.dump(6))
        done = [e for e in ev_b.events if e.kind is EventKind.FILE_DONE]
        if done:
            path = str(done[0].data["path"])
            check("文件名正确", os.path.basename(path) == "测试文件.bin", path)
            with open(path, "rb") as fh:
                got = fh.read()
            check("内容一致 (SHA256)", hashlib.sha256(got).hexdigest() == digest, f"{len(got)} 字节")
            check("校验标记 verified=True", bool(done[0].data.get("verified")))
        else:
            check("文件落盘", False, "没有 FILE_DONE 事件")

        # 手动确认 / 拒绝
        check("与阿强成为好友", make_friends(a, c), ev_c.dump(8))
        other = os.path.join(tmpdir, "private.key")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("secret")
        ev_a.events.clear()
        a.send_file(other, c.peer_id)
        check("收到文件请求", wait_until(lambda: ev_c.has(EventKind.FILE_OFFER), 10), ev_c.dump(6))
        check("未确认时不写文件", not os.path.exists(os.path.join(c.download_dir, "private.key")))
        c.reject_file(peer_id=a.peer_id)
        check("发送方收到拒绝通知", wait_until(lambda: ev_a.has(EventKind.FILE_FAILED, "拒绝"), 10),
              ev_a.dump(6))
    finally:
        stop_all(a, b, c)


def test_wire_is_encrypted(tmpdir: str) -> None:
    print("⑥ 抓包检查: 网络上看不到明文")
    a, _ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir, auto_accept_files=True)
    pair(a, b, "小红")
    try:
        check("成为好友", make_friends(a, b), ev_b.dump(8))
        payload = os.urandom(300_000)
        src = os.path.join(tmpdir, "secret-flow.bin")
        with open(src, "wb") as fh:
            fh.write(payload)
        marker = "绝对不会出现在密文里的明文标记-9381"

        captured = bytearray()
        real_send_frame = protocol.send_frame
        lock = threading.Lock()

        def spy_send_frame(sock, obj):  # type: ignore[no-untyped-def]
            data = protocol.encode_frame(obj)
            with lock:
                captured.extend(data)
            sock.sendall(data)

        protocol.send_frame = spy_send_frame  # type: ignore[assignment]
        try:
            a.send_text(marker, b.peer_id)
            a.send_file(src, b.peer_id)
            check("抓包条件下也能完成传输",
                  wait_until(lambda: ev_b.has(EventKind.FILE_DONE), 60), ev_b.dump(6))
            time.sleep(0.8)
        finally:
            protocol.send_frame = real_send_frame  # type: ignore[assignment]

        blob = bytes(captured)
        text = blob.decode("utf-8", errors="ignore")
        check("流量里没有明文消息", marker not in text)
        check("流量里没有明文文件名", "secret-flow.bin" not in text)
        check("确实是密文容器 (type=enc)", b'"type":"enc"' in blob, f"{len(blob)} 字节")
        check("原始二进制分片没有明文出现", base64.b64encode(payload[:8192]) not in blob)

        # 明文帧只允许出现在握手段 (hello/proof/error), 之后全部是密文
        plaintext_types = set()
        for line in blob.split(b"\n"):
            if not line.strip():
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(frame, dict):
                plaintext_types.add(str(frame.get("type")))
        check("非握手帧全部是密文 (没有明文 payload)",
              plaintext_types <= {"enc", "hello", "proof", "error", "ping", "pong"},
              f"出现过的帧类型: {sorted(plaintext_types)}")
        check("握手里只暴露公钥, 不含任何私钥/会话密钥字样",
              "PRIVATE KEY" not in text and "private_bytes" not in text)
    finally:
        stop_all(a, b)


def test_auto_reconnect(tmpdir: str) -> None:
    print("⑦ 断开后自动重连")
    a, ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir)
    pair(a, b, "小红")
    try:
        check("先加为好友", make_friends(a, b), ev_b.dump(8))
        a.disconnect(b.peer_id)
        check("断开后不再加密连接",
              wait_until(lambda: not (a.get_contact(b.peer_id) or _Fake()).encrypted, 8))
        check("自动重连恢复加密会话",
              wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).encrypted
                                and (b.get_contact(a.peer_id) or _Fake()).encrypted, 25),
              ev_a.dump(10))
        ev_b.events.clear()
        a.send_text("重连之后还能聊", b.peer_id)
        check("重连后消息可达",
              wait_until(lambda: any("重连之后还能聊" in t for _n, t in ev_b.messages()), 8))
    finally:
        stop_all(a, b)


def test_identity_and_persistence(tmpdir: str) -> None:
    print("⑧ 身份与好友关系持久化")
    a, _ev_a = make_service("小明", tmpdir)
    first_id, first_fp, data_dir = a.peer_id, a.fingerprint, a.data_dir
    b, ev_b = make_service("小红", tmpdir)
    pair(a, b, "小红")
    check("成为好友", make_friends(a, b))
    b_port = b.tcp_port
    a.stop()
    b.stop()

    # 用同一个数据目录重新构造实例: 身份与好友都应还在
    a2, ev_a2 = make_service("小明", tmpdir, data_dir=data_dir)
    b2, _ev_b2 = make_service("小红", tmpdir, tcp_port=b_port, data_dir=b.data_dir)
    a2.register_manual_peer(b2.peer_id, b2.name, "127.0.0.1", b2.tcp_port)
    b2.register_manual_peer(a2.peer_id, a2.name, "127.0.0.1", a2.tcp_port)
    try:
        check("重启后身份不变", a2.peer_id == first_id, f"{a2.peer_id[:16]}… vs {first_id[:16]}…")
        check("重启后指纹不变", a2.fingerprint == first_fp)
        check("重启后好友关系已恢复", len(a2.friends()) == 1,
              str([(c.name, c.state) for c in a2.contacts()]))
        check("重启后好友自动恢复加密连接",
              wait_until(lambda: (a2.get_contact(b2.peer_id) or _Fake()).encrypted
                                and (b2.get_contact(a2.peer_id) or _Fake()).encrypted, 25),
              ev_a2.dump(8))
    finally:
        stop_all(a2, b2)


def test_block_unblock_semantics(tmpdir: str) -> None:
    """封禁/解禁的完整语义 (用户报的三个坑都在这里):

      * 封禁好友 -> 解禁后**还是好友**, 不能变成"被删好友", 也不能凭空冒出一条
        没答过题的待处理请求 (旧代码解禁后一律塞回 REQUEST_IN, 于是"题目还没答,
        对面就看到请求了");
      * 被封的一方必须**明确**知道"还在被封"和"已经解禁", 不能一直显示等待确认;
      * 解禁后重新申请, 我设的验证问题照样要答对才放行。
    """
    print("⑨ 封禁/解禁语义 (好友保留 + 通知对方 + 题目照答)")
    a, ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir)
    c, ev_c = make_service("小刚", tmpdir)
    pair(a, b, "小红")
    pair(a, c, "小刚")
    try:
        # ---------- 场景 1: 封禁好友, 解禁后仍是好友 ----------
        check("甲乙先成为好友", make_friends(a, b))
        a.block(b.peer_id)
        check("封禁后甲方黑名单里有乙", any(x.peer_id == b.peer_id for x in a.blocked_contacts()))
        check("被封的一方立刻知道 (显示『对方封禁了你』)",
              wait_until(lambda: bool((b.get_contact(a.peer_id) or _Fake()).blocked_by_peer), 10),
              str([(x.name, x.state, x.blocked, x.blocked_by_peer) for x in b.contacts()]))

        ev_b.events.clear()
        b.send_friend_request(a.peer_id)
        check("还在被封时再申请 -> 对方被明确告知, 不是石沉大海",
              wait_until(lambda: told_still_blocked(ev_b), 20), ev_b.dump(6))
        check("这条申请不会进入甲方的『新朋友』", not a.pending_requests(),
              str([(x.name, x.state) for x in a.contacts()]))

        a.unblock(b.peer_id)
        check("解禁后黑名单清空", not a.blocked_contacts())
        contact_b = a.get_contact(b.peer_id)
        check("解禁后甲这边仍然是好友 (没有被删掉)",
              contact_b is not None and contact_b.state == ContactState.FRIEND.value,
              contact_b.state if contact_b else "联系人没了")
        check("解禁后没有凭空出现待处理请求", not a.pending_requests(),
              str([(x.name, x.state) for x in a.pending_requests()]))

        check("乙方收到『已解除封禁』通知",
              wait_until(lambda: ev_b.has(EventKind.INFO, "已解除对你的封禁"), 20), ev_b.dump(6))
        check("乙方本地的『对方封禁了你』标记被清掉",
              wait_until(lambda: not (b.get_contact(a.peer_id) or _Fake()).blocked_by_peer, 15),
              str([(x.name, x.blocked_by_peer) for x in b.contacts()]))
        check("甲乙恢复加密会话 (不用重新加好友)",
              wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).encrypted
                                and (b.get_contact(a.peer_id) or _Fake()).encrypted, 30),
              f"A={[(x.name, x.state, x.encrypted) for x in a.contacts()]} "
              f"B={[(x.name, x.state, x.encrypted) for x in b.contacts()]}")
        ev_b.events.clear()
        a.send_text("解禁之后还能直接聊吗", b.peer_id)
        check("解禁后消息照样送达",
              wait_until(lambda: any("解禁之后还能直接聊吗" in t for _n, t in ev_b.messages()), 20),
              str(ev_b.messages()))

        # ---------- 场景 2: 没答过题就不该出现在『新朋友』里 ----------
        a.set_question(c.peer_id, "1+1 等于几", "2")
        a.block(c.peer_id)
        a.unblock(c.peer_id)
        contact_c = a.get_contact(c.peer_id)
        check("非好友解禁后回到『陌生人』, 不是待处理请求",
              contact_c is not None and contact_c.state == ContactState.STRANGER.value,
              contact_c.state if contact_c else "联系人没了")
        check("解禁本身不会让『新朋友』里冒出没答题的请求", not a.pending_requests(),
              str([(x.name, x.state) for x in a.pending_requests()]))

        ev_c.events.clear()
        c.send_friend_request(a.peer_id)
        check("对方重新申请 -> 先被出题", wait_until(lambda: bool(c.pending_challenges()), 25),
              f"丙待答={c.pending_challenges()} 甲联系人="
              f"{[(x.name, x.state) for x in a.contacts()]}\n{ev_c.dump(40)}")
        check("解禁通知没有把对方刚发出的请求吞掉 (他那边还是『等待验证』)",
              bool((c.get_contact(a.peer_id) or None)
                   and c.get_contact(a.peer_id).state == ContactState.REQUEST_OUT.value),
              str([(x.name, x.state) for x in c.contacts()]))
        check("题目没答对之前, 甲方『新朋友』里看不到这条请求", not a.pending_requests(),
              str([(x.name, x.state) for x in a.pending_requests()]))
        c.answer_challenge(a.peer_id, "2")
        check("答对之后请求才进入『新朋友』",
              wait_until(lambda: any(x.peer_id == c.peer_id for x in a.pending_requests()), 25),
              str([(x.name, x.state) for x in a.contacts()]))
        a.accept_request(c.peer_id)
        check("同上: 答对->同意之后能建立加密会话",
              wait_until(lambda: (a.get_contact(c.peer_id) or _Fake()).encrypted
                                and (c.get_contact(a.peer_id) or _Fake()).encrypted, 30),
              f"A={[(x.name, x.state, x.encrypted) for x in a.contacts()]} "
              f"C={[(x.name, x.state, x.encrypted) for x in c.contacts()]}")

        # ---------- 场景 3: 被封的人能分清"还在被封"和"已解禁" ----------
        a.remove_friend(c.peer_id)
        wait_until(lambda: c.get_contact(a.peer_id) is None
                           or not (c.get_contact(a.peer_id) or _Fake()).is_friend, 15)
        a.block(c.peer_id)
        ev_c.events.clear()
        c.send_friend_request(a.peer_id)
        check("被封的人申请时被告知『仍然把你封禁着』",
              wait_until(lambda: told_still_blocked(ev_c), 25), ev_c.dump(6))
        a.unblock(c.peer_id)
        ev_c.events.clear()
        # 关键: 对方**什么都不做**, 也应该在几秒内收到"已解禁"通知
        check("解禁后主动通知对方 (不用他再申请一次)",
              wait_until(lambda: ev_c.has(EventKind.INFO, "已解除对你的封禁"), 25),
              ev_c.dump(8))
        check("对方本地的『对方封禁了你』标记跟着清掉",
              wait_until(lambda: not bool((c.get_contact(a.peer_id) or None)
                                          and c.get_contact(a.peer_id).blocked_by_peer), 10),
              str([(x.name, x.blocked_by_peer) for x in c.contacts()]))
        check("对方回了 ack, 我这边待发标记才清掉",
              wait_until(lambda: not bool((a.get_contact(c.peer_id) or None)
                                          and a.get_contact(c.peer_id).unblock_notice), 15),
              f"甲={[(x.name, x.unblock_notice) for x in a.contacts()]}")
        ev_c.events.clear()
        c.send_friend_request(a.peer_id)
        check("解禁后重新申请: 仍然要先答题",
              wait_until(lambda: bool(c.pending_challenges()), 25),
              f"丙待答={c.pending_challenges()} 甲事件={ev_a.dump(6)}")
        check("答题之前甲方的『新朋友』里还是没有他", not a.pending_requests(),
              str([(x.name, x.state) for x in a.pending_requests()]))
        if c.pending_challenges():
            c.answer_challenge(a.peer_id, "2")
        arrived = wait_until(lambda: any(x.peer_id == c.peer_id for x in a.pending_requests()), 25)
        check("答对后才进入甲方『新朋友』", arrived,
              str([(x.name, x.state) for x in a.contacts()]))
        if arrived:
            a.accept_request(c.peer_id)
        check("最终成为好友",
              wait_until(lambda: (a.get_contact(c.peer_id) or _Fake()).is_friend
                                and (c.get_contact(a.peer_id) or _Fake()).is_friend, 25),
              f"A={[(x.name, x.state) for x in a.contacts()]} "
              f"C={[(x.name, x.state) for x in c.contacts()]}")
    finally:
        stop_all(a, b, c)


def test_unread_only_when_conversation_closed(tmpdir: str) -> None:
    """对话开着的时候不该攒未读: 消息就显示在眼前, 再挂个 (1) 只会让人以为漏了。

    用户报的: 双方都在对话里, 却还是一直出现未读提示。
    """
    data_a = os.path.join(tmpdir, f"data-u-a-{uuid.uuid4().hex[:6]}")
    data_b = os.path.join(tmpdir, f"data-u-b-{uuid.uuid4().hex[:6]}")
    a, ev_a = make_service("甲", tmpdir, data_dir=data_a)
    b, ev_b = make_service("乙", tmpdir, data_dir=data_b)
    try:
        pair(a, b)
        check("先成为好友", make_friends(a, b), ev_b.dump(6))

        # 未读先攒起来, 再"打开对话" (GUI 打开时会调 set_active_peer)
        a.send_text("先来一条", b.peer_id)
        check("没开对话时消息算未读",
              wait_until(lambda: (b.get_contact(a.peer_id) or _Fake()).unread >= 1, 20),
              str((b.get_contact(a.peer_id) or _Fake()).unread))
        b.set_active_peer(a.peer_id)
        check("打开对话会把已有的未读清掉",
              (b.get_contact(a.peer_id) or _Fake()).unread == 0,
              str((b.get_contact(a.peer_id) or _Fake()).unread))

        a.send_text("你正开着窗口时发的", b.peer_id)
        check("对话开着时收到消息 -> 未读保持 0",
              wait_until(lambda: any(e["text"] == "你正开着窗口时发的"
                                     for e in b.conversation(a.peer_id)), 20)
              and (b.get_contact(a.peer_id) or _Fake()).unread == 0,
              f"unread={(b.get_contact(a.peer_id) or _Fake()).unread}")

        # 切走 (或退出): 新消息应该重新算未读
        b.set_active_peer("")
        a.send_text("你切走之后发的", b.peer_id)
        check("对话关掉后收到消息 -> 未读 +1",
              wait_until(lambda: (b.get_contact(a.peer_id) or _Fake()).unread >= 1, 20),
              str((b.get_contact(a.peer_id) or _Fake()).unread))
        b.set_active_peer(a.peer_id)
        check("再打开对话 -> 未读又清零",
              (b.get_contact(a.peer_id) or _Fake()).unread == 0,
              str((b.get_contact(a.peer_id) or _Fake()).unread))
        b.set_active_peer("")
    finally:
        stop_all(a, b)


def test_history_survives_peer_restart(tmpdir: str) -> None:
    """会话内聊天记录: 一方重启后, 另一方把之前的对话补发回来 (但关掉程序就没了)。

    用户要的: "当对话中一方下线另一方在线, 如果那一方重新上线另一方则传回之前的对话内容
    (但还是关后即焚)"。
    """
    data_a = os.path.join(tmpdir, f"data-h-a-{uuid.uuid4().hex[:6]}")
    data_b = os.path.join(tmpdir, f"data-h-b-{uuid.uuid4().hex[:6]}")
    a, ev_a = make_service("甲", tmpdir, data_dir=data_a)
    b, ev_b = make_service("乙", tmpdir, data_dir=data_b)
    b2 = None
    try:
        pair(a, b)
        check("先成为好友并建立加密会话", make_friends(a, b), ev_b.dump(6))
        a.send_text("第一句: 你好", b.peer_id)
        b.send_text("第二句: 你也好", a.peer_id)
        check("双方各自记着这段对话",
              wait_until(lambda: len(a.conversation(b.peer_id)) >= 2
                                and len(b.conversation(a.peer_id)) >= 2, 20),
              f"A={a.conversation(b.peer_id)} B={b.conversation(a.peer_id)}")

        # 乙关掉程序 -> 再开一个(同一份数据: 身份/好友还在, 但**聊天记录没了**)。
        # 先把未读清零: unread 是落盘的, 不清的话重启后本来就带着之前的未读, 测不准。
        b.mark_read(a.peer_id)
        b.stop()
        b2, ev_b2 = make_service("乙", tmpdir, data_dir=data_b)
        check("重启后聊天记录确实是空的 (不落盘)", b2.conversation(a.peer_id) == [],
              str(b2.conversation(a.peer_id)))
        pair(a, b2)

        # 甲还在线, 它手上那份记录应该补发给乙 (包括乙自己说过的那句)
        got = wait_until(lambda: {e["text"] for e in b2.conversation(a.peer_id)}
                                 >= {"第一句: 你好", "第二句: 你也好"}, 30)
        check("甲把之前的对话补发回给了重启的乙", got,
              f"乙={[(e['own'], e['text']) for e in b2.conversation(a.peer_id)]}")
        check("补发的历史里包含乙自己说过的话 (own=True)",
              any(e["own"] and e["text"] == "第二句: 你也好"
                  for e in b2.conversation(a.peer_id)),
              str([(e["own"], e["text"]) for e in b2.conversation(a.peer_id)]))
        check("补发的是历史, 不算未读",
              (a.get_contact(b2.peer_id) is not None)
              and (b2.get_contact(a.peer_id) or _Fake()).unread == 0,
              str((b2.get_contact(a.peer_id) or _Fake()).unread))
        check("界面上收到的是 restored 标记的消息",
              any(e.data.get("restored") for e in ev_b2.by_kind(EventKind.MESSAGE)),
              str([(e.text, e.data.get("restored")) for e in ev_b2.by_kind(EventKind.MESSAGE)][:4]))

        # 乙又下线: 这次甲发的消息应该先存着, 等乙回来按"新消息"送到
        b2.stop()
        b2 = None
        time.sleep(1.0)
        a.send_text("第三句: 你不在的时候我说的", b.peer_id)
        stored = [e for e in a.conversation(b.peer_id)
                  if e["text"] == "第三句: 你不在的时候我说的"]
        check("对方离线时发的消息也记在本机 (不会直接丢)",
              bool(stored) and stored[0].get("pending") is True, str(stored))

        b2, ev_b2 = make_service("乙", tmpdir, data_dir=data_b)
        pair(a, b2)
        delivered = wait_until(lambda: any(e["text"] == "第三句: 你不在的时候我说的"
                                          for e in b2.conversation(a.peer_id)), 30)
        check("乙一上线, 离线期间那条就补到了", delivered,
              str([e["text"] for e in b2.conversation(a.peer_id)]))
        check("离线期间发的算新消息 (未读 + 不标 restored)",
              (b2.get_contact(a.peer_id) or _Fake()).unread >= 1
              and any(e.text == "第三句: 你不在的时候我说的" and e.data.get("queued_delivery")
                      for e in ev_b2.by_kind(EventKind.MESSAGE)),
              f"unread={(b2.get_contact(a.peer_id) or _Fake()).unread} "
              f"msgs={[(e.text[:12], e.data.get('queued_delivery')) for e in ev_b2.by_kind(EventKind.MESSAGE)][:5]}")
        check("乙能回话 (补发没把会话搞坏)",
              b2.send_text("第四句: 我回来了", a.peer_id) == 1
              and wait_until(lambda: any("第四句: 我回来了" in e["text"]
                                         for e in a.conversation(b.peer_id)), 20),
              str(a.conversation(b.peer_id)))
    finally:
        stop_all(a, b, b2)


def test_manual_add_without_discovery(tmpdir: str) -> None:
    """手动模式: 完全没有发现层 (模拟 Tailscale / WireGuard 这类不跑广播的组网)。

    两个实例**不**互相登记地址、也关掉 UDP 发现, 只靠"手动填 IP:端口"加好友。
    这条链路以前是断的: 占位身份 `manual:IP:端口` 和握手后的真实 peer_id 对不上,
    签名里绑的也是占位 id, 对方验签必然失败。
    """
    print("⑩ 手动添加 (无广播的组网: 只填 IP / IP:端口 + 隐身开关)")
    a, ev_a = make_service("小明", tmpdir)
    b, ev_b = make_service("小红", tmpdir)
    try:
        check("不登记地址时, 谁都发现不了对方",
              not a.lan_peers() and not b.lan_peers(),
              f"A={a.lan_peers()} B={b.lan_peers()}")

        contact = a.add_manual_peer("127.0.0.1", b.tcp_port, name="小红(手动)")
        check("手动添加后列表里出现这个人", contact is not None and len(a.lan_peers()) == 1,
              str([(p["name"], p["address"]) for p in a.lan_peers()]))
        check("手动添加时马上发起请求 -> 对方收到",
              wait_until(lambda: any(c.peer_id == a.peer_id for c in b.pending_requests()), 25),
              ev_b.dump(8))
        check("请求方的联系人已经换成真实身份 (不是 manual: 占位)",
              bool((a.get_contact(b.peer_id) or None))
              and not any(c.peer_id.startswith("manual:") for c in a.contacts()),
              str([(c.name, c.peer_id, c.state) for c in a.contacts()]))
        check("占位条目从发现列表里清掉了",
              not any(p["peer_id"].startswith("manual:") for p in a.lan_peers()),
              str([(p["name"], p["peer_id"]) for p in a.lan_peers()]))
        check("地址记在联系人上 (重启后还能按它重试)",
              ((a.get_contact(b.peer_id) or _Fake()).manual_address or "") != "",
              str((a.get_contact(b.peer_id) or _Fake()).manual_address))

        b.accept_request(a.peer_id)
        check("手动加的人也能建立加密会话",
              wait_until(lambda: (a.get_contact(b.peer_id) or _Fake()).encrypted
                                and (b.get_contact(a.peer_id) or _Fake()).encrypted, 30),
              f"A={[(c.name, c.state, c.encrypted) for c in a.contacts()]} "
              f"B={[(c.name, c.state, c.encrypted) for c in b.contacts()]}")
        check("能正常聊天", a.send_text("手动加的好友也能聊", b.peer_id) == 1,
              str(ev_a.texts(EventKind.ERROR)))
        check("对方收到消息",
              wait_until(lambda: any("手动加的好友也能聊" in t for _n, t in ev_b.messages()), 20),
              str(ev_b.messages()))

        # ---------- 单播探测: 只填 IP, 自动问出对方的 TCP 端口 ----------
        # 双方用**不同的发现端口**: 广播互相看不到, 只有"单播探测"这一条路能走通
        disc_x, disc_y = _free_port(), _free_port()
        data_x = os.path.join(tmpdir, f"data-x-{uuid.uuid4().hex[:6]}")
        data_y = os.path.join(tmpdir, f"data-y-{uuid.uuid4().hex[:6]}")
        x, _ev_x = make_service("探测方", tmpdir, data_dir=data_x, enable_discovery=True,
                                discovery_port=disc_x)
        y, ev_y = make_service("被探方", tmpdir, data_dir=data_y, enable_discovery=True,
                               discovery_port=disc_y)
        try:
            check("两边发现端口不同 -> 光靠广播互相看不到",
                  not x.lan_peers() and not y.lan_peers(),
                  f"X={x.lan_peers()} Y={y.lan_peers()}")
            probed = x.add_manual_peer("127.0.0.1", 0, name="被探方",
                                       probe_port=disc_y)   # ← 不填 TCP 端口, 只探
            check("只填 IP 也探测到了对方 (自动拿到它的 TCP 端口)",
                  probed is not None and probed.peer_id == y.peer_id,
                  f"探测结果={probed and (probed.name, probed.peer_id, probed.state)} "
                  f"乙={y.peer_id} 甲列表={x.lan_peers()}")
            check("探测到的是真实身份, 不是 manual: 占位",
                  bool(probed and not probed.peer_id.startswith("manual:")),
                  probed.peer_id if probed else "没探测到")
            check("探测之后请求直接送达",
                  wait_until(lambda: any(c.peer_id == x.peer_id for c in y.pending_requests()), 25),
                  ev_y.dump(6))
            y.accept_request(x.peer_id)
            check("探测加的好友也能建立加密会话",
                  wait_until(lambda: (x.get_contact(y.peer_id) or _Fake()).encrypted
                                    and (y.get_contact(x.peer_id) or _Fake()).encrypted, 30),
                  f"X={[(c.name, c.state, c.encrypted) for c in x.contacts()]} "
                  f"Y={[(c.name, c.state, c.encrypted) for c in y.contacts()]}")
        finally:
            stop_all(x, y)

        # ---------- 隐身: 不广播也不回应探测 -> 只能手填 IP:TCP端口 ----------
        disc_p, disc_q = _free_port(), _free_port()
        data_p = os.path.join(tmpdir, f"data-p-{uuid.uuid4().hex[:6]}")
        data_q = os.path.join(tmpdir, f"data-q-{uuid.uuid4().hex[:6]}")
        p, _ev_p = make_service("甲方", tmpdir, data_dir=data_p, enable_discovery=True,
                                discovery_port=disc_p)
        q, ev_q = make_service("隐身方", tmpdir, data_dir=data_q, enable_discovery=True,
                               discovery_port=disc_q)
        try:
            q.set_discoverable(False)
            check("隐身开关能存下来", q.discoverable is False)
            check("隐身时只填 IP 探测不到 (会明确失败)",
                  p.add_manual_peer("127.0.0.1", 0) is None, str(p.lan_peers()))
            direct = p.add_manual_peer("127.0.0.1", q.tcp_port, name="隐身方")
            check("但手填 IP:TCP端口 仍然能加上",
                  direct is not None
                  and wait_until(lambda: any(c.peer_id == p.peer_id for c in q.pending_requests()),
                                 25),
                  ev_q.dump(6))
            q.accept_request(p.peer_id)
            check("隐身模式下照样能加密聊天",
                  wait_until(lambda: (p.get_contact(q.peer_id) or _Fake()).encrypted
                                    and (q.get_contact(p.peer_id) or _Fake()).encrypted, 30),
                  f"P={[(c.name, c.state, c.encrypted) for c in p.contacts()]} "
                  f"Q={[(c.name, c.state, c.encrypted) for c in q.contacts()]}")
        finally:
            stop_all(p, q)

        # ---------- 隐身也真的停掉广播 (两个实例同一个发现端口, 走真广播) ----------
        disc_same = _free_port()
        data_m = os.path.join(tmpdir, f"data-m-{uuid.uuid4().hex[:6]}")
        data_n = os.path.join(tmpdir, f"data-n-{uuid.uuid4().hex[:6]}")
        m, _ev_m = make_service("广播甲", tmpdir, data_dir=data_m, enable_discovery=True,
                                discovery_port=disc_same)
        n, _ev_n = make_service("广播乙", tmpdir, data_dir=data_n, enable_discovery=True,
                                discovery_port=disc_same)
        try:
            check("同一个发现端口下, 双方靠广播互相看得见",
                  wait_until(lambda: any(x["peer_id"] == n.peer_id for x in m.lan_peers())
                                    and any(x["peer_id"] == m.peer_id for x in n.lan_peers()), 25),
                  f"M={[x['name'] for x in m.lan_peers()]} N={[x['name'] for x in n.lan_peers()]}")
            seen_before = next((x["last_seen"] for x in m.lan_peers()
                                if x["peer_id"] == n.peer_id), 0.0)
            n.set_discoverable(False)
            time.sleep(4.5)          # 广播间隔 3 秒: 没隐身的话这里必然刷新一次
            seen_after = next((x["last_seen"] for x in m.lan_peers()
                               if x["peer_id"] == n.peer_id), 0.0)
            check("隐身之后对方那边不再刷新我的 beacon (真的不广播了)",
                  seen_before > 0 and seen_after == seen_before,
                  f"last_seen {seen_before:.2f} -> {seen_after:.2f}")
        finally:
            stop_all(m, n)

        # 固定端口: 就是"⚙ 设置 → 本机端口"填一个数 (对方不用碰命令行)
        free = _free_port()
        data_c = os.path.join(tmpdir, f"data-c-{uuid.uuid4().hex[:6]}")
        c, _ev_c = make_service("小刚", tmpdir, data_dir=data_c)
        check("『设置 → 本机端口』能存下来", c.set_tcp_port(free))
        c.stop()
        c2, ev_c2 = make_service("小刚", tmpdir, data_dir=data_c)
        try:
            check("重启后真的绑在固定端口上", c2.tcp_port == free, f"{c2.tcp_port} vs {free}")
            check("用这个端口手动添加 -> 对方收到请求",
                  a.add_manual_peer("127.0.0.1", free) is not None
                  and wait_until(lambda: any(x.peer_id == a.peer_id for x in c2.pending_requests()),
                                 25),
                  ev_c2.dump(6))
        finally:
            stop_all(c2)

        # 重启后: 联系人还在, 地址还在, 自动重连还能用 (没有任何广播参与)
        b.stop()
        data_a = a.data_dir
        a.stop()
        again, _ev = make_service("小明", tmpdir, data_dir=data_a)
        try:
            reloaded = again.get_contact(b.peer_id)
            check("重启后手动加的好友还在", reloaded is not None and reloaded.is_friend,
                  str([(c.name, c.state) for c in again.contacts()]))
            check("重启后地址也没丢 (不依赖广播)",
                  bool(reloaded and reloaded.manual_address)
                  and again._resolve_address(reloaded) == ("127.0.0.1", b.tcp_port),
                  f"{reloaded.manual_address if reloaded else '无'} / "
                  f"{again._resolve_address(reloaded) if reloaded else '无'}")
        finally:
            stop_all(again)
    finally:
        stop_all(a, b)


# ===========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="局域网聊天工具联调测试")
    parser.add_argument("--only", default="", help="只运行指定编号, 如 1,2")
    parser.add_argument("--discovery-port", type=int, default=50701)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    global DISCOVERY_PORT
    DISCOVERY_PORT = args.discovery_port

    tests = [
        ("1 加好友流程", test_friend_flow),
        ("2 拒绝加好友", test_reject_flow),
        ("3 封禁/解禁", test_block_flow),
        ("4 多人群聊", test_group_chat),
        ("5 文件传输", test_file_transfer),
        ("6 抓包检查", test_wire_is_encrypted),
        ("7 好友重连", test_auto_reconnect),
        ("8 身份持久化", test_identity_and_persistence),
        ("9 封禁/解禁语义", test_block_unblock_semantics),
        ("10 手动添加(无广播)", test_manual_add_without_discovery),
        ("11 会话记录补发", test_history_survives_peer_restart),
        ("12 未读只在对话关闭时算", test_unread_only_when_conversation_closed),
    ]
    wanted = {int(x) for x in args.only.split(",") if x.strip().isdigit()} if args.only else None
    selected = [t for i, t in enumerate(tests, start=1) if wanted is None or i in wanted]

    print("=" * 60)
    print(f"联调测试 (本机多实例)  发现端口={DISCOVERY_PORT}")
    print("=" * 60)

    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-tests")
    tmpdir = os.path.join(base, uuid.uuid4().hex[:8])
    os.makedirs(tmpdir, exist_ok=True)
    try:
        for title, fn in selected:
            print(f"--- {title} ---")
            fn(tmpdir)
            print()
    finally:
        if args.keep_temp:
            print(f"临时目录保留在: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败:")
        for item in FAILURES:
            print(f"   - {item}")
        return 1
    print(f"✅ 测试完成, 全部通过 ({len(selected)} 组)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
