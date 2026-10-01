"""单元测试: 身份密钥、HTTPS 式握手、会话加密、防重放/防篡改。

直接运行:  python tests/test_units.py
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat.console import configure_console  # noqa: E402

configure_console()  # Windows 中文控制台默认 GBK, 打印 ✅ 会直接崩

from lanchat import crypto, protocol  # noqa: E402
from lanchat.crypto import (  # noqa: E402
    HandshakeRejected, SecurityError, SessionState, handshake_initiate, handshake_respond,
)
from lanchat.identity import (  # noqa: E402
    IdentityCard, LocalIdentity, derive_peer_id, fingerprint_of,
)

FAILURES: list[str] = []
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-units")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def expect_error(name: str, fn, *errors) -> None:
    """断言 fn() 抛异常 (可选: 限定异常类型)。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        if errors and not isinstance(exc, errors):
            check(name, False, f"异常类型不对: {type(exc).__name__}: {exc}")
            return
        check(name, True, f"{type(exc).__name__}: {exc}")
        return
    check(name, False, "居然没有报错")


# 这些异常都表示"这次握手被拒绝", 预期之内
_EXPECTED_HANDSHAKE_ERRORS = (
    SecurityError, HandshakeRejected, crypto.DecryptError, protocol.ProtocolError, OSError,
)


def _pair() -> tuple:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return server, client


def _handshake_pair(a: LocalIdentity, b: LocalIdentity, expect_a=None, expect_b=None,
                    purpose="friend", responder_purpose=None):
    """跑一次完整握手, 返回 (A 侧结果, B 侧结果, 错误)。

    purpose 是发起方声明的用途, responder_purpose 是接受方期望的用途 (默认与之一致)。
    """
    if responder_purpose is None:
        responder_purpose = purpose
    server, client = _pair()
    box: dict = {}

    def responder() -> None:
        try:
            box["b"] = handshake_respond(server, b, expected_peer=expect_b,
                                         purpose=responder_purpose, timeout=5)
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, _EXPECTED_HANDSHAKE_ERRORS):
                raise
            box["b_error"] = exc

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()
    try:
        box["a"] = handshake_initiate(client, a, expected_peer=expect_a, purpose=purpose, timeout=5)
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, _EXPECTED_HANDSHAKE_ERRORS):
            raise
        box["a_error"] = exc
    thread.join(timeout=5)
    for sock in (server, client):
        try:
            sock.close()
        except OSError:
            pass
    return box


# ---------------------------------------------------------------------------
def test_identity() -> None:
    print("身份密钥与指纹")
    a = LocalIdentity.generate("小明")
    card = a.card()
    check("身份卡可以自校验", IdentityCard.from_dict(card.to_dict()) is not None)
    check("peer_id 由公钥推导", derive_peer_id(base64.b64decode(card.ed_pub)) == a.peer_id)
    check("指纹格式可读", fingerprint_of(a.peer_id) == card.fingerprint and "-" in card.fingerprint,
          card.fingerprint)

    # 篡改身份卡必须被发现
    bad = card.to_dict()
    bad["name"] = "小红"
    expect_error("改名字后身份卡签名失效", lambda: IdentityCard.from_dict(bad), ValueError)

    bad2 = card.to_dict()
    other = LocalIdentity.generate("冒充者")
    bad2["ed_pub"] = other.card().ed_pub
    expect_error("换公钥后身份卡被拒 (peer_id 对不上)", lambda: IdentityCard.from_dict(bad2), ValueError)

    # 落盘 / 复用
    path = os.path.join(TMP, "identity.json")
    a.save(path)
    loaded, created = LocalIdentity.load_or_create(path, "别的名字")
    check("重启后复用同一身份", (not created) and loaded.peer_id == a.peer_id)
    check("署名依然有效", loaded.card().fingerprint == card.fingerprint)

    # 每次生成的身份都不一样
    check("两个身份互不相同", LocalIdentity.generate("x").peer_id != LocalIdentity.generate("x").peer_id)


