"""加密与密钥协商 —— 自研的 "HTTPS 式" 握手。

与 HTTPS 的对应关系
-------------------
============================  ==========================================
HTTPS / TLS                   本工具
============================  ==========================================
服务器证书里的 ECDSA 密钥      启动时生成的 Ed25519 **长期身份密钥** (落盘保存)
握手时的 ECDHE 临时密钥        每次连接新生成的 X25519 **临时密钥对**
CertificateVerify 签名         对握手记录 (transcript) 的 Ed25519 签名
Finished / 密钥派生            HKDF-SHA256 派生 AES-256-GCM 会话密钥
证书指纹校验                    好友请求时展示并记录对方的密钥指纹
============================  ==========================================

关键性质
--------
* **前向保密**: 会话密钥来自临时 X25519 密钥, 握完就丢; 长期私钥泄露也解不开旧聊天记录。
* **身份绑定**: 双方对"两个临时公钥 + 双方身份卡 + 会话号"整体签名, 中途换掉任何一项签名都会失败。
* **抗重放**: 每次连接都是全新的临时密钥和随机数; 会话内还用递增计数器拒绝重放报文。
* **不信任未认证会话**: 握手完成但还没得到好友确认时, 会话标记为 PENDING,
  只允许传输"加好友请求"这类元数据, 不允许聊天内容。
"""

from __future__ import annotations

from .i18n import t
import hashlib
import hmac
import json
import os
import secrets
import socket
import time
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import protocol
from .constants import (
    ANSWER_KDF_ITERATIONS,
    ANSWER_NONCE_BYTES,
    ANSWER_SALT_BYTES,
    CIPHER_SUITE,
    COMPRESS_ENABLED,
    COMPRESS_MAX_BYTES,
    COMPRESS_MIN_BYTES,
    NONCE_PREFIX_BYTES,
    PAD_ENABLED,
    PAD_HEADROOM,
    PAD_MAX,
    PAD_RANDOM_EXTRA,
    PAD_STEP,
    PROTOCOL_VERSION,
    SESSION_ID_BYTES,
)
from .identity import IdentityCard, LocalIdentity, b64d, b64e, new_session_id, verify_detached

_LABEL = b"lanchat/v2"

# 允许的乱序/重放窗口大小: 计数落后于最高水位超过这个数就直接判为非法
_REPLAY_WINDOW = 4096


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class SecurityError(Exception):
    """加密/身份相关错误 (签名不对、密文被改、协议不符…)。"""


