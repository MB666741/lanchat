"""启动阶段日志 (带时间戳), 用来定位"运行了但没反应"卡在哪一步。

为什么不直接用 logging 模块: 启动早期可能连数据目录都还没确定,
这里要的是一个"绝不出错"的极简实现。
"""

from __future__ import annotations

from .i18n import t
import os
import sys
import time
import traceback

_start = time.time()
_log_file: str = ""


def _candidates() -> list:
    """日志候选位置: 优先命令行里的 --data-dir, 其次 ~/.lanchat, 最后脚本目录。"""
    out = []
    args = sys.argv[1:]
    for index, item in enumerate(args):
        if item in ("--data-dir", "-d") and index + 1 < len(args):
            out.append(args[index + 1])
        elif item.startswith("--data-dir="):
            out.append(item.split("=", 1)[1])
    try:
        out.append(os.path.join(os.path.expanduser("~"), ".lanchat"))
    except Exception:  # noqa: BLE001
        pass
    out.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lanchat"))
    return out


def log_file() -> str:
    global _log_file
    if _log_file:
        return _log_file
    for directory in _candidates():
        try:
            os.makedirs(directory, exist_ok=True)
            _log_file = os.path.join(directory, "last-run.log")
            return _log_file
        except OSError:
            continue
    _log_file = os.path.join(os.getcwd(), "last-run.log")
    return _log_file


def _stream_ok(stream) -> bool:
    try:
        stream.write("")
        stream.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def step(message: str) -> None:
    """记录一个启动步骤 (同时写控制台和日志文件)。"""
    elapsed = time.time() - _start
    line = f"[{time.strftime('%H:%M:%S')} +{elapsed:5.2f}s] {message}"
    try:
        if sys.stdout is not None and _stream_ok(sys.stdout):
            print(line, flush=True)
            # 子进程被强制结束 (terminate) 时块缓冲会丢内容, 这里再显式刷一次
            try:
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(log_file(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def dump_exception(context: str) -> str:
    """把异常详情写进日志并返回文本。"""
    detail = traceback.format_exc()
    step(t("!! {0} 失败").format(context))
    try:
        with open(log_file(), "a", encoding="utf-8") as fh:
            fh.write(detail + "\n")
    except OSError:
        pass
    return detail