def test_handshake_ok() -> None:
    print("握手 (双方都是好友)")
    a, b = LocalIdentity.generate("小明"), LocalIdentity.generate("小红")
    box = _handshake_pair(a, b, expect_a=b.peer_id, expect_b=a.peer_id)
    check("发起方握手成功", "a" in box, str(box.get("a_error", "")))
    check("接受方握手成功", "b" in box, str(box.get("b_error", "")))
    if "a" not in box or "b" not in box:
        return
    ra, rb = box["a"], box["b"]
    check("双方互相看到正确身份", ra.card.peer_id == b.peer_id and rb.card.peer_id == a.peer_id)
    check("会话被标记为已认证好友",
          ra.state is SessionState.AUTHENTICATED and rb.state is SessionState.AUTHENTICATED)
    check("会话号一致", ra.session_id == rb.session_id)

    token = ra.cipher.encrypt_message({"t": "chat", "text": "你好"})
    check("A 发的密文 B 能解", rb.cipher.decrypt_message(token).get("text") == "你好")
    token2 = rb.cipher.encrypt_message({"t": "chat", "text": "你也好"})
    check("B 发的密文 A 能解", ra.cipher.decrypt_message(token2).get("text") == "你也好")

    raw = os.urandom(5000)
    check("文件分片密钥互通",
          ra.cipher.decrypt_chunk(rb.cipher.encrypt_chunk(raw)) == raw)

    # 两次握手的密钥必须不同 (临时密钥 + 随机数)
    box2 = _handshake_pair(a, b, expect_a=b.peer_id, expect_b=a.peer_id)
    if "a" in box2:
        check("两次握手得到不同会话密钥",
              box2["a"].cipher.encrypt_message({"x": 1})["c"] != token["c"])
        check("两次握手的会话号不同", box2["a"].session_id != ra.session_id)
    else:
        check("第二次握手成功", False, str(box2.get("a_error")))


def test_handshake_identity_mismatch() -> None:
    print("握手 (身份不符 / 未确认好友)")
    a, b = LocalIdentity.generate("小明"), LocalIdentity.generate("小红")
    impostor = LocalIdentity.generate("小红")

    # A 以为自己在连 B, 实际对面是冒充者 -> 必须失败
    box = _handshake_pair(a, impostor, expect_a=b.peer_id, expect_b=a.peer_id)
    check("对方身份不符时被拒绝", "a" not in box and "a_error" in box,
          str(box.get("a_error", ""))[:80])

    # 双方都没有预期身份 -> 允许建立, 但标记为 PENDING (只能谈加好友)
    box2 = _handshake_pair(a, b)
    if "a" in box2 and "b" in box2:
        check("陌生连接标记为待确认", box2["a"].state is SessionState.PENDING
              and box2["b"].state is SessionState.PENDING)
        check("待确认会话不算已认证", box2["a"].verified is False)
    else:
        check("陌生连接可以建立", False, str(box2.get("a_error", box2.get("b_error"))))

    # 用途不符: A 说是"加好友请求", 但 B 只接受"好友连接" -> 双方都发现无法继续
    box3 = _handshake_pair(a, b, purpose="request", responder_purpose="friend", expect_b=a.peer_id)
    check("用途不符时握手被拒绝, 不会半开",
          "b" not in box3 and "a" not in box3,
          f"b_error={box3.get('b_error')} a_error={box3.get('a_error')}")


