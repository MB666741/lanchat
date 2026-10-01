"""单条 TCP 加密会话。

一条连接 = 三个线程:
    reader   : recv -> 用会话密钥解密 -> 分派 (聊天 / 文件分片 / 控制帧)
    writer   : 从发送队列取密文帧 -> sendall (避免多线程同时写 socket)
    heartbeat: 定期发 ping, 及时发现"假连接"

安全边界: 所有内容都经过 AES-256-GCM 加密; 若会话还没通过好友确认
(state=PENDING), 上层只允许发"加好友请求"这类元数据。
"""

from __future__ import annotations

import hashlib
import os
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import crypto, protocol
from .constants import FILE_CHUNK_SIZE, HEARTBEAT_INTERVAL, REKEY_RETRY_SECONDS
from .crypto import DecryptError, HandshakeResult, SessionCipher, SessionState

_SENTINEL = object()
_REKEY_LABEL = "rekey"      # 会话内部使用的密钥轮换消息标记 (不上报给上层)
_REKEY_REQUEST = "rekey-request"   # "请你发起一次轮换" (传文件前后用, 见 request_rotation)

class TransferError(Exception):
    pass


# ---------------------------------------------------------------------------
# 文件传输状态
# ---------------------------------------------------------------------------
@dataclass
class OutgoingTransfer:
    """一个待发送/正在发送的文件。"""

    transfer_id: str
    name: str
    path: str
    size: int
    sha256: str
    sent: int = 0
    started: bool = False

    @property
    def progress(self) -> float:
        return 1.0 if self.size == 0 else min(1.0, self.sent / self.size)


@dataclass
class IncomingTransfer:
    """正在接收的文件。"""

    transfer_id: str
    name: str
    size: int
    sha256: str
    path: str = ""
    received: int = 0
    handle: Any = None
    hasher: Any = field(default_factory=hashlib.sha256)
    verified: Optional[bool] = None
    actual_sha256: str = ""

    @property
    def progress(self) -> float:
        return 1.0 if self.size == 0 else min(1.0, self.received / self.size)

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            finally:
                self.handle = None


