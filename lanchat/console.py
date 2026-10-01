"""控制台输出编码: 把 stdout/stderr 切到 UTF-8。

Windows 简体中文控制台默认用 GBK 编码。一旦往控制台打印 GBK 表示不了的字符
(⚠ ✅ ❌ 🔒 这类), Python 会抛 ``UnicodeEncodeError`` —— 这是一个**致命异常**:
程序会在打印那行的时候直接崩掉, 而且如果是双击运行/IDE 里运行, 报错一闪而过,
用起来就表现为"运行了但什么反应都没有"。

任何会往控制台打印非 ASCII 字符的入口 (chat_gui.py / chatgui.py / 测试脚本)
都应该在**最开头**调用一次 :func:`configure_console`。
"""

from __future__ import annotations

import sys


def configure_console() -> bool:
    """把 stdout/stderr 重新配置成 UTF-8 (无法配置时安全返回 False)。"""
    ok = False
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
            ok = True
        except Exception:  # noqa: BLE001 - 有些被重定向的流不支持 reconfigure
            pass
    return ok