def test_tamper_detection() -> None:
    print("防篡改 / 防重放")
    a, b = LocalIdentity.generate("小明"), LocalIdentity.generate("小红")
    box = _handshake_pair(a, b, expect_a=b.peer_id, expect_b=a.peer_id)
    if "a" not in box:
        check("握手成功 (前置条件)", False, str(box.get("a_error")))
        return
    ra, rb = box["a"], box["b"]

    # 密文被改一个字符必须解密失败
    token = ra.cipher.encrypt_message({"t": "chat", "text": "原始内容"})
    body = list(token["c"])
    body[len(body) // 2] = "A" if body[len(body) // 2] != "A" else "B"
    expect_error("篡改密文被拒绝",
                 lambda: rb.cipher.decrypt_message({"n": token["n"], "c": "".join(body)}),
                 crypto.DecryptError)

    # 重放同一条报文必须被拒绝
    ok_token = ra.cipher.encrypt_message({"t": "chat", "text": "只发一次"})
    rb.cipher.decrypt_message(ok_token)
    expect_error("重放同一密文被拒绝", lambda: rb.cipher.decrypt_message(ok_token),
                 crypto.DecryptError)

    # 换一条会话的密钥解不开 (AAD 绑定双方身份)
    c = LocalIdentity.generate("第三方")
    box2 = _handshake_pair(a, c, expect_a=c.peer_id, expect_b=a.peer_id)
    if "a" in box2:
        expect_error("跨会话密文无法解密",
                     lambda: box2["a"].cipher.decrypt_message(
                         ra.cipher.encrypt_message({"t": "chat", "text": "x"})),
                     crypto.DecryptError)
    else:
        check("跨会话测试的前置握手成功", False, str(box2.get("a_error")))

    # 握手记录被改 -> 证明签名失败
    hello = crypto.make_hello(a, base64.b64encode(os.urandom(32)).decode(),
                             base64.b64encode(os.urandom(32)).decode(), "sid1", "friend")
    hello2 = dict(hello)
    hello2["x"] = base64.b64encode(os.urandom(32)).decode()
    digest1 = crypto.transcript_digest(hello, hello)
    digest2 = crypto.transcript_digest(hello2, hello)
    check("改动临时公钥会改变握手摘要", digest1 != digest2)

    proof = crypto.make_proof(a, "sid1", digest1, "initiator")
    crypto.check_proof(proof, a.card(), "sid1", digest1, "initiator")
    check("正确的证明可以通过校验", True)
    expect_error("用错误摘要校验证明会失败",
                 lambda: crypto.check_proof(proof, a.card(), "sid1", digest2, "initiator"),
                 SecurityError)


def test_friend_request_signature() -> None:
    print("好友请求签名")
    a, b = LocalIdentity.generate("小明"), LocalIdentity.generate("小红")
    ts = 1700000000
    payload = crypto.request_signature_payload(a.card(), b.peer_id, ts, "一起玩")
    signature = base64.b64encode(a.sign(payload)).decode()
    check("签名可校验", crypto.verify_request_signature(a.card(), b.peer_id, ts, "一起玩", signature))
    check("改附言后签名失效",
          not crypto.verify_request_signature(a.card(), b.peer_id, ts, "改成别的", signature))
    check("改收件人后签名失效",
          not crypto.verify_request_signature(a.card(), LocalIdentity.generate("x").peer_id,
                                              ts, "一起玩", signature))


def test_rekey_mechanism() -> None:
    """密钥轮换机制的密码学部分。

    (注意: 自动轮换的"触发时机"仍在完善, 默认关闭; 这里固化的是密钥派生部分 ——
    双方用同样的输入各自算出同样的新密钥, 不需要传输任何密钥材料。)
    """
    print("密钥轮换机制")
    a, b = LocalIdentity.generate("小明"), LocalIdentity.generate("小红")
    box = _handshake_pair(a, b, expect_a=b.peer_id, expect_b=a.peer_id)
    if "a" not in box or "b" not in box:
        check("握手成功 (前置条件)", False, str(box.get("a_error", box.get("b_error"))))
        return

    ra, rb = box["a"], box["b"]
    fresh = base64.b64encode(os.urandom(32)).decode()
    new_a = ra.cipher.next_epoch(fresh, ra.session_id)      # 发起方视角
    new_b = rb.cipher.next_epoch(fresh, rb.session_id)      # 接收方视角

    check("轮换后 epoch 递增", new_a.epoch == 1 and new_b.epoch == 1,
          f"{ra.cipher.epoch}/{rb.cipher.epoch} → {new_a.epoch}/{new_b.epoch}")
    token = new_a.encrypt_message({"t": "chat", "text": "轮换之后"})
    check("轮换后 A→B 仍然互通", new_b.decrypt_message(token).get("text") == "轮换之后")
    token2 = new_b.encrypt_message({"t": "chat", "text": "反向也对"})
    check("轮换后 B→A 仍然互通", new_a.decrypt_message(token2).get("text") == "反向也对")
    expect_error("旧密钥解不开新密文",
                 lambda: ra.cipher.decrypt_message(token), crypto.DecryptError)
    other = ra.cipher.next_epoch(base64.b64encode(os.urandom(32)).decode(), ra.session_id)
    check("不同随机数派生出不同密钥",
          other.encrypt_message({"x": 1})["c"] != new_a.encrypt_message({"x": 1})["c"])


def test_question_answer() -> None:
    """加好友验证问题: 答案不上网, 每次提问只传一次性证明。"""
    print("加好友验证问题")
    salt = crypto.new_answer_salt()
    nonce1, nonce2 = crypto.new_answer_nonce(), crypto.new_answer_nonce()
    proof = crypto.answer_proof("  北京 ", salt, nonce1)
    check("答案归一化后能对上 (首尾空格无关)",
          crypto.answer_proof_matches("北京", salt, nonce1, proof))
    check("错误答案被拒绝", not crypto.answer_proof_matches("上海", salt, nonce1, proof))
    check("换随机数后旧证明失效 (防重放)",
          not crypto.answer_proof_matches("北京", salt, nonce2, proof))
    check("每次证明都不同 (线上看不到答案本身)",
          proof != crypto.answer_proof("北京", salt, nonce2))
    check("英文大小写/多空格也归一化",
          crypto.answer_proof_matches("  HelLo  World ", salt, nonce1,
                                      crypto.answer_proof("hello world", salt, nonce1)))
    check("只存哈希不存明文",
          crypto.answer_hash("北京", salt) != "北京"
          and len(crypto.answer_hash("北京", salt)) > 20)
    # 重启存活: 出题方只用硬盘上存下来的派生密钥就能校验 (内存里没有答案明文)
    stored_key = crypto.answer_hash("北京", salt)
    check("只用落盘的派生密钥也能校验 (重启后仍有效)",
          crypto.answer_proof_matches_key(stored_key, nonce1,
                                          crypto.answer_proof("北京", salt, nonce1)))
    check("派生密钥相同即证明相同",
          crypto.answer_proof_with_key(stored_key, nonce1) == proof)
    check("换一把密钥就验不过",
          not crypto.answer_proof_matches_key(crypto.answer_hash("上海", salt), nonce1, proof))


def test_framing() -> None:
    print("分帧 (粘包 / 半包 / 超长)")
    server, client = _pair()
    reader = protocol.FrameReader(server)
    client.sendall(protocol.encode_frame({"i": 1}) + protocol.encode_frame({"i": 2}))
    raw = protocol.encode_frame({"i": 3})
    client.sendall(raw[:5])
    import time as _time
    _time.sleep(0.05)
    client.sendall(raw[5:])
    got = [reader.read_frame(timeout=2).get("i") for _ in range(3)]
    check("粘包/半包都正确处理", got == [1, 2, 3], str(got))

    client.sendall(b"x" * (protocol.MAX_FRAME_BYTES + 10) + b"\n")
    expect_error("超长帧被拒绝", lambda: reader.read_frame(timeout=5), protocol.ProtocolError)
    server.close()
    client.close()


def test_manual_address_input() -> None:
    """手动添加的地址容错: 中文冒号 / 全角数字 / 空格等都要能自动纠正。

    踩过: 中文输入法下打 `192.168.1.5：50606` (冒号是 U+FF1A), 程序只报
    "端口不是数字: 50606" —— 肉眼看不出哪里错了, 用户只能来问。
    """
    print("手动添加: 地址输入容错 (中文冒号 / 全角字符 / 空格 / 前缀)")
    from lanchat.service import normalize_address_input, split_manual_address

    cases = [
        ("192.168.1.5:50606", ("192.168.1.5", 50606), "标准写法"),
        ("192.168.1.5：50606", ("192.168.1.5", 50606), "中文冒号"),
        ("192.168.1.5：５０６０６", ("192.168.1.5", 50606), "中文冒号 + 全角数字"),
        ("  192.168.1.5 : 50606  ", ("192.168.1.5", 50606), "前后/中间空格"),
        ("192.168.1.5 50606", ("192.168.1.5", 50606), "空格代替冒号"),
        ("`192.168.1.5:50606`", ("192.168.1.5", 50606), "连反引号一起粘进来"),
        ("http://192.168.1.5:50606/", ("192.168.1.5", 50606), "带协议前缀和路径"),
        ("100.64.0.7", ("100.64.0.7", 0), "只填 IP (port=0 去探测)"),
        ("100.64.0.7　", ("100.64.0.7", 0), "全角空格"),
    ]
    for raw, expected, why in cases:
        host, port, error = split_manual_address(raw)
        check(f"{why}: {raw!r} -> {expected}",
              (host, port, error) == (expected[0], expected[1], ""),
              f"得到 ({host!r}, {port!r}, {error!r})")

    for raw, why in (("", "空"), ("   ", "全空格"), ("：50606", "只有冒号和端口"),
                     ("192.168.1.5:abc", "端口不是数字"),
                     ("192.168.1.5：７００００", "全角端口超范围")):
        host, port, error = split_manual_address(raw)
        check(f"该报错的还是报错 ({why})", bool(error) and not host,
              f"{raw!r} -> ({host!r}, {port!r}, {error!r})")

    check("归一化函数本身也幂等",
          normalize_address_input(normalize_address_input("192.168.1.5：50606"))
          == "192.168.1.5:50606",
          normalize_address_input("192.168.1.5：50606"))


def test_interface_enumeration_is_cached_and_async() -> None:
    """建发现层**不能**在调用线程里枚举网卡 —— 那正是"设置完昵称先未响应几秒"的根因。

    打包成窗口程序后 `ipconfig` 子进程要 1.5~2.5 秒 (源码运行只要 0.04 秒), 而它以前是在
    `DiscoveryService.__init__` 里同步跑的, 而那句又在 Tk 的界面线程上 (ChatApp → start())。
    现在: 结果缓存 + 缓存冷时丢给后台线程, 调用者立刻返回。
    """
    print("启动卡顿: 网卡枚举必须缓存 + 只在后台线程里做")
    from lanchat import discovery as D

    name = "_interfaces_via_ifconfig" if os.name == "posix" else "_interfaces_via_ipconfig_windows"
    real = getattr(D, name)
    calls = {"n": 0}

    def slow_enum():
        calls["n"] += 1
        time.sleep(1.5)                      # 假装 ipconfig 很慢
        return [("10.0.0.5", "255.255.255.0")]

    setattr(D, name, slow_enum)
    D._IFACE_CACHE["pairs"], D._IFACE_CACHE["at"] = [], 0.0        # 清缓存
    started = time.perf_counter()
    try:
        svc = D.DiscoveryService(peer_id="lc-unit", name="单元", tcp_port=1, discovery_port=51999)
        elapsed = time.perf_counter() - started
        # 判据是"调用者没等": 后台线程可能已经开跑 (calls 计数会变), 但调用必须立刻返回
        check("缓存冷时建发现层也不阻塞调用者 (没有同步等 ipconfig)",
              elapsed < 0.5 and svc._targets == [],
              f"耗时 {elapsed:.2f}s, 后台枚举 {calls['n']} 次, targets={svc._targets}")

        warmed = False
        deadline = time.time() + 6
        while time.time() < deadline:
            if svc._targets == ["10.0.0.255"]:
                warmed = True
                break
            time.sleep(0.1)
        check("后台线程随后把广播地址补上了", warmed, f"targets={svc._targets} calls={calls['n']}")

        before = calls["n"]
        D.interface_pairs()
        D.interface_pairs()
        check("有缓存之后不再重复枚举 (每次启动只付一次代价)", calls["n"] == before,
              f"{before} -> {calls['n']}")
        check("max_age=0 可以强制重新枚举",
              (lambda n: D.interface_pairs(max_age=0.0) is not None and calls["n"] == n + 1)(calls["n"]),
              f"calls={calls['n']}")
    finally:
        setattr(D, name, real)
        D._IFACE_CACHE["pairs"], D._IFACE_CACHE["at"] = [], 0.0


class _StubConn:
    """假的会话对象: 只把要发的帧记下来 (测补发逻辑, 不用真连网)。"""

    def __init__(self, peer_id: str, name: str = "对端") -> None:
        self.peer_id = peer_id
        self.peer_name = name
        self.authenticated = True
        self.session_id = "unit-test"
        self.sent: list = []

    def send_message(self, message, urgent: bool = False) -> bool:  # noqa: ARG002
        self.sent.append(message)
        return True


class _FakeContact:
    unread = 0
    is_friend = False


def test_history_sync_logic() -> None:
    """会话记录补发: 谁缺什么补什么、重复的不补、离线期间发的按"新消息"算。"""
    print("会话记录: 补发逻辑 (对账 / 去重 / 分帧)")
    from lanchat.constants import HISTORY_BATCH
    from lanchat.service import ChatService, Contact, ContactState, EventKind

    events: list = []
    a = ChatService(name="甲", data_dir=os.path.join(TMP, "hist-a"), enable_discovery=False,
                    tcp_port=0, on_event=events.append)
    b = ChatService(name="乙", data_dir=os.path.join(TMP, "hist-b"), enable_discovery=False,
                    tcp_port=0, on_event=events.append)
    try:
        a._contacts[b.peer_id] = Contact(peer_id=b.peer_id, name="乙",
                                         state=ContactState.FRIEND.value)
        b._contacts[a.peer_id] = Contact(peer_id=a.peer_id, name="甲",
                                         state=ContactState.FRIEND.value)
        now = time.time()
        # 甲这边: 自己发了 3 条 (第 3 条是"乙离线时发的"), 收到乙 2 条
        for seq in (1, 2, 3):
            entry = a._record_chat(b.peer_id, own=True, seq=seq, text=f"甲第{seq}句",
                                   ts=now + seq)
            entry["pending"] = (seq == 3)
        for seq in (1, 2):
            a._record_chat(b.peer_id, own=False, seq=seq, text=f"乙第{seq}句", ts=now + 10 + seq)
        a._out_seq[b.peer_id], a._peer_seq[b.peer_id] = 3, 2

        check("对账信息: 收到他 2 条, 自己发到第 3 条",
              a.history_state(b.peer_id) == {"have_in": 2, "have_out": 3},
              str(a.history_state(b.peer_id)))

        conn = _StubConn(b.peer_id, "乙")
        # 乙说自己只有"甲的第 1 条 + 自己的第 1 条" -> 甲要补: 甲2/甲3 + 乙2
        a._handle_history_state(conn, {"in": 1, "out": 1})
        frames = [m for m in conn.sent if m.get("t") == "history-replay"]
        entries = [e for frame in frames for e in frame["entries"]]
        check("只补对方缺的 (不多发)",
              sorted(e["text"] for e in entries) == ["乙第2句", "甲第2句", "甲第3句"],
              str([e["text"] for e in entries]))
        check("补发按时间顺序", [e["text"] for e in entries]
              == ["甲第2句", "甲第3句", "乙第2句"], str([e["text"] for e in entries]))
        check("当时没送出去的那条带着 pending 标记",
              [e.get("pending") for e in entries if e["text"] == "甲第3句"] == [True],
              str(entries))
        check("补发完会给个提示",
              any(e.kind is EventKind.INFO and "补发" in e.text for e in events),
              str([e.text for e in events if e.kind is EventKind.INFO]))

        # 乙收到这些 -> 记回自己的记录; 甲第 3 条对乙来说是"新消息"
        b_events: list = []
        b.on_event = b_events.append
        b_conn = _StubConn(a.peer_id, "甲")
        for frame in frames:
            b._handle_history_replay(b_conn, frame)
        got = {(e["own"], e["text"]) for e in b.conversation(a.peer_id)}
        check("乙把自己的记录拼回来了",
              got == {(False, "甲第2句"), (False, "甲第3句"), (True, "乙第2句")}, str(got))
        check("补的历史算 restored, 离线期间那条算新消息",
              any(e.kind is EventKind.MESSAGE and e.data.get("queued_delivery")
                  for e in b_events),
              str([(e.data.get("text"), e.data.get("restored"),
                    e.data.get("queued_delivery")) for e in b_events
                   if e.kind is EventKind.MESSAGE]))
        check("离线期间那条记进未读",
              (b.get_contact(a.peer_id) or _FakeContact()).unread == 1,
              str((b.get_contact(a.peer_id) or _FakeContact()).unread))
        check("取回历史时给界面一条 HISTORY 事件",
              any(e.kind is EventKind.HISTORY for e in b_events),
              str([e.text for e in b_events if e.kind is EventKind.HISTORY]))

        before = len(b.conversation(a.peer_id))
        for frame in frames:                    # 同样的帧再来一遍: 必须去重
            b._handle_history_replay(b_conn, frame)
        check("重复补发不会重复记录", len(b.conversation(a.peer_id)) == before,
              f"{before} -> {len(b.conversation(a.peer_id))}")

        # 分帧: 一次补很多条时不应该塞进一个巨帧
        conn2 = _StubConn(b.peer_id, "乙")
        a._chat_log[b.peer_id] = []
        total = HISTORY_BATCH * 2 + 4               # 165 条: 80 + 80 + 5
        for seq in range(1, total + 1):
            a._record_chat(b.peer_id, own=True, seq=seq, text=f"批量{seq}", ts=now + seq)
        a._out_seq[b.peer_id] = total
        a._handle_history_state(conn2, {"in": 0, "out": 0})
        # 多帧是**带随机间隔**在后台线程里发的 (别一发一大串), 所以这里等一会儿
        deadline = time.time() + 8
        while time.time() < deadline:
            if sum(len(m["entries"]) for m in conn2.sent if m.get("t") == "history-replay") >= total:
                break
            time.sleep(0.05)
        chunks = [len(m["entries"]) for m in conn2.sent if m.get("t") == "history-replay"]
        expected = [HISTORY_BATCH] * (total // HISTORY_BATCH)
        if total % HISTORY_BATCH:
            expected.append(total % HISTORY_BATCH)
        check(f"条数多时按 {HISTORY_BATCH} 条分帧", chunks == expected,
              f"{chunks} vs {expected}")
        check("分帧后条数不多不少", sum(chunks) == total, f"{sum(chunks)} vs {total}")
        check("记录超过上限时只留最近若干条",
              len(a.conversation(b.peer_id)) <= 500, str(len(a.conversation(b.peer_id))))
    finally:
        a.stop()
        b.stop()


def test_length_padding() -> None:
    """长度混淆: 每次随机插 + 按需压缩, 密文长度不能暴露消息长短。"""
    print("长度混淆 (padding/zlib): 包大小看不出发了什么")
    from lanchat import crypto, protocol
    from lanchat.constants import PAD_STEP

    # 会话密钥是**方向分离**的 (发送/接收各一把), 所以要按真实情况造两端
    key_a, key_b = os.urandom(32), os.urandom(32)
    sender = crypto.SessionCipher(key_a, key_b, "sid-pad", "lc-a", "lc-b")
    receiver = crypto.SessionCipher(key_b, key_a, "sid-pad", "lc-b", "lc-a")

    def wire(obj) -> int:
        sealed = crypto.seal_message(obj)
        return len(protocol.encode_frame({"type": "enc", "body": sender.encrypt_message(sealed)}))

    # 1) 加解密往返必须完全一样 (填充/压缩在解密侧被还原)
    original = {"t": "chat", "text": "你好", "seq": 3, "ts": 1.0}
    padded = crypto.pad_message(original)
    check("填充字段确实加上了", "p" in padded and len(padded["p"]) > 0,
          f"p 长度 {len(padded.get('p', ''))}")
    check("原消息没被改动", original == {"t": "chat", "text": "你好", "seq": 3, "ts": 1.0},
          str(original))
    back = receiver.decrypt_message(sender.encrypt_message(crypto.seal_message(original)))
    check("解密回来和原来一模一样 (填充丢掉 / 压缩还原)", back == original, str(back))

    # 2) "每次随机插": 同一条消息发 20 次, 大小应该各不相同
    same = {wire({"t": "chat", "text": "一样的内容", "seq": 1, "ts": 1.0}) for _ in range(20)}
    check("同一条消息每次发送大小都不同 (不是固定指纹)", len(same) >= 12,
          f"20 次里 {len(same)} 种大小: {sorted(same)[:6]}...")

    # 3) 长度差别很大的短消息, 大小仍然落在一个很窄的窗口里
    short = [wire({"t": "chat", "text": "x" * length, "seq": 1, "ts": 1.0})
             for length in (1, 20, 80, 200)]
    check("长度 1~200 的消息密文大小看不出区别 (窗口 <= 一个步长)",
          max(short) - min(short) <= 2 * PAD_STEP, f"sizes={short}")

    # 4) 压缩: 重复内容要真的变小 (而且解出来一字不差)
    long_text = "这是一段会被压缩的重复内容。" * 120        # 约 4.5 KB 明文
    repetitive = {"t": "chat", "text": long_text, "seq": 5, "ts": 1.0}
    compressed = wire(repetitive)
    saved = crypto.COMPRESS_ENABLED
    try:
        crypto.COMPRESS_ENABLED = False
        uncompressed = wire(repetitive)
    finally:
        crypto.COMPRESS_ENABLED = saved
    check("重复内容会被 zlib 压小 (省流量)", compressed < uncompressed * 0.5,
          f"{uncompressed} -> {compressed} 字节")
    restored = receiver.decrypt_message(sender.encrypt_message(crypto.seal_message(repetitive)))
    check("压缩过的消息解出来一字不差", restored == repetitive,
          f"长度 {len(str(restored.get('text', '')))} vs {len(long_text)}")

    # 5) 大块 (文件分片) 既不补也不压: 补了没意义, 压了白费 CPU
    big_raw = {"t": "file-chunk", "id": "t1", "body": base64.b64encode(
        os.urandom(PAD_STEP * 40)).decode()}
    check("大块不补", "p" not in crypto.pad_message(big_raw), str(sorted(big_raw)))
    check("大块不压 (已经是高熵数据)", "z" not in crypto.seal_message(big_raw),
          str(sorted(crypto.seal_message(big_raw))))


def test_probe_ports() -> None:
    """手动添加时的单播探测端口候选 (只有 IP 时打哪里)。"""
    print("单播探测: 端口候选 & 隐身开关")
    from lanchat.constants import DEFAULT_DISCOVERY_PORT
    from lanchat.service import ChatService

    data = os.path.join(TMP, "probe-ports")
    svc = ChatService(name="甲", data_dir=data, enable_discovery=False, tcp_port=0,
                      discovery_port=50511)
    try:
        check("默认: 先试本机发现端口, 再补一个标准 50505",
              svc._probe_ports() == [50511, DEFAULT_DISCOVERY_PORT], str(svc._probe_ports()))
        check("两个地址重复时不会重复探测",
              svc._probe_ports() == [50511, DEFAULT_DISCOVERY_PORT]
              and len(set(svc._probe_ports())) == 2, str(svc._probe_ports()))
        same = ChatService(name="乙", data_dir=data + "-same", enable_discovery=False,
                           tcp_port=0, discovery_port=DEFAULT_DISCOVERY_PORT)
        try:
            check("本机发现端口就是 50505 时不重复",
                  same._probe_ports() == [DEFAULT_DISCOVERY_PORT], str(same._probe_ports()))
        finally:
            same.stop()
        check("隐身开关默认开着", svc.discoverable is True)
        svc.set_discoverable(False)
        check("隐身开关能关 (并落盘)",
              svc.discoverable is False
              and "discoverable" in open(os.path.join(data, "settings.json"),
                                        encoding="utf-8").read(),
              open(os.path.join(data, "settings.json"), encoding="utf-8").read()[:80])
        again = ChatService(name="甲", data_dir=data, enable_discovery=False, tcp_port=0)
        check("重启后隐身设置还在 (默认应读回 False)", again.discoverable is False,
              str(again.discoverable))
        again.stop()
    finally:
        svc.stop()


def main() -> int:
    print("=" * 60)
    print("单元测试")
    print("=" * 60)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    try:
        for fn in (test_identity, test_handshake_ok, test_handshake_identity_mismatch,
                   test_tamper_detection, test_friend_request_signature,
                   test_rekey_mechanism, test_question_answer, test_framing,
                   test_manual_address_input, test_interface_enumeration_is_cached_and_async,
                   test_history_sync_logic, test_length_padding, test_probe_ports):
            fn()
            print()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
        return 1
    print("✅ 全部单元测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