class HandshakeRejected(Exception):
    """对方明确拒绝了这次连接 (封禁 / 还没同意 / 版本不符)。

    code 是可选的机器可读原因 (`blocked` 等), 让服务层能区分"被封禁"和
    "一般连不上", 从而给用户一句说得清的提示。
    """

    def __init__(self, reason: str, code: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class DecryptError(SecurityError):
    """密文无法解密 (密钥不符、被篡改或重放)。"""


class SessionState(str, Enum):
    PENDING = "pending"                # 握手完成, 但对方身份尚未被确认 (仅允许好友请求)
    AUTHENTICATED = "authenticated"    # 对方是已知好友, 身份签名通过, 可正常聊天
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# 会话加解密 (AES-256-GCM, 双向独立密钥, 计数器 nonce)
# ---------------------------------------------------------------------------
class SessionCipher:
    """一条会话的对称加密器。

    * 方向分离: 发送密钥和接收密钥不同, 避免两边计数器撞车。
    * nonce = 4 字节随机会话前缀 + 8 字节递增计数器, 永不复用。
    * 附加认证数据 (AAD) 绑定双方 peer_id 与会话号, 防止密文被搬到别的会话里。
    """

    def __init__(self, send_key: bytes, recv_key: bytes, session_id: str,
                 local_peer: str, remote_peer: str,
                 role: str = "initiator", secret: Optional[bytes] = None) -> None:
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self.epoch = 0
        self.session_id = session_id
        self.local_peer = local_peer
        self.remote_peer = remote_peer
        self._role = role
        # 轮换时要用的"共享秘密": 双方都持有同一个值 (会话主密钥), 但不用于直接加密
        self._secret = secret
        self._send_prefix = os.urandom(NONCE_PREFIX_BYTES)
        self._send_counter = 0
        self._high_water = 0          # 已成功解密过的最大计数
        self._seen: set = set()       # 仍在重放窗口内的计数
        self._aad = session_aad(session_id, local_peer, remote_peer)

    # -- 统计 (调试/测试用) --------------------------------------------
    @property
    def sent_messages(self) -> int:
        return self._send_counter

    @property
    def received_messages(self) -> int:
        return self._high_water

    def seen_before(self, nonce_b64: str) -> bool:
        """这个 nonce 计数是否已经处理过?

        用途: 换密钥/乱序时可能有重复帧到达, 这种帧应当**静默丢弃**而不是断开连接;
        真正无法判断来源的（可能被篡改的）帧才需要断开。
        """
        try:
            nonce = b64d(nonce_b64)
        except Exception:  # noqa: BLE001
            return False
        if len(nonce) < NONCE_PREFIX_BYTES + 8:
            return False
        counter = int.from_bytes(nonce[NONCE_PREFIX_BYTES:], "big")
        return counter in self._seen or (self._high_water and counter <= self._high_water)

    def _next_nonce(self) -> bytes:
        self._send_counter += 1
        return self._send_prefix + self._send_counter.to_bytes(8, "big")

    def encrypt_bytes(self, raw: bytes) -> Dict[str, str]:
        nonce = self._next_nonce()
        blob = self._send.encrypt(nonce, raw, self._aad)
        return {"n": b64e(nonce), "c": b64e(blob)}

    def decrypt_bytes(self, payload: Dict[str, Any]) -> bytes:
        try:
            nonce = b64d(str(payload["n"]))
            blob = b64d(str(payload["c"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecryptError(t("密文格式非法: {0}").format(exc)) from exc
        if len(nonce) < NONCE_PREFIX_BYTES + 8:
            raise DecryptError(t("nonce 长度非法"))
        counter = int.from_bytes(nonce[NONCE_PREFIX_BYTES:], "big")
        if counter <= 0:
            raise DecryptError(t("nonce 计数非法"))
        if counter in self._seen:
            raise DecryptError(t("检测到重放报文, 已丢弃"))
        try:
            raw = self._recv.decrypt(nonce, blob, self._aad)
        except InvalidTag as exc:
            raise DecryptError(t("密文认证失败 (被篡改或密钥不符)")) from exc
        # 只有解密成功才记入窗口, 避免伪造报文把计数顶高
        self._seen.add(counter)
        if counter > self._high_water:
            self._high_water = counter
        if len(self._seen) > _REPLAY_WINDOW:
            cutoff = self._high_water - _REPLAY_WINDOW
            self._seen = {value for value in self._seen if value > cutoff}
        return raw

    def encrypt_message(self, message: Dict[str, Any]) -> Dict[str, str]:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.encrypt_bytes(raw)

    def decrypt_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = self.decrypt_bytes(payload)
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DecryptError(t("明文不是合法 JSON: {0}").format(exc)) from exc
        if not isinstance(message, dict):
            raise DecryptError(t("明文必须是 JSON 对象"))
        message.pop("p", None)          # 去掉长度混淆用的填充 (见 pad_message)
        packed = message.get("z")
        if isinstance(packed, str) and packed:
            # 发送方压缩过 (见 seal_message): 还原成原始消息, 上层完全无感
            try:
                return json.loads(zlib.decompress(b64d(packed)).decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise DecryptError(t("压缩消息解不开: {0}").format(exc)) from exc
        return message

    def encrypt_chunk(self, raw: bytes) -> Dict[str, str]:
        """文件分片: 与聊天消息共用密钥, 但使用独立的递增计数器。"""
        return self.encrypt_bytes(raw)

    def decrypt_chunk(self, payload: Dict[str, Any]) -> bytes:
        return self.decrypt_bytes(payload)

    # -- 密钥轮换 (类似 TLS 的 KeyUpdate) --------------------------------
    def next_epoch(self, fresh_b64: str, sid: str) -> "SessionCipher":
        """用"共享秘密 + 双方新随机数"派生出下一轮的会话密钥。

        双方用同样的输入算同样的结果, 所以不需要传输任何密钥; 轮换后旧密钥立即作废,
        即使某一轮的密钥日后泄露, 也只能解开那一轮的消息。
        """
        try:
            fresh = b64d(fresh_b64)
        except Exception as exc:  # noqa: BLE001
            raise SecurityError(t("轮换随机数非法: {0}").format(exc)) from exc
        if len(fresh) < 16:
            raise SecurityError(t("轮换随机数太短"))
        secret = self._secret if self._secret else self._send_fallback_secret()
        send_key, recv_key = derive_rekey(secret, self.session_id, self.epoch, fresh, self._role)
        new_cipher = SessionCipher(send_key, recv_key, self.session_id,
                                   self.local_peer, self.remote_peer,
                                   role=self._role, secret=secret)
        new_cipher.epoch = self.epoch + 1
        return new_cipher

    def _send_fallback_secret(self) -> bytes:
        """没有显式共享秘密时的兜底 (仅用于测试构造的 SessionCipher)。"""
        return b"\x00" * 32


# ---------------------------------------------------------------------------
# 长度混淆: 让"这个包多大"说明不了什么
# ---------------------------------------------------------------------------
def _raw_json(obj: Any) -> bytes:
    """按发送时的同一套写法序列化 (不加排序, 保证长度算得准)。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def pad_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """给要加密的消息加一段**随机长度**的填充 (`p` 字段)。

    思路是"每次发送都随机插一段", 而不是把所有消息都塞成同一个大小:
      * 先补齐到 PAD_STEP 的整数倍 (桶) —— 保证"3 个字的闲聊"和"半屏文字"落在同一档;
      * **桶之上再随机插 0 ~ 一整个步长**（`PAD_RANDOM_EXTRA`）—— 于是同一条消息
        每次发送的密文长度都不一样, 不会出现"这串长度总是一样, 认出来就是它"。

    填充内容来自 `os.urandom`, 和正文一起被 AEAD 加密 —— 既不可预测, 被改过也过不了校验。
    **只在明文小于 PAD_MAX 时补**: 文件分片那种大块补了也没意义。

    注意: 这只能盖住"每个包多大", 盖不住"总共传了多少、什么时候传的"。
    """
    if not PAD_ENABLED or PAD_STEP <= 0 or not isinstance(message, dict):
        return message
    payload = dict(message)
    base = len(_raw_json({**payload, "p": ""}))
    if base >= PAD_MAX:
        return payload
    target = (base + PAD_HEADROOM + PAD_STEP - 1) // PAD_STEP * PAD_STEP
    if PAD_RANDOM_EXTRA:
        target += secrets.randbelow(PAD_STEP)                 # 每次都不一样
    size = max(1, (target - base) * 3 // 4)                   # base64 大约 4/3 膨胀
    for _ in range(4):                                        # 微调, 落到目标附近
        payload["p"] = b64e(os.urandom(size))
        current = len(_raw_json(payload))
        if current >= target:
            break
        size += max(1, (target - current) * 3 // 4)
    return payload


def seal_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """发送前的最后一道加工: **先按需压缩, 再做长度混淆**。

    压缩: zlib 只在这条消息"确实变小"时才用 (太短的、本身就是二进制/高熵的会跳过),
    结果放进 `z` 字段, 外层仍保留 `t`, 所以收包方照旧按 `t` 分派。
    **顺序很重要**: 先压缩再补齐 —— 反过来的话填充的随机字节会把压缩率毁掉。

    安全上说明一下: 这里是**逐条消息独立压缩**, 不存在 CRIME/BREACH 那种
    "攻击者注入的内容和秘密在同一段被反复压缩" 的条件 (一条消息只由一方写),
    所以不会因为压缩长度而泄露别的内容。解密侧会自动还原 (见 `decrypt_message`)。
    """
    if not isinstance(message, dict):
        return message
    payload: Dict[str, Any] = dict(message)
    if COMPRESS_ENABLED:
        raw = _raw_json(message)
        if COMPRESS_MIN_BYTES <= len(raw) <= COMPRESS_MAX_BYTES:
            packed = zlib.compress(raw, 6)
            # 只有"压缩 + base64 之后确实更小"才用, 免得小消息反而变大
            if len(b64e(packed)) + 24 < len(raw):
                payload = {"t": message.get("t", ""), "z": b64e(packed)}
    return pad_message(payload)


# ---------------------------------------------------------------------------
# 握手报文
# ---------------------------------------------------------------------------
def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def transcript_digest(hello_i: Dict[str, Any], hello_r: Dict[str, Any]) -> bytes:
    """握手记录摘要: 把双方身份卡和临时公钥都绑进来, 中途替换任何一项都会验签失败。"""
    body = {
        "v": hello_i.get("v"),
        "suite": hello_i.get("suite"),
        "sid": hello_i.get("sid"),
        "I": {"card": hello_i.get("card"), "x": hello_i.get("x"), "nonce": hello_i.get("nonce")},
        "R": {"card": hello_r.get("card"), "x": hello_r.get("x"), "nonce": hello_r.get("nonce")},
    }
    return hashlib.sha256(_canonical(body)).digest()


def _derive_keys(shared: bytes, sid: str, digest: bytes, initiator_is_me: bool) -> Tuple[bytes, bytes]:
    """HKDF-SHA256 派生双向会话密钥 (每次握手的结果都不同)。"""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=hashlib.sha256(sid.encode("ascii") + digest).digest(),
        info=_LABEL + b"/session-keys",
    )
    material = hkdf.derive(shared)
    i_to_r, r_to_i = material[:32], material[32:]
    return (i_to_r, r_to_i) if initiator_is_me else (r_to_i, i_to_r)


def session_aad(session_id: str, peer_a: str, peer_b: str) -> bytes:
    """附加认证数据: 绑定会话号与双方身份。

    双方身份按字典序排列, 保证两边算出来的 AAD 完全一致 (否则 AAD 不同会导致解密失败)。
    """
    first, second = sorted([peer_a, peer_b])
    return f"{session_id}|{first}|{second}".encode("utf-8")


def derive_rekey(secret: bytes, session_id: str, epoch: int, fresh: bytes,
                 role: str) -> Tuple[bytes, bytes]:
    """密钥轮换: 从"秘密 + 双方新随机数"派生出下一轮的 (发送, 接收) 密钥。

    关键: **必须用双方共享的秘密**做 HKDF 输入, 并用"角色"区分方向, 否则两端算出的
    密钥对不上, 轮换后会话直接失效 (踩过这个坑)。
        role = "initiator" / "responder"
    双方各自把 i2r 那把当发送、r2i 那把当接收 (或反过来), 于是天然互补。
    """
    salt = hashlib.sha256(
        f"{session_id}|{epoch + 1}".encode("ascii") + b"|" + fresh
    ).digest()
    i_to_r = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                  info=_LABEL + b"/rekey/i2r").derive(secret)
    r_to_i = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                  info=_LABEL + b"/rekey/r2i").derive(secret)
    if role == "initiator":
        return i_to_r, r_to_i
    return r_to_i, i_to_r


# ---------------------------------------------------------------------------
# 加好友验证问题 (可选): 答案本身永远不上网, 每次提问只传一个一次性的证明
#
#   接收方(出题人): 保存 盐 + 密钥 K = PBKDF2(答案, 盐)
#                    (硬盘上只留 盐 + K 的 base64; 答案明文不落盘, 也不留在内存)
#   提问:            接收方发 题目 + 盐 + 随机数 nonce
#   回答:            回答方算 proof = HMAC(K, nonce) 发回来
#   校验:            接收方用自己保存的 K 算出同一个 proof 比对
#
# 为什么这样设计:
#   * 答案不上网, 线上只有题目和一个每次不同的 proof;
#   * 每次用新 nonce, 所以抓包也**无法重放**旧答案;
#   * 校验只需要 K, 所以程序重启后(没记住答案明文)依然能验证, 不会被误判成答错;
#   * 拿到 contacts.json 也只能拿 K 去猜答案 —— 每个候选要跑 20 万次 PBKDF2;
#   * 对方答错次数由接收方计数, 到上限自动封禁 (在线爆破直接封掉)。
# ---------------------------------------------------------------------------
def new_answer_salt() -> str:
    """生成验证问题的随机盐 (跟着问题一起发给回答方, 不算秘密)。"""
    return b64e(secrets.token_bytes(ANSWER_SALT_BYTES))


def new_answer_nonce() -> str:
    """每次提问都新生成一个随机数, 让答案证明只能用一次。"""
    return b64e(secrets.token_bytes(ANSWER_NONCE_BYTES))


def normalize_answer(text: str) -> str:
    """归一化答案: 去首尾空白、合并中间空白、转小写 (NFKC), 避免大小写空格导致误判。"""
    import unicodedata

    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def answer_hash(answer: str, salt_b64: str) -> str:
    """把答案经 PBKDF2 派生成 32 字节密钥, 以 base64 存起来 (用于展示/校验存在性)。

    真正的校验用 :func:`answer_proof_matches` —— 每次提问带一个随机数, 线上只传一次性证明。
    """
    return b64e(answer_key(answer, salt_b64))


def answer_key(answer: str, salt_b64: str) -> bytes:
    """把答案经 PBKDF2 派生为密钥 (20 万次迭代)。"""
    try:
        salt = b64d(salt_b64)
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("验证问题的盐非法: {0}").format(exc)) from exc
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ANSWER_KDF_ITERATIONS)
    return kdf.derive(normalize_answer(answer).encode("utf-8"))


def answer_proof(answer: str, salt_b64: str, nonce_b64: str) -> str:
    """回答方: 用答案算一次性的证明 (HMAC-SHA256(密钥, 随机数))。"""
    try:
        nonce = b64d(nonce_b64)
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("验证问题的随机数非法: {0}").format(exc)) from exc
    key = answer_key(answer, salt_b64)
    return b64e(hmac.new(key, b"lanchat/v2/answer-proof|" + nonce, hashlib.sha256).digest())


def answer_proof_matches(answer: str, salt_b64: str, nonce_b64: str, proof_b64: str) -> bool:
    """出题方: 用自己保存的答案验证对方提交的证明。"""
    if not proof_b64:
        return False
    try:
        expected = answer_proof(answer, salt_b64, nonce_b64)
    except SecurityError:
        return False
    return hmac.compare_digest(expected, proof_b64)


def answer_proof_with_key(key_b64: str, nonce_b64: str) -> str:
    """出题方: 直接用**已保存的派生密钥**算证明 (不需要答案明文)。

    这就是程序重启后还能校验的原因: 硬盘上存的就是 :func:`answer_hash` 返回的
    那个 K, 而答案明文既不落盘也不留在内存里。
    """
    try:
        key = b64d(key_b64)
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("验证问题的密钥非法: {0}").format(exc)) from exc
    try:
        nonce = b64d(nonce_b64)
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("验证问题的随机数非法: {0}").format(exc)) from exc
    return b64e(hmac.new(key, b"lanchat/v2/answer-proof|" + nonce, hashlib.sha256).digest())


def answer_proof_matches_key(key_b64: str, nonce_b64: str, proof_b64: str) -> bool:
    """出题方: 用保存的派生密钥比对对方提交的证明。"""
    if not proof_b64 or not key_b64:
        return False
    try:
        expected = answer_proof_with_key(key_b64, nonce_b64)
    except SecurityError:
        return False
    return hmac.compare_digest(expected, proof_b64)


def negotiate_rekey(mine: int, theirs: Optional[int]) -> int:
    """协商"每多少条消息换一次密钥" (双方必须算出同一个值)。

    规则 (写在 README 里, 两端一致):
      * 任意一方设为 0 (关闭) -> 这条会话就不自动轮换 (尊重"我不想轮换"的选择);
      * 否则取**较小**的那个 (更频繁 = 更保险, 也避免一方被另一方的宽松设置拖累);
      * 对方的 hello 里没带这个字段 (老版本) -> 用我自己的值。
    """
    mine = max(0, int(mine or 0))
    if theirs is None:
        return mine
    theirs = max(0, int(theirs))
    if mine == 0 or theirs == 0:
        return 0
    return min(mine, theirs)


def make_hello(identity: LocalIdentity, eph_pub_b64: str, nonce_b64: str,
               session_id: str, purpose: str, rekey_every: int = 0) -> Dict[str, Any]:
    return {
        "type": "hello",
        "v": PROTOCOL_VERSION,
        "suite": CIPHER_SUITE,
        "sid": session_id,
        "purpose": purpose,          # friend = 已知好友的连接; request = 加好友请求
        "rekey": max(0, int(rekey_every or 0)),   # 我希望的密钥轮换间隔 (0=关闭), 双方取更严格
        "card": identity.card().to_dict(),
        "x": eph_pub_b64,            # 本次连接的临时 X25519 公钥
        "nonce": nonce_b64,          # 32 字节随机数
        "ts": int(time.time()),
    }


def peer_rekey_of(hello: Dict[str, Any]) -> Optional[int]:
    """取对方 hello 里声明的轮换间隔 (没有这个字段的老版本返回 None)。"""
    value = hello.get("rekey")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def check_hello(hello: Dict[str, Any]) -> IdentityCard:
    """校验对方 hello 报文结构, 返回并验证其身份卡。"""
    if hello.get("type") != "hello":
        raise SecurityError(t("握手报文类型错误"))
    if int(hello.get("v", 0)) != PROTOCOL_VERSION:
        raise SecurityError(t("协议版本不一致 (对方 {0}, 本机 {1})").format(hello.get('v'), PROTOCOL_VERSION))
    if hello.get("suite") != CIPHER_SUITE:
        raise SecurityError(t("加密套件不一致: {0}").format(hello.get('suite')))
    if hello.get("purpose") not in ("friend", "request"):
        raise SecurityError(t("握手用途字段非法"))
    for field in ("sid", "x", "nonce"):
        value = hello.get(field)
        if not isinstance(value, str) or not value:
            raise SecurityError(t("握手报文缺少字段 {0}").format(field))
    try:
        if len(b64d(str(hello["x"]))) != 32 or len(b64d(str(hello["nonce"]))) != 32:
            raise ValueError(t("长度不对"))
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("临时公钥/随机数非法: {0}").format(exc)) from exc
    card_data = hello.get("card")
    if not isinstance(card_data, dict):
        raise SecurityError(t("对方没有附带身份卡"))
    try:
        return IdentityCard.from_dict(card_data)
    except ValueError as exc:
        raise SecurityError(t("对方身份卡非法: {0}").format(exc)) from exc


def _proof_payload(sid: str, digest: bytes, role: str) -> bytes:
    return _canonical({"sid": sid, "digest": b64e(digest), "role": role, "label": "lanchat/v2/proof"})


def make_proof(identity: LocalIdentity, sid: str, digest: bytes, role: str) -> Dict[str, Any]:
    return {"type": "proof", "sig": b64e(identity.sign(_proof_payload(sid, digest, role)))}


def check_proof(proof: Dict[str, Any], card: IdentityCard, sid: str, digest: bytes, role: str) -> None:
    if proof.get("type") != "proof":
        raise SecurityError(t("握手证明报文类型错误"))
    try:
        signature = b64d(str(proof.get("sig", "")))
    except Exception as exc:  # noqa: BLE001
        raise SecurityError(t("握手证明编码非法")) from exc
    if not verify_detached(card.ed_pub, signature, _proof_payload(sid, digest, role)):
        raise SecurityError(t("对方身份签名验证失败 (可能是中间人攻击或身份被替换)"))


@dataclass
class HandshakeResult:
    """握手结果。"""

    card: IdentityCard                 # 对方身份卡 (已验签)
    cipher: SessionCipher              # 会话加密器
    session_id: str
    state: SessionState                # AUTHENTICATED / PENDING
    verified: bool                     # 是否确认对方就是"我以为的那个人"
    expected: bool                     # 我们是否对其有身份预期 (即对方是不是好友)
    purpose: str                       # friend / request
    peer_rekey: Optional[int] = None   # 对方声明的密钥轮换间隔 (None = 老版本没带)
    rekey_every: int = 0               # 协商结果 (双方一致)


def _session_cipher(identity: LocalIdentity, peer_card: IdentityCard, shared: bytes,
                    sid: str, digest: bytes, initiator_is_me: bool) -> SessionCipher:
    send_key, recv_key = _derive_keys(shared, sid, digest, initiator_is_me)
    # 把"双方共享的 ECDH 秘密"也传给会话: 轮换密钥时要靠它, 但从不直接用于加密
    return SessionCipher(send_key, recv_key, sid, identity.peer_id, peer_card.peer_id,
                         role="initiator" if initiator_is_me else "responder",
                         secret=shared)


def _send_error(sock: socket.socket, reason: str, code: str = "") -> None:
    frame: Dict[str, Any] = {"type": "error", "reason": reason}
    if code:
        frame["code"] = code
    try:
        protocol.send_frame(sock, frame)
    except OSError:
        pass


def _read_frame(sock: socket.socket, timeout: float) -> Dict[str, Any]:
    try:
        return protocol.read_one_frame(sock, timeout=timeout)
    except protocol.ConnectionClosed as exc:
        raise HandshakeRejected(t("对方提前断开连接 ({0})").format(exc)) from exc


def _finish(identity: LocalIdentity, peer_card: IdentityCard, my_eph: X25519PrivateKey,
            peer_x_b64: str, sid: str, digest: bytes, initiator_is_me: bool,
            expected_peer: Optional[str], purpose: str,
            my_rekey: int = 0, peer_rekey: Optional[int] = None) -> HandshakeResult:
    peer_pub = X25519PublicKey.from_public_bytes(b64d(peer_x_b64))
    shared = my_eph.exchange(peer_pub)
    cipher = _session_cipher(identity, peer_card, shared, sid, digest, initiator_is_me)
    if expected_peer is not None and expected_peer != peer_card.peer_id:
        # 好友会话里对方换了身份 => 直接拒绝
        raise SecurityError(
            t("对方身份与好友记录不符 (对方指纹 {0})").format(peer_card.fingerprint)
        )
    expected = expected_peer is not None
    return HandshakeResult(
        card=peer_card,
        cipher=cipher,
        session_id=sid,
        state=SessionState.AUTHENTICATED if expected else SessionState.PENDING,
        verified=expected,
        expected=expected,
        purpose=purpose,
        peer_rekey=peer_rekey,
        rekey_every=negotiate_rekey(my_rekey, peer_rekey),
    )


# ---------------------------------------------------------------------------
# 握手: 发起方
# ---------------------------------------------------------------------------
def handshake_initiate(
    sock: socket.socket,
    identity: LocalIdentity,
    expected_peer: Optional[str],
    purpose: str = "friend",
    timeout: float = 10.0,
    rekey_every: int = 0,
) -> HandshakeResult:
    """主动连接方: 发 hello -> 收 hello -> 互相验签 -> 派生会话密钥。"""
    sid = new_session_id()
    my_eph = X25519PrivateKey.generate()
    my_x = b64e(my_eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw))
    my_nonce = b64e(secrets.token_bytes(32))
    my_hello = make_hello(identity, my_x, my_nonce, sid, purpose, rekey_every)

    protocol.send_frame(sock, my_hello)

    reply = _read_frame(sock, timeout)
    if reply.get("type") == "error":
        raise HandshakeRejected(str(reply.get("reason", t("对方拒绝连接"))),
                                code=str(reply.get("code", "") or ""))
    peer_card = check_hello(reply)
    if reply.get("sid") != sid:
        raise SecurityError(t("会话号不一致"))
    purpose_effective = purpose
    if purpose == "friend" and reply.get("purpose") != "friend":
        # 我以为对方是好友, 但对方并不认识我 -> 按加好友请求流程处理
        purpose_effective = "request"

    digest = transcript_digest(my_hello, reply)
    protocol.send_frame(sock, make_proof(identity, sid, digest, "initiator"))
    final = _read_frame(sock, timeout)
    if final.get("type") == "error":
        raise HandshakeRejected(str(final.get("reason", t("对方拒绝连接"))),
                                code=str(final.get("code", "") or ""))
    check_proof(final, peer_card, sid, digest, "responder")

    return _finish(identity, peer_card, my_eph, str(reply["x"]), sid, digest,
                   initiator_is_me=True, expected_peer=expected_peer,
                   purpose=purpose_effective, my_rekey=rekey_every,
                   peer_rekey=peer_rekey_of(reply))


# ---------------------------------------------------------------------------
# 握手: 接受方
# ---------------------------------------------------------------------------
def handshake_respond(
    sock: socket.socket,
    identity: LocalIdentity,
    expected_peer: Optional[str],
    purpose: str = "friend",
    timeout: float = 10.0,
    first_frame: Optional[Dict[str, Any]] = None,
    purpose_strict: bool = True,
    rekey_every: int = 0,
) -> HandshakeResult:
    """被动接受方。purpose: 我这边声明的用途 (friend / request)。

    first_frame: 调用方已经读到的对方 hello (避免重复读取)。
    purpose_strict: True = 对方声明的用途必须与我一致 (加密层默认行为);
        False = 容忍不一致。服务层用 False: "对方想建会话"这件事两边状态机
        可能不同步 (比如对方被我封禁后又被我解禁, 他重新申请时声明 request,
        而我这边已经是 request-in), 真正的授权只看好友名单, 不该因为用途字段
        对不上就永久连不上。
    rekey_every: 我希望的密钥轮换间隔; 与对方声明的值协商 (见 negotiate_rekey)。
    """
    hello = first_frame if first_frame is not None else _read_frame(sock, timeout)
    if hello.get("type") == "error":
        raise HandshakeRejected(str(hello.get("reason", t("对方拒绝连接"))),
                                code=str(hello.get("code", "") or ""))
    peer_card = check_hello(hello)
    if purpose_strict and hello.get("purpose") != purpose:
        _send_error(sock, t("用途不符 (对方声明 {0})").format(hello.get('purpose')))
        raise HandshakeRejected(t("用途不符: {0}").format(hello.get('purpose')))
    if expected_peer is not None and peer_card.peer_id != expected_peer:
        _send_error(sock, t("你的密钥与你好友列表里的身份不符"))
        raise SecurityError(t("对方 peer_id 与好友记录不符"))

    sid = str(hello["sid"])
    my_eph = X25519PrivateKey.generate()
    my_x = b64e(my_eph.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw))
    my_nonce = b64e(secrets.token_bytes(32))
    my_hello = make_hello(identity, my_x, my_nonce, sid, purpose, rekey_every)
    protocol.send_frame(sock, my_hello)

    digest = transcript_digest(hello, my_hello)

    proof = _read_frame(sock, timeout)
    if proof.get("type") == "error":
        raise HandshakeRejected(str(proof.get("reason", t("对方拒绝连接"))))
    try:
        check_proof(proof, peer_card, sid, digest, "initiator")
    except SecurityError as exc:
        _send_error(sock, t("身份签名验证失败"))
        raise SecurityError(str(exc)) from exc

    protocol.send_frame(sock, make_proof(identity, sid, digest, "responder"))
    return _finish(identity, peer_card, my_eph, str(hello["x"]), sid, digest,
                   initiator_is_me=False, expected_peer=expected_peer, purpose=purpose,
                   my_rekey=rekey_every, peer_rekey=peer_rekey_of(hello))


# ---------------------------------------------------------------------------
# 好友请求的独立签名 (与会话解耦, 便于单独校验和转发)
# ---------------------------------------------------------------------------
def request_signature_payload(from_card: IdentityCard, to_peer: str, ts: int,
                              message: str = "") -> bytes:
    return _canonical({
        "kind": "lanchat/v2/friend-request",
        "from": from_card.peer_id,
        "from_name": from_card.name,
        "to": to_peer,
        "ts": ts,
        "msg": message,
    })


def verify_request_signature(from_card: IdentityCard, to_peer: str, ts: int,
                             message: str, signature_b64: str) -> bool:
    try:
        signature = b64d(signature_b64)
    except Exception:  # noqa: BLE001
        return False
    return verify_detached(from_card.ed_pub, signature,
                           request_signature_payload(from_card, to_peer, ts, message))


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
