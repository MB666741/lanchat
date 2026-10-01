"""会话层密钥轮换验证。

覆盖三件必须成立的事:
  1. 连续单向消息: 一条不丢、顺序正确, 而且**每一次轮换都真的发生**
     (旧版本 `_rekey_outstanding` 忘了收尾, 轮换两次之后就再也不轮换了,
      接着 10 秒超时又会单方面换密钥 —— 两边密钥岔开, 传大文件时直接掉线);
  2. 两边**同时**大量收发: 双方 epoch 必须始终一致, 会话不能断;
  3. 频繁轮换期间传大文件: 内容 SHA256 一致, 连接存活。

直接运行:  python tests/test_rekey.py
"""

import hashlib
import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lanchat.console import configure_console  # noqa: E402

configure_console()
from lanchat import crypto  # noqa: E402
from lanchat.connection import Connection  # noqa: E402
from lanchat.identity import LocalIdentity  # noqa: E402

FAILURES: list[str] = []


def _instrument() -> None:
    """打点: 看轮换卡在哪一步 (REKEY_TRACE=1 时启用)。"""
    orig_maybe = Connection._maybe_auto_rekey
    orig_rekey = Connection._handle_rekey
    orig_rotate = Connection.rotate_keys
    orig_install = Connection._install_cipher

    def spy_maybe(self):
        before = (self._payload_count, self._rekey_outstanding, self.cipher.epoch)
        result = orig_maybe(self)
        after = (self._payload_count, self._rekey_outstanding, self.cipher.epoch)
        if before != after:
            print(f"      [MAYBE {self.peer_name}] {before} -> {after}", flush=True)
        return result

    def spy_rotate(self):
        result = orig_rotate(self)
        print(f"      [TX-REKEY {self.peer_name}] begin epoch={self.cipher.epoch} "
              f"sent={result}", flush=True)
        return result

    def spy_rekey(self, message):
        print(f"      [RX-REKEY {self.peer_name}] {message.get('state')} "
              f"msg_ep={message.get('epoch')} my_ep={self.cipher.epoch} "
              f"out={self._rekey_outstanding}", flush=True)
        return orig_rekey(self, message)

    def spy_install(self, new_cipher):
        print(f"      [INSTALL {self.peer_name}] epoch {self.cipher.epoch} -> "
              f"{new_cipher.epoch}", flush=True)
        return orig_install(self, new_cipher)

    Connection._maybe_auto_rekey = spy_maybe
    Connection._handle_rekey = spy_rekey
    Connection.rotate_keys = spy_rotate
    Connection._install_cipher = spy_install


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _pair(rekey_every: int, incoming_a: str, incoming_b: str) -> tuple:
    """建立一对已握手的 Connection。"""
    a_id, b_id = LocalIdentity.generate("A"), LocalIdentity.generate("B")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket()
    client.connect(listener.getsockname())
    server, _ = listener.accept()
    listener.close()

    box: dict = {}

    def respond() -> None:
        box["b"] = crypto.handshake_respond(server, b_id, expected_peer=a_id.peer_id,
                                            purpose="friend", timeout=5,
                                            rekey_every=rekey_every)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    ra = crypto.handshake_initiate(client, a_id, expected_peer=b_id.peer_id,
                                   purpose="friend", timeout=5, rekey_every=rekey_every)
    thread.join(5)
    rb = box["b"]

    got_b: list = []
    got_a: list = []
    lock = threading.Lock()

    def on_b(conn, message):
        if message.get("t") == "chat":
            with lock:
                got_b.append(message.get("text"))

    def on_a(conn, message):
        if message.get("t") == "chat":
            with lock:
                got_a.append(message.get("text"))

    conn_a = Connection(client, ra, "127.0.0.1", True, on_a, lambda *_: None,
                        incoming_a, rekey_every=rekey_every)
    conn_b = Connection(server, rb, "127.0.0.1", False, on_b, lambda *_: None,
                        incoming_b, rekey_every=rekey_every)
    return conn_a, conn_b, got_a, got_b, lock