def file_sha256(path: str, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def unique_download_path(directory: str, name: str) -> str:
    """避免覆盖同名文件。"""
    safe = os.path.basename(name).replace("\x00", "") or "received.bin"
    candidate = os.path.join(directory, safe)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(safe)
    for i in range(1, 10000):
        candidate = os.path.join(directory, f"{stem}({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(directory, f"{uuid.uuid4().hex}{ext}")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class Connection:
    """一条已完成握手、正在运行的加密会话。"""

    def __init__(
        self,
        sock: socket.socket,
        handshake: HandshakeResult,
        host: str,
        initiated_by_us: bool,
        on_message: Callable[["Connection", Dict[str, Any]], None],
        on_close: Callable[["Connection", Optional[str]], None],
        incoming_dir: str,
        auto_accept_files: bool = False,
        rekey_every: int = 0,
    ) -> None:
        self.sock = sock
        self.cipher: SessionCipher = handshake.cipher
        self.card = handshake.card
        self.peer_id = handshake.card.peer_id
        self.peer_name = handshake.card.name
        self.peer_fingerprint = handshake.card.fingerprint
        self.session_id = handshake.session_id
        self.host = host
        self.initiated_by_us = initiated_by_us
        self.authenticated = handshake.verified       # 好友身份已确认; 请求会话为 False
        self.purpose = handshake.purpose
        self.established_at = time.time()
        self.last_activity = time.time()
        self.auto_accept_files = auto_accept_files
        self._incoming_dir = incoming_dir
        self._payload_count = 0        # 本轮收发过的消息数 (统计/自动轮换用)
        self.rekeys_done = 0           # 本次会话轮换过几次密钥
        # 用**握手协商出来**的值: 两边设置不同时按"有 0 就关闭, 否则取更严格(更小)的"
        # (见 crypto.negotiate_rekey), 双方算出的结果必然一致。
        self.local_rekey = max(0, int(rekey_every))     # 我自己的设置 (改设置时会更新)
        self.peer_rekey = handshake.peer_rekey          # 对方声明的值
        self.rekey_every = max(0, int(handshake.rekey_every or 0))   # 协商结果
        self._rekey_outstanding = False
        self._rekey_epoch = -1
        self._rekey_fresh = ""
        self._rekey_sent_at = 0.0
        # "请求换一次密钥"的节流状态 (传文件前后各要一次, 见 request_rotation)
        self._last_rotation_at = 0.0
        self._last_rotation_epoch = -1
        self._rotation_retry: Optional[threading.Timer] = None
        # 换密钥瞬间可能还有"用旧密钥加密、正在路上"的消息, 所以旧密钥留一小段宽限期,
        # 否则那些消息会被判成重放/解密失败 (踩过这个坑)。
        self._old_cipher: Optional[SessionCipher] = None
        self._old_cipher_deadline = 0.0
        self._old_window = 5.0
        # 我发起了轮换、还没收到 accept 之前, 对方已经切成新密钥了;
        # 这把"下一轮密钥"专门用来解它在这段窗口里发来的帧 (只解密, 不发送)。
        self._pending_cipher: Optional[SessionCipher] = None

        self._on_message = on_message
        self._on_close = on_close
        self._send_queue: "queue.Queue[Any]" = queue.Queue(maxsize=256)
        self._write_lock = threading.Lock()
        # 保证"加密 + 入队 + 换密钥"三者原子: 既避免密文乱序, 也避免用错密钥加密。
        # 用可重入锁: _handle_rekey 持锁时会调用 _flush_rekey_queue, 二者都需要这把锁。
        self._order_lock = threading.RLock()
        self._closed = threading.Event()
        self._close_reason: Optional[str] = None

        self.outgoing: Dict[str, OutgoingTransfer] = {}
        self.incoming: Dict[str, IncomingTransfer] = {}
        self._pending_offers: Dict[str, IncomingTransfer] = {}
        self._progress_cb: Dict[str, Optional[Callable[[OutgoingTransfer], None]]] = {}

        self._reader = threading.Thread(target=self._read_loop, name=f"rx-{self.peer_name}", daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name=f"tx-{self.peer_name}", daemon=True)
        self._heartbeat: Optional[threading.Thread] = None

    # -- 生命周期 ----------------------------------------------------------
    def start(self) -> None:
        self._reader.start()
        self._writer.start()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop,
                                           name=f"hb-{self.peer_name}", daemon=True)
        self._heartbeat.start()

    def close(self, reason: str = "主动断开") -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        try:
            self._send_queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        for transfer in list(self.incoming.values()):
            transfer.close()
        self.incoming.clear()
        for pending in list(self._pending_offers.values()):
            pending.close()
        self._pending_offers.clear()
        if self._writer.is_alive() and threading.current_thread() is not self._writer:
            self._writer.join(timeout=1.0)
        if self._on_close:
            self._on_close(self, reason)

    @property
    def is_alive(self) -> bool:
        return not self._closed.is_set()

    def mark_authenticated(self) -> None:
        """好友请求被同意后, 把这条会话升级为可信好友会话。"""
        self.authenticated = True
        self.purpose = "friend"

    def set_incoming_dir(self, directory: str) -> None:
        """用户在设置里改了接收目录 -> 这条会话也跟着改。"""
        if directory:
            self._incoming_dir = directory

    def set_local_rekey(self, value: int) -> int:
        """我在设置里改了轮换频率 -> 重新协商 (对方的偏好仍然有效)。返回协商结果。"""
        self.local_rekey = max(0, int(value or 0))
        self.rekey_every = crypto.negotiate_rekey(self.local_rekey, self.peer_rekey)
        return self.rekey_every

    def set_peer_rekey(self, value: Optional[int]) -> int:
        """对方告诉我他改了轮换频率 -> 重新协商。返回协商结果。"""
        self.peer_rekey = None if value is None else max(0, int(value))
        self.rekey_every = crypto.negotiate_rekey(self.local_rekey, self.peer_rekey)
        return self.rekey_every

    # -- 发送 --------------------------------------------------------------
    def send_message(self, message: Dict[str, Any], urgent: bool = False) -> bool:
        """加密并排队发送一条应用层消息 (message 里带 t 字段)。"""
        if self._closed.is_set():
            return False
        if not self.authenticated and message.get("t") not in _PENDING_ALLOWED:
            # 未确认身份的会话只能用来谈"加好友"
            return False
        self._maybe_auto_rekey()
        return self._send_encrypted(message, urgent)

    def _send_encrypted(self, message: Dict[str, Any], urgent: bool = False) -> bool:
        """在锁里完成"加密 + 入队": 顺序和密钥都不会被打乱。"""
        try:
            with self._order_lock:
                self._payload_count += 1
                # 先压缩 + 长度混淆, 再加密: 这样"包多大"不泄露消息长短 (crypto.seal_message)
                body = self.cipher.encrypt_message(crypto.seal_message(message))
                frame = {"type": "enc", "body": body}
                if urgent:
                    self._send_queue.put_nowait(frame)
                else:
                    self._send_queue.put(frame, timeout=5.0)
            return True
        except queue.Full:
            self.close("发送队列拥塞, 已断开")
            return False
        except Exception as exc:  # noqa: BLE001
            self.close(f"加密失败: {exc}")
            return False

    # -- 密钥轮换 (KeyUpdate) ---------------------------------------------
    #
    # 协议 (双方各自用"共享秘密 + 同一份随机数"派生新密钥, 不传输任何密钥材料):
    #   发起方: 用旧密钥发 begin(fresh) -> 继续用旧密钥发消息 -> 收到 accept 后切到新密钥
    #   接收方: 收到 begin 立即切到新密钥 -> 用**旧密钥**回一个 accept
    # 因为接收方切换后仍保留旧密钥一小段时间 (宽限期) 解密在途消息, 所以:
    #   * 发起方收到 accept 之后才切, 它之前发的旧密钥消息都能被解出来;
    #   * 无需把消息排队, 也就不会出现"某条消息永远卡住"的问题 (踩过这个坑)。
    #
    # 三条硬规则 (都是踩坑换来的):
    #   1. 收到 accept 必须把"这次轮换"收尾 (清掉 _rekey_outstanding), 否则状态机会卡死;
    #   2. **绝不单方面换密钥**: 只有收到对方的 begin/accept 才切。自己偷偷切会和对方岔开,
    #      之后每一帧都解不开 (现象: 传大文件时突然"密文认证失败"然后掉线);
    #   3. 同一时刻只允许一方发起自动轮换 (按 peer_id 定一个"发起方"),
    #      否则两边各用自己的 fresh 派生, 密钥直接岔开。
    def rotate_keys(self) -> bool:
        """立即切换本会话密钥, 并通知对方一起切。返回是否成功发起。"""
        if self._closed.is_set() or not self.authenticated:
            return False
        with self._order_lock:
            if self._rekey_outstanding:
                return False
            self._rekey_outstanding = True
            self._rekey_epoch = self.cipher.epoch
            self._rekey_sent_at = time.time()
            fresh = crypto.b64e(os.urandom(32))
            self._rekey_fresh = fresh
            # 提前把"下一轮密钥"准备好, 只用来**解密**, 不用于发送:
            # 对方收到 begin 就会立刻切到新密钥, 在它回 accept、我这边收到之前,
            # 它发来的帧已经是新密钥了。没有这把 pending 密钥, 那些帧会被判成
            # "密文认证失败" -> 断开重连 (现象: 疯狂验证失败、断开又重连)。
            try:
                self._pending_cipher = self.cipher.next_epoch(fresh, self.session_id)
            except Exception:  # noqa: BLE001 - 派生失败就不预置, 走老逻辑
                self._pending_cipher = None
            body = self.cipher.encrypt_message(
                {"t": _REKEY_LABEL, "state": "begin", "epoch": self.cipher.epoch,
                 "fresh": fresh})
            try:
                self._send_queue.put({"type": "enc", "body": body}, timeout=5.0)
            except queue.Full:
                self._rekey_outstanding = False
                self._pending_cipher = None
                self.close("发送队列拥塞, 已断开")
                return False
        return True

    def _resend_rekey_begin(self) -> None:
        """回执迟迟没到: 用同一个 fresh 重发 begin (幂等), 绝不自己单方面换密钥。"""
        with self._order_lock:
            if not self._rekey_outstanding or self._closed.is_set():
                return
            self._rekey_sent_at = time.time()
            try:
                body = self.cipher.encrypt_message(
                    {"t": _REKEY_LABEL, "state": "begin", "epoch": self._rekey_epoch,
                     "fresh": self._rekey_fresh})
                self._send_queue.put({"type": "enc", "body": body}, timeout=5.0)
            except queue.Full:
                pass

    def _finish_rekey(self) -> None:
        """这次轮换收尾 (必须在持锁时调用)。"""
        self._rekey_outstanding = False
        self._rekey_fresh = ""
        self._rekey_epoch = -1
        self._pending_cipher = None

    def _handle_rekey(self, message: Dict[str, Any]) -> None:
        """处理对方的密钥轮换通知 / 回执。"""
        state = message.get("state")
        epoch = message.get("epoch")
        fresh = str(message.get("fresh", ""))
        try:
            if state == "begin":
                if epoch == self.cipher.epoch - 1 and self._old_cipher is not None:
                    # 对方没收到回执又重发了一次: 用旧密钥把回执再发一遍 (幂等),
                    # 不然对方会一直等下去。
                    with self._order_lock:
                        body = self._old_cipher.encrypt_message(
                            {"t": _REKEY_LABEL, "state": "accept", "epoch": epoch,
                             "fresh": fresh})
                        self._send_queue.put({"type": "enc", "body": body}, timeout=5.0)
                    return
                if epoch != self.cipher.epoch:
                    return                      # 过期的轮换请求, 忽略
                with self._order_lock:
                    # 对方的轮换优先: 如果我自己也在申请, 放弃我自己的 —— 两边各用
                    # 各自的 fresh 派生会直接岔开密钥。
                    self._finish_rekey()
                    # 用旧密钥回执 (把对方给的 fresh 原样带回, 对方据此派生同一把新密钥),
                    # 然后自己立即切到新密钥 (旧密钥进入宽限期, 还能解在途消息)
                    body = self.cipher.encrypt_message(
                        {"t": _REKEY_LABEL, "state": "accept", "epoch": epoch,
                         "fresh": fresh})
                    self._send_queue.put({"type": "enc", "body": body}, timeout=5.0)
                    self._install_cipher(self.cipher.next_epoch(fresh, self.session_id))
            elif state == "accept":
                with self._order_lock:
                    if not self._rekey_outstanding or self._rekey_epoch != epoch:
                        return                  # 不是我要的那种回执
                    if fresh and fresh != self._rekey_fresh:
                        return                  # 回执里的随机数对不上, 忽略
                    new_cipher = self.cipher.next_epoch(fresh or self._rekey_fresh,
                                                        self.session_id)
                    self._finish_rekey()        # ← 关键: 收尾, 否则以后再也不轮换了
                    self._install_cipher(new_cipher)
        except queue.Full:
            pass
        except Exception as exc:  # noqa: BLE001
            self.close(f"密钥轮换失败: {exc}")

    @property
    def _is_rekey_leader(self) -> bool:
        """自动轮换只由一方发起 (按 peer_id 定, 两边算出来一定一致)。"""
        return self.cipher.local_peer < self.cipher.remote_peer

    def request_rotation(self, why: str = "") -> bool:
        """请求"立刻换一次密钥"(任意一方都能调用, 幂等)。

        用途: 传文件时给文件单独开一把密钥 —— 大块数据不该和聊天共用同一轮密钥。
        轮换只能由固定的"发起方"做 (两边同时发 begin 会各用各的 fresh, 密钥直接岔开),
        所以不是发起方的那一边就发一条 `rekey-request`, 由对方来发起。

        节流: 刚请求过、而且**密钥还没真的换掉**时不再重复请求; 换过之后 (epoch 变了)
        就可以立刻请求下一次 —— 传文件正好是"开始一次、结束一次"。
        """
        if self._closed.is_set() or not self.authenticated:
            return False
        now = time.time()
        if now - self._last_rotation_at < 0.4 and self.cipher.epoch == self._last_rotation_epoch:
            return False                    # 刚请求过、还没落地, 别连着催
        self._last_rotation_at = now
        self._last_rotation_epoch = self.cipher.epoch
        if self._is_rekey_leader:
            if self.rotate_keys():
                return True
            # 上一次轮换还没收尾 (小文件可能几十毫秒就传完): 稍后补一次, 别把
            # "传完再换一把"这件事悄悄丢掉
            self._schedule_rotation_retry()
            return False
        return self.send_message({"t": _REKEY_REQUEST, "why": why, "ts": time.time()})

    def _schedule_rotation_retry(self) -> None:
        if self._rotation_retry is not None:
            return
        timer = threading.Timer(0.7, self._rotation_retry_fire)
        timer.daemon = True
        self._rotation_retry = timer
        timer.start()

    def _rotation_retry_fire(self) -> None:
        self._rotation_retry = None
        if self._closed.is_set() or not self.authenticated:
            return
        self._last_rotation_at = 0.0        # 绕过节流, 这次是补发
        if self._is_rekey_leader:
            self.rotate_keys()

    def _maybe_auto_rekey(self) -> None:
        """按配置的条数自动轮换 (0 = 关闭)。"""
        if not self.rekey_every or self._closed.is_set() or not self.authenticated:
            return
        if self._rekey_outstanding:
            if time.time() - self._rekey_sent_at > REKEY_RETRY_SECONDS:
                # 只是重发请求, 不自己换密钥 (单方面换 = 和对方岔开)
                self._resend_rekey_begin()
            return
        if not self._is_rekey_leader:
            # 由对端负责发起: 两边都发 begin 会各用各的 fresh, 密钥就岔开了
            return
        if self._payload_count >= self.rekey_every:
            self.rotate_keys()

    def _looks_like_stale(self, body: Any) -> bool:
        """这个帧是不是"已经处理过的计数" (换密钥/乱序造成的重复)? """
        if not isinstance(body, dict):
            return False
        nonce = str(body.get("n", ""))
        if not nonce:
            return False
        if self.cipher.seen_before(nonce):
            return True
        for cipher in (self._old_cipher, self._pending_cipher):
            if cipher is not None and cipher.seen_before(nonce):
                return True
        return False

    def _decrypt_with_old_cipher(self, body: Any) -> Optional[Dict[str, Any]]:
        """轮换窗口里用"别的密钥"再试一次。

        要试两把:
          * `_old_cipher`: 接收方刚切到新密钥时, 对方用旧密钥发来的在途帧;
          * `_pending_cipher`: 我发起了轮换、对方已经切了, 但它回 accept 之前
            发来的帧已经是新密钥 —— 不试这把就会误判成"密文认证失败"然后断开。
        """
        if not isinstance(body, dict):
            return None
        old = self._old_cipher
        if old is not None and time.time() <= self._old_cipher_deadline:
            try:
                return old.decrypt_message(body)
            except DecryptError:
                pass
        pending = self._pending_cipher
        if pending is not None:
            try:
                return pending.decrypt_message(body)
            except DecryptError:
                pass
        return None

    def _install_cipher(self, new_cipher: SessionCipher) -> None:
        """切换会话密钥, 同时保留旧密钥一小段时间用于解密在途消息。"""
        self._old_cipher = self.cipher
        self._old_cipher_deadline = time.time() + self._old_window
        self.cipher = new_cipher
        self._payload_count = 0
        self.rekeys_done += 1

    def send_chat(self, text: str, seq: int = 0) -> bool:
        """发一条聊天消息。

        `seq` 是"我发给这个人的第几条"(从 1 开始, 只在内存里)。对方拿它来对账:
        重连后报"我收到第几条了", 我就能把缺的补发过去 (见 service 里的 history-*)。
        老版本对端忽略这个字段即可。
        """
        message = {"t": "chat", "text": text, "ts": time.time()}
        if seq > 0:
            message["seq"] = int(seq)
        return self.send_message(message)

    # -- 文件传输 ----------------------------------------------------------
    def send_file(self, path: str, on_progress: Optional[Callable[[OutgoingTransfer], None]] = None,
                  description: str = "") -> OutgoingTransfer:
        """只为文件发一条"请求"; 等对方同意后才开始推送分片。"""
        if not self.authenticated:
            raise TransferError("会话还没有通过好友确认")
        if not os.path.isfile(path):
            raise TransferError(f"文件不存在: {path}")
        size = os.path.getsize(path)
        transfer = OutgoingTransfer(
            transfer_id=uuid.uuid4().hex[:12],
            name=os.path.basename(path),
            path=path,
            size=size,
            sha256=file_sha256(path),
        )
        self.outgoing[transfer.transfer_id] = transfer
        self._progress_cb[transfer.transfer_id] = on_progress
        self.send_message({
            "t": "file-offer",
            "id": transfer.transfer_id,
            "name": transfer.name,
            "size": size,
            "sha256": transfer.sha256,
            "desc": description,
        })
        return transfer

    def start_transfer(self, transfer_id: str) -> bool:
        transfer = self.outgoing.get(transfer_id)
        if transfer is None or transfer.started:
            return False
        transfer.started = True
        threading.Thread(target=self._stream_file,
                         args=(transfer, self._progress_cb.get(transfer_id)),
                         name=f"file-{transfer.name}", daemon=True).start()
        return True

    def _stream_file(self, transfer: OutgoingTransfer,
                     on_progress: Optional[Callable[[OutgoingTransfer], None]]) -> None:
        try:
            with open(transfer.path, "rb") as fh:
                while transfer.sent < transfer.size and self.is_alive:
                    if transfer.transfer_id not in self.outgoing:
                        return  # 对方拒绝或已取消
                    chunk = fh.read(FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    # 分片内容和外层信封必须在同一把密钥下加密: 换密钥是在收线程里做的,
                    # 中间插进来的话就会出现"信封是新密钥、内容是旧密钥", 对端分片解不开。
                    with self._order_lock:
                        body = self.cipher.encrypt_chunk(chunk)
                        ok = self.send_message({"t": "file-chunk",
                                                "id": transfer.transfer_id, "body": body})
                    if not ok:
                        break
                    transfer.sent += len(chunk)
                    if on_progress:
                        on_progress(transfer)
            if self.is_alive and transfer.transfer_id in self.outgoing:
                self.send_message({"t": "file-end", "id": transfer.transfer_id})
        except OSError as exc:
            if self.is_alive:
                self.send_message({"t": "file-abort", "id": transfer.transfer_id, "reason": str(exc)})
        finally:
            self.outgoing.pop(transfer.transfer_id, None)
            self._progress_cb.pop(transfer.transfer_id, None)

    def accept_file(self, transfer_id: str, save_path: Optional[str] = None) -> bool:
        """同意接收: 打开目标文件并回复 file-accept。

        save_path 非空时保存到用户指定的位置 (「另存到别处…」), 否则用接收目录。
        目标已存在时自动改名, 不覆盖别人的文件。
        """
        pending = self._pending_offers.get(transfer_id)
        if pending is None:
            existing = self.incoming.get(transfer_id)
            if existing and existing.handle is not None:
                return self.send_message({"t": "file-accept", "id": transfer_id})
            return False
        if save_path:
            path = os.path.abspath(os.path.expanduser(save_path))
            directory = os.path.dirname(path) or "."
            if not os.path.isdir(directory):
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError as exc:
                    self.reject_file(transfer_id, f"无法创建目录: {exc}")
                    return False
            if os.path.exists(path):
                path = unique_download_path(directory, os.path.basename(path))
        else:
            path = unique_download_path(self._incoming_dir, pending.name)
        try:
            pending.path = path
            pending.name = os.path.basename(path)
            pending.handle = open(path, "wb")
        except OSError as exc:
            self.reject_file(transfer_id, f"无法写入文件: {exc}")
            return False
        self._pending_offers.pop(transfer_id, None)
        self.incoming[transfer_id] = pending
        accepted = self.send_message({"t": "file-accept", "id": transfer_id})
        if accepted:
            # 文件单独开一把密钥: 传大块数据不该和聊天共用同一轮密钥。
            # (发起方立刻换; 不是发起方就请对方换, 见 request_rotation)
            self.request_rotation("file-start")
        return accepted

    def reject_file(self, transfer_id: str, reason: str = "对方拒绝接收") -> bool:
        self._pending_offers.pop(transfer_id, None)
        return self.send_message({"t": "file-reject", "id": transfer_id, "reason": reason})

    def cancel_file(self, transfer_id: str, reason: str = "已取消") -> bool:
        transfer = self.outgoing.pop(transfer_id, None)
        if transfer is None:
            return False
        return self.send_message({"t": "file-abort", "id": transfer_id, "reason": reason})

    @property
    def pending_offers(self) -> Dict[str, IncomingTransfer]:
        return dict(self._pending_offers)

    # -- 收消息 ------------------------------------------------------------
    def _read_loop(self) -> None:
        reader = protocol.FrameReader(self.sock)
        try:
            while not self._closed.is_set():
                try:
                    frame = reader.read_frame(timeout=1.0)
                except socket.timeout:
                    continue
                except protocol.ConnectionClosed as exc:
                    self.close(str(exc))
                    return
                except protocol.ProtocolError as exc:
                    self.close(f"协议错误: {exc}")
                    return
                self.last_activity = time.time()
                self._dispatch(frame)
        finally:
            self.close(self._close_reason or "对端断开")

    def _dispatch(self, frame: Dict[str, Any]) -> None:
        ftype = frame.get("type")
        if ftype == "enc":
            body = frame.get("body")
            if not isinstance(body, dict):
                self.close("收到非法加密帧")
                return
            try:
                message = self.cipher.decrypt_message(body)
            except DecryptError as exc:
                message = self._decrypt_with_old_cipher(body)
                if message is None:
                    # 先判断是不是"换密钥瞬间的重复/过期帧": 这种直接丢掉, 保持连接。
                    # 只有既解不开、又无法解释来源的帧才当成安全问题断开。
                    if self._looks_like_stale(body):
                        return
                    self.close(f"密文校验失败: {exc}")
                    return
            self._handle_message(message)
        elif ftype == "ping":
            self.send_control("pong")
        elif ftype == "pong":
            pass
        else:
            self.close(f"收到未知帧类型 {ftype!r}")

    def send_control(self, kind: str) -> None:
        """发送不加密的控制帧 (只用于 ping/pong, 不含任何隐私内容)。

        必须和加密帧走**同一个发送队列 / 同一个写线程**, 否则控制帧会插到排队中的
        文件分片中间, 对端就会收到乱序密文 (会话的 nonce 计数器会直接判为重放并丢弃)。
        """
        if self._closed.is_set():
            return
        try:
            self._send_queue.put_nowait({"type": kind})
        except queue.Full:
            pass  # 队列满说明正忙, 丢掉这次心跳即可 (另一侧有超时检测)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == _REKEY_LABEL:
            # 密钥轮换是会话层自己的事, 不往上报
            self._handle_rekey(message)
            return
        if kind == _REKEY_REQUEST:
            # 对方想换一次密钥 (传文件前后各一次): 只有"发起方"能真的换。
            # 会收到这条就说明发起方是**我**(对方不是发起方才需要来求我), 直接换即可。
            if self._is_rekey_leader and self.authenticated:
                self.rotate_keys()
            return
        self._payload_count += 1
        self._maybe_auto_rekey()
        if kind == "file-chunk":
            self._handle_chunk(message)
            return
        if kind == "file-offer":
            self._handle_offer(message)
        if self._on_message:
            self._on_message(self, message)

    def _handle_offer(self, message: Dict[str, Any]) -> None:
        transfer_id = str(message.get("id", ""))
        if not transfer_id:
            return
        try:
            size = int(message.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        transfer = IncomingTransfer(
            transfer_id=transfer_id,
            name=os.path.basename(str(message.get("name", "file.bin"))) or "file.bin",
            size=size,
            sha256=str(message.get("sha256", "")),
        )
        self._pending_offers[transfer_id] = transfer
        if self.auto_accept_files and self.authenticated:
            self.accept_file(transfer_id)

    def _decrypt_chunk_with_grace(self, body: Any) -> bytes:
        """解一个文件分片; 换密钥瞬间在途的分片要用旧密钥再试一次。

        外层信封在 :meth:`_dispatch` 里有"旧密钥宽限期"兜底, 分片内容是**二次加密**的,
        必须同样兜底 —— 否则换密钥时正在传输的大文件会在这里解不开, 直接被判成
        "分片校验失败"并中断传输 (踩过这个坑)。
        """
        try:
            return self.cipher.decrypt_chunk(body)
        except DecryptError:
            old = self._old_cipher
            if old is not None and time.time() <= self._old_cipher_deadline:
                try:
                    return old.decrypt_chunk(body)
                except DecryptError:
                    pass
            raise

    def _handle_chunk(self, message: Dict[str, Any]) -> None:
        transfer_id = str(message.get("id", ""))
        transfer = self.incoming.get(transfer_id)
        if transfer is None or transfer.handle is None:
            return  # 没同意接收的分片直接丢弃
        body = message.get("body")
        if not isinstance(body, dict):
            return
        try:
            chunk = self._decrypt_chunk_with_grace(body)
        except DecryptError as exc:
            self._fail_transfer(transfer, f"分片校验失败: {exc}")
            return
        try:
            transfer.handle.write(chunk)
        except OSError as exc:
            self._fail_transfer(transfer, f"写入失败: {exc}")
            return
        transfer.hasher.update(chunk)
        transfer.received += len(chunk)

    def _fail_transfer(self, transfer: IncomingTransfer, reason: str) -> None:
        transfer.close()
        self.incoming.pop(transfer.transfer_id, None)
        self.send_message({"t": "file-abort", "id": transfer.transfer_id, "reason": reason})

    def finish_transfer(self, transfer_id: str) -> Optional[IncomingTransfer]:
        """收到 file-end 后调用: 关文件、算 SHA256、给出校验结果。"""
        transfer = self.incoming.pop(transfer_id, None)
        if transfer is None:
            transfer = self._pending_offers.pop(transfer_id, None)
        if transfer is None:
            return None
        transfer.close()
        digest = transfer.hasher.hexdigest()
        transfer.actual_sha256 = digest
        transfer.verified = (not transfer.sha256) or (digest == transfer.sha256)
        # 文件收完了: 立刻再换一把密钥, 把"传文件用的那一轮"退役掉
        self.request_rotation("file-end")
        return transfer

    def _write_loop(self) -> None:
        try:
            while True:
                item = self._send_queue.get()
                if item is _SENTINEL:
                    break
                try:
                    with self._write_lock:
                        protocol.send_frame(self.sock, item)
                except (OSError, protocol.ProtocolError):
                    break
        finally:
            self.close(self._close_reason or "发送失败, 连接中断")

    def _heartbeat_loop(self) -> None:
        while not self._closed.wait(HEARTBEAT_INTERVAL):
            if time.time() - self.last_activity > HEARTBEAT_INTERVAL * 4:
                self.close("心跳超时")
                return
            self.send_control("ping")

    def describe(self) -> str:
        return (f"{self.peer_name}@{self.host} 会话 {self.session_id} "
                f"({self.cipher.sent_messages}↑/{self.cipher.received_messages}↓)")


# 未通过好友确认的会话只允许这些消息类型
_PENDING_ALLOWED = {"friend-request", "friend-accept", "friend-accept-ack",
                    "friend-reject", "friend-removed", "friend-cancel",
                    "friend-unblocked", "friend-unblocked-ack",
                    "question-challenge", "question-answer", "question-result", "rekey-pref"}
