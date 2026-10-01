"""别名入口: 文件名写成 chatgui.py / chat_gui.py 都能启动。

（主入口是 chat_gui.py; 这个文件只是方便记不住下划线的场合。）

用法:
    python chatgui.py
    python chatgui.py --name 小明
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lanchat.console import configure_console  # noqa: E402

configure_console()  # Windows 中文控制台默认 GBK, 不切换会因打印特殊字符而崩溃

from chat_gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
