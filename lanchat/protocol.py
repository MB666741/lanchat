"""帧收发: 每条消息是一个 JSON 对象 + '\\n' (NDJSON)。

选择 NDJSON 而不是"4 字节长度前缀"的原因:
  * 便于调试 (tcpdump / 手写 telnet 都能看懂)
  * 加密后内容为 base64/hex, 不含换行, 不会破坏分帧
  * 实现简单, 不易出现粘包/半包错误

安全: 单帧长度硬上限, 读到超长行直接判定为恶意/异常并断开。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, Optional

from .constants import MAX_FRAME_BYTES


class ProtocolError(Exception):
    """对端违反协议 (超长帧 / 非法 JSON / 连接关闭)。"""


class ConnectionClosed(ProtocolError):
    """对端关闭了连接。"""


def encode_frame(obj: Dict[str, Any]) -> bytes:
    """把 dict 序列化成一行 UTF-8 JSON。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def send_frame(sock: socket.socket, obj: Dict[str, Any]) -> None:
    """线程安全地发送一帧。多个线程写同一 socket 时由 lock 串行化。"""
    data = encode_frame(obj)
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("待发送的帧超过大小上限")
    sock.sendall(data)


class FrameReader:
    """从 socket 读取 NDJSON 帧。每个连接由一个读线程独占。"""

    def __init__(self, sock: socket.socket, keepalive_buffer: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(keepalive_buffer)
        self._lock = threading.Lock()

    def read_frame(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """读取一帧; 超时抛 socket.timeout, 对端关闭抛 ConnectionClosed。"""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError(f"收到非法 JSON 帧: {exc}") from exc
                if not isinstance(obj, dict):
                    raise ProtocolError("帧必须是 JSON 对象")
                return obj

            with self._lock:
                self._sock.settimeout(timeout)
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    raise
                except OSError as exc:
                    raise ConnectionClosed(f"连接读取失败: {exc}") from exc
            if not chunk:
                raise ConnectionClosed("对端关闭了连接")
            self._buf.extend(chunk)
            if len(self._buf) > MAX_FRAME_BYTES:
                raise ProtocolError("单帧超过大小上限, 断开连接")


def read_one_frame(sock: socket.socket, timeout: Optional[float] = 10.0) -> Dict[str, Any]:
    """在握手阶段读取恰好一帧 (不保留多余缓冲)。"""
    reader = FrameReader(sock)
    return reader.read_frame(timeout=timeout)
