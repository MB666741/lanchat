"""本机身份密钥与身份卡片。

启动时自动生成**长期身份密钥对**并保存到磁盘 (类似 HTTPS 服务器证书里的密钥):

    Ed25519 密钥对  —— 身份签名 (证明"我真的是我")
    X25519  密钥对  —— 静态密钥协商 (目前作为身份一部分, 便于以后支持静态 ECDH)

身份卡片 (IdentityCard) 就是公开部分: peer_id + 昵称 + 两个公钥 + 自签名。
peer_id 由 Ed25519 公钥直接推导, 所以 **peer_id 本身不可伪造**;
指纹 (fingerprint) 是 peer_id 的可读形式, 用于人工比对身份。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from . import constants

_KEY_BYTES = 32


# ---------------------------------------------------------------------------
# 编解码 helper
# ---------------------------------------------------------------------------
def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def derive_peer_id(ed_pub_raw: bytes) -> str:
    """peer_id = SHA256(Ed25519 公钥) 的十六进制, 不可伪造。"""
    return "lc-" + hashlib.sha256(ed_pub_raw).hexdigest()[:32]


def fingerprint_of(peer_id: str) -> str:
    """把 peer_id 变成便于人工核对的指纹: AB3F-91C2-77DE-0A55。"""
    body = peer_id[3:] if peer_id.startswith("lc-") else peer_id
    groups = [body[i : i + 4].upper() for i in range(0, min(len(body), 16), 4)]
    return "-".join(groups)


@dataclass(frozen=True)
class IdentityCard:
    """一个人的公开身份信息 (可以随便传播, 不含任何私钥)。"""

    peer_id: str
    name: str
    ed_pub: str        # Ed25519 公钥 (base64)
    x_pub: str         # X25519 公钥 (base64)
    signature: str     # 用 Ed25519 私钥对自己内容的签名 (base64)
    created: float

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.peer_id)

    def payload(self) -> Dict[str, object]:
        """被签名的内容 (不含签名字段本身)。"""
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "ed_pub": self.ed_pub,
            "x_pub": self.x_pub,
            "created": self.created,
        }

    def to_dict(self) -> Dict[str, object]:
        data = self.payload()
        data["signature"] = self.signature
        return data

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "IdentityCard":
        card = IdentityCard(
            peer_id=str(data.get("peer_id", "")),
            name=str(data.get("name", "")),
            ed_pub=str(data.get("ed_pub", "")),
            x_pub=str(data.get("x_pub", "")),
            signature=str(data.get("signature", "")),
            created=float(data.get("created", 0.0)),
        )
        card.verify()
        return card

    def verify(self) -> None:
        """校验: 自签名有效, 且 peer_id 确实是公钥的哈希 (防伪造身份)。"""
        try:
            ed_raw = b64d(self.ed_pub)
            sig = b64d(self.signature)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"身份卡编码非法: {exc}") from exc
        if len(ed_raw) != _KEY_BYTES:
            raise ValueError("身份卡公钥长度非法")
        if derive_peer_id(ed_raw) != self.peer_id:
            raise ValueError("身份卡 peer_id 与公钥不匹配 (身份被伪造)")
        payload = json.dumps(self.payload(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        try:
            Ed25519PublicKey.from_public_bytes(ed_raw).verify(sig, payload)
        except InvalidSignature as exc:
            raise ValueError("身份卡签名无效") from exc
        try:
            x_raw = b64d(self.x_pub)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"X25519 公钥非法: {exc}") from exc
        if len(x_raw) != _KEY_BYTES:
            raise ValueError("X25519 公钥长度非法")


# ---------------------------------------------------------------------------
# 本机身份
# ---------------------------------------------------------------------------
class LocalIdentity:
    """本机长期身份。第一次运行时生成, 之后一直复用 (除非删除身份文件)。"""

    def __init__(self, peer_id: str, name: str, created: float,
                 ed_priv: Ed25519PrivateKey, x_priv: X25519PrivateKey) -> None:
        self.peer_id = peer_id
        self.name = name
        self.created = created
        self._ed_priv = ed_priv
        self._x_priv = x_priv
        self._lock = threading.RLock()
        self._card: Optional[IdentityCard] = None

    # -- 构造 / 持久化 --------------------------------------------------
    @staticmethod
    def generate(name: str) -> "LocalIdentity":
        ed_priv = Ed25519PrivateKey.generate()
        x_priv = X25519PrivateKey.generate()
        ed_raw = ed_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return LocalIdentity(derive_peer_id(ed_raw), name, time.time(), ed_priv, x_priv)

    @staticmethod
    def load_or_create(path: str, name: str) -> Tuple["LocalIdentity", bool]:
        """读取身份文件; 不存在或损坏则新建。返回 (身份, 是否新建)。"""
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                ed_priv = Ed25519PrivateKey.from_private_bytes(b64d(data["ed_priv"]))
                x_priv = X25519PrivateKey.from_private_bytes(b64d(data["x_priv"]))
                ed_raw = ed_priv.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
                )
                identity = LocalIdentity(
                    peer_id=str(data.get("peer_id") or derive_peer_id(ed_raw)),
                    name=str(data.get("name") or name),
                    created=float(data.get("created", time.time())),
                    ed_priv=ed_priv,
                    x_priv=x_priv,
                )
                if identity.peer_id != derive_peer_id(ed_raw):
                    raise ValueError("身份文件与公钥不匹配")
                return identity, False
            except Exception:  # noqa: BLE001 - 损坏就重建, 不让程序起不来
                backup = path + f".broken-{int(time.time())}"
                try:
                    os.replace(path, backup)
                except OSError:
                    pass
        identity = LocalIdentity.generate(name)
        identity.save(path)
        return identity, True

    def save(self, path: str) -> None:
        data = {
            "peer_id": self.peer_id,
            "name": self.name,
            "created": self.created,
            "ed_priv": b64e(self._ed_priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )),
            "x_priv": b64e(self._x_priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # -- 名称 ------------------------------------------------------------
    def set_name(self, name: str) -> None:
        with self._lock:
            self.name = name
            self._card = None

    # -- 卡片与签名 ------------------------------------------------------
    def card(self) -> IdentityCard:
        with self._lock:
            if self._card is None:
                ed_raw = self._ed_priv.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
                )
                x_raw = self._x_priv.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
                )
                draft = IdentityCard(
                    peer_id=self.peer_id,
                    name=self.name,
                    ed_pub=b64e(ed_raw),
                    x_pub=b64e(x_raw),
                    signature="",
                    created=self.created,
                )
                payload = json.dumps(draft.payload(), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")
                signature = b64e(self._ed_priv.sign(payload))
                self._card = IdentityCard(
                    peer_id=draft.peer_id, name=draft.name, ed_pub=draft.ed_pub,
                    x_pub=draft.x_pub, signature=signature, created=draft.created,
                )
            return self._card

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.peer_id)

    def sign(self, data: bytes) -> bytes:
        return self._ed_priv.sign(data)

    def public_x(self) -> bytes:
        return self._x_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )

    def private_x(self) -> X25519PrivateKey:
        return self._x_priv


def verify_detached(ed_pub_b64: str, signature: bytes, data: bytes) -> bool:
    """用给定公钥校验一段数据的签名。"""
    try:
        pub = Ed25519PublicKey.from_public_bytes(b64d(ed_pub_b64))
        pub.verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# 数据目录 (写不进去时自动降级, 保证程序永远能跑起来)
# ---------------------------------------------------------------------------
_DIR_LOCK = threading.Lock()
_DIR_CACHE: Dict[str, str] = {}


def data_dir_writable_warning() -> str:
    return _DIR_CACHE.get("warning", "")


def resolve_app_dir(preferred: Optional[str] = None) -> str:
    """返回可写的本机数据目录。

    优先 ~/.lanchat; 不可写时依次降级到「当前目录/.lanchat」「临时目录/.lanchat」。
    """
    with _DIR_LOCK:
        if preferred:
            candidates = [preferred]
        else:
            home = os.path.expanduser("~")
            import tempfile

            candidates = [
                os.path.join(home, constants.APP_DIR_NAME),
                os.path.join(os.getcwd(), constants.APP_DIR_NAME),
                os.path.join(tempfile.gettempdir(), constants.APP_DIR_NAME),
            ]
        errors = []
        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".write-test")
                with open(probe, "wb") as fh:
                    fh.write(b"ok")
                os.remove(probe)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if errors:
                _DIR_CACHE["warning"] = f"首选数据目录不可用 ({errors[0]}), 已改用: {path}"
            else:
                _DIR_CACHE["warning"] = ""
            _DIR_CACHE["path"] = path
            return path
        # 全部失败: 返回首选路径, 后续读写会各自报错
        _DIR_CACHE["warning"] = "找不到可写的数据目录: " + "; ".join(errors)
        return candidates[0]


def new_session_id() -> str:
    return uuid.uuid4().hex[:2 * constants.SESSION_ID_BYTES]


def local_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "未知主机"
