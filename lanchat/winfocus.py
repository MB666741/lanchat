"""把窗口真正带到前台 (Windows 专用, 其它平台用普通 Tk 调用)。

为什么需要这个
--------------
Windows 有"防止抢焦点"机制 (foreground lock): 一个不是当前前台进程的程序调用
``SetForegroundWindow`` 时, 系统只会让任务栏按钮闪一下, 窗口仍然待在别的窗口后面。
从 IDE、批处理脚本、计划任务、自动化工具里启动 GUI 时最常见, 表现就是
**"程序运行了, 但是看不到窗口"** —— 用户会以为程序没启动。

这里的做法 (和很多启动器相同的套路):
    1. 先 ``SetWindowPos`` 把窗口抬到 Z 序顶部 (不抢焦点, 不违反前台锁);
    2. 通过 ``AllowSetForegroundWindow`` + ``AttachThreadInput`` 拿到把窗口设为
       前台的资格, 再 ``SetForegroundWindow``;
    3. 用 ``FlashWindowEx`` 闪任务栏按钮, 万一还是没到前台, 用户也能注意到。
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Any, Dict, List

_IS_WINDOWS = sys.platform == "win32"

# 给每个窗口排的延后任务 (窗口销毁前要取消, 见 cancel_pending)
_PENDING: Dict[int, List[Any]] = {}

# --- Win32 API 声明 (只在 Windows 上加载) ----------------------------------
if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    _FLASHW_ALL = 0x00000003
    _FLASHW_TIMERNOFG = 0x0000000C
    _SWP_NOSIZE = 0x0001
    _SWP_NOMOVE = 0x0002
    _SWP_NOACTIVATE = 0x0010
    _HWND_TOPMOST = -1
    _HWND_NOTOPMOST = -2


def _window_handle(window: tk.Misc) -> int:
    """取 Tk 窗口的 Win32 HWND。"""
    try:
        window.update_idletasks()
        return int(window.winfo_id())
    except (tk.TclError, ValueError):
        return 0


def _attach_to_foreground() -> int:
    """附加到当前前台窗口的输入线程, 换来"可以把窗口设成前台"的资格。"""
    foreground = _user32.GetForegroundWindow()
    thread_id = _user32.GetWindowThreadProcessId(foreground, None)
    current = _kernel32.GetCurrentThreadId()
    attached = 0
    if thread_id and thread_id != current:
        if _user32.AttachThreadInput(thread_id, current, True):
            attached = thread_id
    return attached


def _detach(attached: int) -> None:
    if attached:
        _user32.AttachThreadInput(attached, _kernel32.GetCurrentThreadId(), False)


def force_foreground(window: tk.Misc) -> bool:
    """尽最大努力把窗口带到最前面并拿到焦点。返回是否成功。"""
    handle = _window_handle(window)
    if not handle:
        return False
    if not _IS_WINDOWS:
        try:
            window.lift()
            window.focus_force()
            return True
        except tk.TclError:
            return False

    ok = False
    attached = 0
    try:
        _user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        # 1) 抬到 Z 序顶部 (不激活, 不受前台锁限制)
        _user32.SetWindowPos(handle, _HWND_TOPMOST, 0, 0, 0, 0,
                             _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        _user32.BringWindowToTop(handle)
        # 2) 真正激活
        _user32.ShowWindow(handle, 5)  # SW_SHOW
        _user32.SetForegroundWindow(handle)
        _user32.SetActiveWindow(handle)
        attached = _attach_to_foreground()
        _user32.SetForegroundWindow(handle)
        try:
            window.focus_force()
        except tk.TclError:
            pass
        ok = _user32.GetForegroundWindow() == handle
        # 3) 任务栏闪一下, 保证用户能注意到
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), handle,
                           _FLASHW_ALL | _FLASHW_TIMERNOFG, 6, 0)
        _user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # noqa: BLE001 - 提不到前台也不能影响程序运行
        ok = False
    finally:
        _detach(attached)
    return ok


def drop_topmost(window: tk.Misc) -> None:
    """取消置顶 (不然窗口会一直压着别的窗口, 很讨厌)。"""
    if not _IS_WINDOWS:
        return
    handle = _window_handle(window)
    if not handle:
        return
    try:
        _user32.SetWindowPos(handle, _HWND_NOTOPMOST, 0, 0, 0, 0,
                             _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001
        pass


def bring_to_front(window: tk.Misc, hold_topmost_ms: int = 2500) -> bool:
    """把窗口带前台: 立即尝试一次, 300ms 后再试一次, 并短暂置顶。

    返回最后一次尝试是否成功把窗口设为前台 (拿不到前台也没关系, 任务栏会闪)。
    """
    try:
        window.deiconify()
    except tk.TclError:
        return False
    first = force_foreground(window)

    def _safe(action) -> None:
        def run() -> None:
            try:
                action(window)
            except Exception:  # noqa: BLE001 - 窗口可能已经关掉了
                pass
        return run

    try:
        jobs = _PENDING.setdefault(id(window), [])
        jobs.append(window.after(300, _safe(force_foreground)))
        if hold_topmost_ms > 0:
            jobs.append(window.after(hold_topmost_ms, _safe(drop_topmost)))
    except tk.TclError:
        pass
    return first


def cancel_pending(window: tk.Misc) -> None:
    """取消给这个窗口排的延后任务。

    必须在销毁窗口**之前**调用: Tkinter 销毁控件时会把回调命令一起删掉, 之后再触发
    那些 `after` 任务就会打出 `invalid command name "...<lambda>"` 这种 Tcl 噪音。
    """
    for job in _PENDING.pop(id(window), []):
        try:
            window.after_cancel(job)
        except Exception:  # noqa: BLE001
            pass
