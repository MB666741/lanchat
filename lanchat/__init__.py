"""局域网聊天 / 文件传输工具 (LAN chat & file transfer)。

设计要点:
    * 启动时生成本机长期身份密钥对 (Ed25519) 并落盘保存
    * 每次连接都用 X25519 临时密钥协商会话密钥 (前向保密), AES-256-GCM 加密
    * UDP 广播自动发现同网段用户; 加好友需要对方同意, 可封禁/解禁

模块划分:
    constants.py   协议常量与默认配置
    protocol.py    NDJSON 帧收发
    identity.py    本机身份密钥、身份卡片与指纹
    crypto.py      HTTPS 式握手 (X25519 + Ed25519 + HKDF + AES-GCM)
    discovery.py   UDP 广播自动发现
    connection.py  单条加密会话 (收发/心跳/文件分片)
    service.py     总控: 发现 + 好友关系 + 多会话 + 文件传输
    gui.py         微信风格图形界面
"""

from .constants import DEFAULT_DISCOVERY_PORT, PROTOCOL_VERSION
from .identity import IdentityCard, LocalIdentity
from .service import ChatService, Contact, ContactState, EventKind, ServiceEvent

__all__ = [
    "ChatService",
    "Contact",
    "ContactState",
    "EventKind",
    "ServiceEvent",
    "IdentityCard",
    "LocalIdentity",
    "DEFAULT_DISCOVERY_PORT",
    "PROTOCOL_VERSION",
]

__version__ = "2.3.0"
