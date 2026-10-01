"""回归测试: 第一次启动 (不带 --name) 必须真的把窗口显示出来。

这是踩过的坑: 早先"设置昵称"用的是一个以 withdraw 掉的 root 为父窗口的 Toplevel,
它一直是 withdrawn (看不见), 主流程又在 wait_window 上死等 —— 现象就是
"运行了 chat_gui.py 但什么窗口都没有"。
这个脚本用子进程真实启动, 然后检查:
  1. 进程活着
  2. 该进程存在一个 **可见** 的顶层窗口
  3. 该窗口尺寸和位置合理 (不是 1x1, 不在屏幕外)
  4. 窗口标题不带 'tk' 默认名

直接运行:  python tests/test_startup_window.py
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import uuid
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat.console import configure_console  # noqa: E402

configure_console()

TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-startup")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _windows_of(pid: int) -> list:
    """列出该进程所有 **可见** 的顶层窗口: (hwnd, class, rect, title)。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 512)
            rect = _RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append((hwnd, cls.value, rect, title.value))
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found


def run_case(title: str, args: list, expect_visible_title: bool = True) -> None:
    print(title)
    data_dir = os.path.join(TMP, uuid.uuid4().hex[:8])
    cmd = [sys.executable, os.path.join(os.path.dirname(TMP), "chat_gui.py"),
           "--data-dir", data_dir, "--discovery-port", str(50990 + int(uuid.uuid4().int % 500))]
    cmd += args
    out_path = data_dir + ".out"
    err_path = data_dir + ".err"
    os.makedirs(data_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=os.path.dirname(TMP))
        try:
            visible = []
            deadline = time.time() + 20
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                visible = _windows_of(proc.pid)
                if visible:
                    break
                time.sleep(0.4)

            check("进程没有提前退出", proc.poll() is None,
                  f"退出码 {proc.poll()}" if proc.poll() is not None else "")
            check("存在一个可见的顶层窗口", bool(visible),
                  str([(w[1], w[3]) for w in visible]))
            if visible:
                # 看到窗口之后再等一下: 程序可能"先让窗口可见、再写日志", 立刻 terminate
                # 会把日志截断, 看起来像"没有任何显示记录" (多个测试并行跑时特别容易)
                time.sleep(1.5)
                _hwnd, cls, rect, win_title = visible[0]
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                check("窗口尺寸合理 (不是 1x1)", width > 200 and height > 150,
                      f"{width}x{height}")
                check("窗口在屏幕范围内",
                      rect.left > -50 and rect.top > -50, f"({rect.left},{rect.top})")
                if expect_visible_title:
                    check("窗口标题不是 Tk 默认名", win_title not in ("tk", ""),
                          repr(win_title))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
    with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
        text += fh.read()
    check("启动过程没有异常", "Traceback" not in text,
          text[-300:] if "Traceback" in text else "")
    markers = [line for line in text.splitlines()
               if "已显示" in line or "窗口应该已经出现" in line or "viewable=1" in line]
    check("日志里记录了窗口已显示", bool(markers), markers[-1] if markers else "没有任何显示记录")
    shutil.rmtree(data_dir, ignore_errors=True)
    for path in (out_path, err_path):
        try:
            os.remove(path)
        except OSError:
            pass
    print()


def run_self_test_case() -> None:
    """场景 4: --self-test 两个窗口必须能互相找到 (而且地址里不能是端口 0)。

    踩过的坑: 自测模式在 service.start() 之前登记对方地址, 那时端口还没绑定 (=0),
    结果两个窗口看得到对方、发请求却永远失败, 用户以为"对方那边没有提示来同意"。

    现在自测**优先靠真实广播发现**, 广播不通才兜底登记 —— 所以这行日志要等几秒,
    而且地址是广播里带的地址 (不一定是 127.0.0.1), 但端口必须是真实的。
    """
    print("场景 4: --self-test (两个窗口必须拿到真实端口并互相看见)")
    data_dir = os.path.join(TMP, uuid.uuid4().hex[:8])
    cmd = [sys.executable, os.path.join(os.path.dirname(TMP), "chat_gui.py"),
           "--data-dir", data_dir, "--discovery-port", str(51400 + int(uuid.uuid4().int % 300)),
           "--self-test"]
    out_path, err_path = data_dir + ".out", data_dir + ".err"
    os.makedirs(data_dir, exist_ok=True)
    registered: list = []

    def read_registered() -> list:
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
                return [line for line in handle.read().splitlines() if "看到的" in line]
        except OSError:
            return []

    with open(out_path, "w", encoding="utf-8") as out, open(err_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, cwd=os.path.dirname(TMP))
        try:
            visible: list = []
            deadline = time.time() + 25
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                visible = _windows_of(proc.pid)
                if len(visible) >= 2:
                    break
                time.sleep(0.4)
            check("两个窗口都显示出来了", len(visible) >= 2,
                  str([w[3] for w in visible]))
            check("窗口标题标明了甲/乙",
                  any("[自测 甲]" in w[3] for w in visible)
                  and any("[自测 乙]" in w[3] for w in visible),
                  str([w[3] for w in visible]))
            if len(visible) >= 2:
                time.sleep(1.5)          # 等日志落盘 (理由同上)
                rects = sorted(((w[2].left, w[2].right) for w in visible), key=lambda r: r[0])
                check("两个窗口并排、不互相压住",
                      rects[-1][0] >= rects[0][1] - 5, str(rects))
                # 自测现在是"先等广播发现 (最多 9 秒), 不行才兜底", 日志会晚一点出现
                deadline = time.time() + 30
                while time.time() < deadline:
                    registered = read_registered()
                    if len(registered) >= 2:
                        break
                    time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
    with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
        text += fh.read()
    registered = [line for line in text.splitlines() if "看到的" in line] or registered
    check("启动过程没有异常", "Traceback" not in text,
          text[-300:] if "Traceback" in text else "")
    check("日志里有两个实例互相看到的地址", len(registered) == 2, str(registered))
    # 只取 "= 地址" 那一段来判断端口 (整行里有时间戳, 别拿整行匹配 ":0")
    addresses = [line.rsplit("=", 1)[-1].strip().split()[0] for line in registered]
    check("地址是真实端口 (不是 0)",
          len(addresses) == 2 and all(":" in a and not a.endswith(":0")
                                      and "没有登记上" not in a for a in addresses),
          str(addresses))
    check("日志说明了这条记录是怎么来的 (广播发现 / 内部登记兜底)",
          len(registered) == 2 and all("来源:" in line for line in registered),
          str(registered))
    shutil.rmtree(data_dir, ignore_errors=True)
    for path in (out_path, err_path):
        try:
            os.remove(path)
        except OSError:
            pass
    print()


def main() -> int:
    if sys.platform != "win32":
        print("[SKIP] 这个检查依赖 Windows API")
        return 0
    print("=" * 62)
    print("启动窗口回归测试")
    print("=" * 62)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    try:
        run_case("场景 1: 不带 --name (第一次使用, 应先显示设置昵称界面)",
                 [], expect_visible_title=True)
        run_case("场景 2: 带 --name (直接进主界面)",
                 ["--name", "启动测试"], expect_visible_title=True)
        run_case("场景 3: --window-test 5 (只弹测试窗)",
                 ["--window-test", "5"], expect_visible_title=True)
        run_self_test_case()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
        return 1
    print("✅ 启动窗口回归测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