def test_frames_in_rekey_window() -> None:
    """轮换窗口: 我发起了轮换、还没收到 accept 时, 对方已经用新密钥发帧了。

    这是"疯狂密文认证失败、断开又重连"的根因: 对方收到 begin 就立刻切到新密钥,
    它在这期间发来的帧用的是新密钥; 如果发起方只认旧密钥, 就会被判成
    "密文认证失败"-> 断开。发起方必须预先备好"下一轮密钥"专门用来解这种帧。
    """
    print("\n轮换窗口: 对方先切了密钥, 我还没收到回执时它发来的帧")
    tmpdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".tmp-rekey-window")
    os.makedirs(os.path.join(tmpdir, "a"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "b"), exist_ok=True)
    conn_a = conn_b = None
    try:
        conn_a, conn_b, got_a, got_b, _lock = _pair(
            0, os.path.join(tmpdir, "a"), os.path.join(tmpdir, "b"))
        conn_a.start()
        conn_b.start()

        # 把 A 处理 accept 的动作"扣住", 模拟回执还没到
        stashed: list = []
        real_handler = conn_a._handle_rekey

        def slow_handler(message):
            if message.get("state") == "accept":
                stashed.append(message)
                return
            return real_handler(message)

        conn_a._handle_rekey = slow_handler       # type: ignore[assignment]
        check("发起轮换", conn_a.rotate_keys())
        deadline = time.time() + 5
        while time.time() < deadline and conn_b.cipher.epoch == 0:
            time.sleep(0.05)
        check("对方已切到新密钥", conn_b.cipher.epoch == 1,
              f"乙 epoch={conn_b.cipher.epoch}")
        check("我这边还没收到回执 (还是旧密钥)", conn_a.cipher.epoch == 0,
              f"甲 epoch={conn_a.cipher.epoch}")

        # 此刻对方发消息: 用的是新密钥
        got_a.clear()
        conn_b.send_chat("轮换窗口里的消息")
        deadline = time.time() + 5
        while time.time() < deadline and not got_a:
            time.sleep(0.05)
        check("对方换密钥后发的消息我仍然收得到 (没有断开)",
              got_a == ["轮换窗口里的消息"], f"收到 {got_a}")
        check("连接没有因此断开", conn_a.is_alive and conn_b.is_alive,
              f"甲 {conn_a._close_reason} / 乙 {conn_b._close_reason}")

        # 回执到了之后一切照旧
        for message in stashed:
            real_handler(message)
        deadline = time.time() + 5
        while time.time() < deadline and conn_a.cipher.epoch != conn_b.cipher.epoch:
            time.sleep(0.05)
        check("回执到达后双方 epoch 一致",
              conn_a.cipher.epoch == conn_b.cipher.epoch,
              f"甲 {conn_a.cipher.epoch} / 乙 {conn_b.cipher.epoch}")
        got_a.clear()
        conn_b.send_chat("回执之后的消息")
        deadline = time.time() + 5
        while time.time() < deadline and not got_a:
            time.sleep(0.05)
        check("回执之后依然能收消息", got_a == ["回执之后的消息"], f"收到 {got_a}")
    finally:
        for conn in (conn_a, conn_b):
            if conn is not None:
                try:
                    conn.close("测试结束")
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    if os.environ.get("REKEY_TRACE"):
        _instrument()

    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-rekey")
    shutil.rmtree(base, ignore_errors=True)
    tmpdir = base
    os.makedirs(os.path.join(tmpdir, "a"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "b"), exist_ok=True)
    conn_a = conn_b = None
    try:
        conn_a, conn_b, got_a, got_b, lock = _pair(
            5, os.path.join(tmpdir, "a"), os.path.join(tmpdir, "b"))
        conn_b.auto_accept_files = True        # 文件测试里让对方自动收
        conn_a.start()
        conn_b.start()

        # ---------- 1. 单向连续消息 ----------
        total = 40
        for i in range(total):
            conn_a.send_chat(f"A-{i}")
            time.sleep(0.05)
        deadline = time.time() + 20
        while time.time() < deadline and len(got_b) < total:
            time.sleep(0.1)
        check(f"A→B 连续 {total} 条消息全部送达", len(got_b) == total,
              f"实收 {len(got_b)} 条")
        check("消息顺序与内容正确",
              got_b == [f"A-{i}" for i in range(total)],
              f"前 3 条 {got_b[:3]} 后 2 条 {got_b[-2:]}")
        check("每一次轮换都真的发生了 (不是轮换一两次就卡住)",
              conn_a.rekeys_done >= total // 5 - 2,
              f"甲 {conn_a.rekeys_done} 次 (期望 ≈ {total // 5}) / 乙 {conn_b.rekeys_done} 次")
        check("双方 epoch 始终一致", conn_a.cipher.epoch == conn_b.cipher.epoch,
              f"甲 {conn_a.cipher.epoch} / 乙 {conn_b.cipher.epoch}")
        check("会话仍然存活", conn_a.is_alive and conn_b.is_alive)

        # ---------- 2. 两边同时大量收发 ----------
        before_epoch = conn_a.cipher.epoch
        bullet = 60
        stop = threading.Event()

        def blast(conn, tag):
            for i in range(bullet):
                if stop.is_set():
                    return
                conn.send_chat(f"{tag}-{i}")
                time.sleep(0.005)

        threads = [threading.Thread(target=blast, args=(conn_a, "X")),
                   threading.Thread(target=blast, args=(conn_b, "Y"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop.set()
        time.sleep(2.5)
        with lock:
            received = len(got_a) + len(got_b)
        check("双向对轰后连接没断", conn_a.is_alive and conn_b.is_alive,
              f"甲 {conn_a._close_reason} / 乙 {conn_b._close_reason}")
        check("双向对轰期间仍在持续轮换", conn_a.cipher.epoch > before_epoch + 5,
              f"epoch {before_epoch} -> {conn_a.cipher.epoch}")
        check("双向对轰后双方 epoch 依然一致",
              conn_a.cipher.epoch == conn_b.cipher.epoch,
              f"甲 {conn_a.cipher.epoch} / 乙 {conn_b.cipher.epoch}")
        # 用户报过"对面说轮换了 3 次, 我这边才 2 次": 同一会话内两边的计数必须一样
        check("双方轮换次数也一致 (不会一边多一边少)",
              conn_a.rekeys_done == conn_b.rekeys_done,
              f"甲 {conn_a.rekeys_done} 次 / 乙 {conn_b.rekeys_done} 次")
        check("双向对轰的消息基本都收到了", received >= bullet,
              f"收到 {received} / 至少 {bullet}")

        # ---------- 3. 频繁轮换期间传大文件 ----------
        conn_a.rekey_every = 3
        conn_b.rekey_every = 3
        payload = os.urandom(2 * 1024 * 1024)      # 8 个 256 KiB 分片
        src = os.path.join(tmpdir, "big.bin")
        with open(src, "wb") as fh:
            fh.write(payload)
        digest = hashlib.sha256(payload).hexdigest()
        transfer = conn_a.send_file(src)
        # Connection 层只负责发请求: 对方同意后由上层 (service) 调 start_transfer 开始推数据
        deadline = time.time() + 10
        while time.time() < deadline and transfer.transfer_id not in conn_b.incoming:
            time.sleep(0.05)
        check("对方已同意接收", transfer.transfer_id in conn_b.incoming)
        conn_a.start_transfer(transfer.transfer_id)

        target = os.path.join(os.path.join(tmpdir, "b"), "big.bin")

        def ready() -> bool:
            try:
                return os.path.isfile(target) and os.path.getsize(target) >= len(payload)
            except OSError:
                return False

        deadline = time.time() + 60
        while time.time() < deadline and not ready():
            time.sleep(0.1)
        check("轮换期间大文件传完了", ready(),
              f"目录 {os.listdir(os.path.join(tmpdir, 'b'))} "
              f"实际 {os.path.getsize(target) if os.path.isfile(target) else '无'} 字节 / "
              f"期望 {len(payload)}")
        if ready():
            time.sleep(0.4)
            with open(target, "rb") as fh:
                check("大文件内容 SHA256 一致",
                      hashlib.sha256(fh.read()).hexdigest() == digest)
        check("传完大文件后连接依然存活", conn_a.is_alive and conn_b.is_alive,
              f"甲 {conn_a._close_reason} / 乙 {conn_b._close_reason}")
        check("传完大文件后双方 epoch 一致",
              conn_a.cipher.epoch == conn_b.cipher.epoch,
              f"甲 {conn_a.cipher.epoch} / 乙 {conn_b.cipher.epoch}")

        # ---------- 4. 继续聊天 ----------
        got_b.clear()
        conn_a.send_chat("轮换之后还能聊")
        deadline = time.time() + 10
        while time.time() < deadline and "轮换之后还能聊" not in got_b:
            time.sleep(0.1)
        check("轮换之后还能继续聊天", "轮换之后还能聊" in got_b, str(got_b[-3:]))
    finally:
        for conn in (conn_a, conn_b):
            if conn is not None:
                try:
                    conn.close("测试结束")
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    test_frames_in_rekey_window()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
        return 1
    print("✅ 会话层密钥轮换验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
