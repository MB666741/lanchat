"""入口: python chat_gui.py  ->  微信风格图形界面

这个入口负责"让双击能跑、出错看得见":
  1. 把脚本目录加进 sys.path —— 在任何目录下执行都不受影响;
  2. 把控制台输出切成 UTF-8 —— Windows 中文控制台默认 GBK, 打印 ⚠/✅/🔒 会抛
     UnicodeEncodeError 让程序在第一步就崩掉 (看起来就是"运行了没反应");
  3. 每个启动阶段都写一行带时间戳的日志到 <数据目录>/last-run.log, 卡在哪一步一目了然;
  4. --diagnose: 只做环境自检并打印报告, 不打开窗口。
"""

from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from lanchat.console import configure_console  # noqa: E402

configure_console()   # 必须最先做: 否则第一次打印特殊字符就会崩

from lanchat import startup_log  # noqa: E402

EXIT_NEEDS_ENV = 2


def log(message: str) -> None:
    startup_log.step(message)


def log_path() -> str:
    return startup_log.log_file()


def _fail(title: str, hints: list) -> int:
    print()
    print("=" * 62)
    print(f"启动失败: {title}")
    print("=" * 62)
    for hint in hints:
        print("  • " + hint)
    print()
    log(f"启动失败: {title} | " + " / ".join(hints))
    return EXIT_NEEDS_ENV


def check_environment() -> int:
    """启动前自检: 依赖与图形界面。返回 0 表示没问题。"""
    log(f"Python {sys.version.split()[0]} | {sys.executable}")
    log(f"脚本目录 {_HERE} | 工作目录 {os.getcwd()}")

    try:
        import tkinter
        log(f"tkinter OK (Tk {tkinter.TkVersion})")
    except ImportError as exc:
        return _fail(f"没有 tkinter 图形库 ({exc})", [
            "Windows/macOS 官方 Python 自带 tkinter; 这个提示说明装的是精简版 Python",
            "请到 python.org 重新安装 Python 并勾选 tcl/tk",
            "Linux 上执行: sudo apt install python3-tk",
        ])

    try:
        import cryptography

        log(f"cryptography {cryptography.__version__}")
    except ImportError as exc:
        return _fail(f"缺少 cryptography 库 ({exc})", [
            f"在本目录执行: {os.path.basename(sys.executable)} -m pip install -r requirements.txt",
            "或者只装这一个包: python -m pip install cryptography",
        ])
    return 0


def _diagnose_options(argv) -> dict:
    """从命令行里挑出 `--diagnose` 也关心的参数。

    不引第二套 argparse: 自检报告必须反映**程序实际会用**的路径/端口, 否则用户按
    `--data-dir X --diagnose` 跑, 报告里却写着默认目录, 会白排查半天 (踩过)。
    """
    out = {"data_dir": "", "download_dir": "", "discovery_port": ""}
    argv = list(argv or [])
    for index, item in enumerate(argv):
        for flag, key in (("--data-dir", "data_dir"),
                          ("--download-dir", "download_dir"),
                          ("--discovery-port", "discovery_port")):
            if item == flag and index + 1 < len(argv):
                out[key] = argv[index + 1]
            elif item.startswith(flag + "="):
                out[key] = item.split("=", 1)[1]
    return out


def _read_settings(data_dir: str) -> dict:
    """读一下 settings.json (不存在/坏了都当空处理, 自检不能因此崩掉)。"""
    import json

    try:
        with open(os.path.join(data_dir, "settings.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def diagnose(argv=None) -> int:
    """--diagnose: 只做检查并打印报告, 不打开窗口。"""
    print("=" * 62)
    print("局域网聊天工具 —— 环境自检")
    print("=" * 62)
    code = check_environment()
    if code != 0:
        return code

    import socket

    from lanchat.constants import DEFAULT_DISCOVERY_PORT
    from lanchat.discovery import broadcast_addresses, local_ipv4_addresses
    from lanchat.identity import LocalIdentity, fingerprint_of, resolve_app_dir

    options = _diagnose_options(argv)
    data_dir = (options["data_dir"] or "").strip() or resolve_app_dir()
    print(f"  数据目录 : {data_dir}")

    saved = _read_settings(data_dir)

    from lanchat.service import _prepare_dir, default_download_dir

    # 接收目录: 命令行 > 用户自己在设置里选过的 > 系统下载目录 (和启动时同一套规则)
    wanted = (options["download_dir"] or "").strip()
    if not wanted and saved.get("download_dir") and saved.get("download_dir_chosen"):
        wanted = str(saved["download_dir"])
    download_dir, warning = _prepare_dir(wanted or default_download_dir())
    print(f"  接收目录 : {download_dir}")
    if warning:
        print(f"             ⚠ {warning}")

    print(f"  本机地址 : {', '.join(local_ipv4_addresses()) or '未知'}")
    print(f"  广播地址 : {', '.join(broadcast_addresses())}")

    try:
        disc_port = int(options["discovery_port"] or 0) or DEFAULT_DISCOVERY_PORT
    except ValueError:
        disc_port = DEFAULT_DISCOVERY_PORT
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("", disc_port))
        print(f"  发现端口 : UDP {disc_port} 可用")
    except OSError as exc:
        print(f"  发现端口 : UDP {disc_port} 被占用 ({exc})")
        print(f"             已经开着程序属正常; 想再开一个可以加 --discovery-port {disc_port + 1}")
    finally:
        probe.close()

    listener = socket.socket()
    try:
        listener.bind(("", 0))
        print(f"  TCP 监听 : 可用 (随机端口示例 {listener.getsockname()[1]})")
    finally:
        listener.close()

    if saved:
        tcp_saved = saved.get("tcp_port", 0)
        hidden = saved.get("discoverable", True) is False
        print(f"  已存设置 : 本机端口 {tcp_saved or '随机(每次启动可能不同)'}"
              f" | 允许被自动搜索: {'否 (隐身中)' if hidden else '是'}")

    identity_path = os.path.join(data_dir, "identity.json")
    if os.path.isfile(identity_path):
        identity, created = LocalIdentity.load_or_create(identity_path, "临时")
        print(f"  身份密钥 : 已存在, 指纹 {fingerprint_of(identity.peer_id)}")
        if created:
            print("             ⚠ 原身份文件损坏, 已重新生成")
    else:
        print("  身份密钥 : 还没有, 首次启动时自动生成")

    # 真正建一个窗口看能不能显示 —— 区分"代码问题"和"环境显示不出窗口"
    print()
    print("  窗口测试 : 正在创建测试窗口…")
    try:
        import tkinter as _tk

        window = _tk.Tk()
        window.title("窗口测试")
        window.geometry("360x120+{}+{}".format(
            max(0, (window.winfo_screenwidth() - 360) // 2),
            max(0, (window.winfo_screenheight() - 120) // 3),
        ))
        _tk.Label(window, text="能看到这个窗口吗?", bg="#ffffff").pack(expand=True)
        window.update()
        window.update_idletasks()
        viewable = bool(window.winfo_viewable())
        window.destroy()
        if viewable:
            print("             窗口可以创建并显示, 图形环境正常")
        else:
            print("             ⚠ 窗口建出来了但没有显示 (可能被环境隐藏)")
    except Exception as exc:  # noqa: BLE001
        print(f"             ✗ 创建窗口失败: {type(exc).__name__}: {exc}")
        print("               这通常说明当前环境没有可用的图形界面")

    harness = {k: v for k, v in os.environ.items() if k.startswith("DSH_")}
    if harness:
        print()
        print("  ⚠ 当前终端带着自动化工具的环境变量 "
              f"({', '.join(sorted(harness))}), 图形界面在这里可能显示不出来。")
        print("     请换一个普通的 PowerShell / 命令提示符窗口, 或双击 run_chat.bat。")

    print()
    print("✅ 环境检查通过, 可以运行: python chat_gui.py")
    print("   只看窗口能不能弹出来: python chat_gui.py --window-test 15")
    print(f"   启动日志: {log_path()}")
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--diagnose" in args:
        return diagnose(args)

    log("=" * 58)
    log(f"启动: {' '.join(sys.argv)}")
    code = check_environment()
    if code != 0:
        return code

    log("准备打开界面…")
    try:
        from lanchat.gui import main as gui_main

        result = gui_main(args)
        log(f"界面已退出 (返回 {result})")
        return result
    except KeyboardInterrupt:
        log("用户中断退出")
        return 0
    except Exception:  # noqa: BLE001 - 界面起不来时把原因写下来
        detail = startup_log.dump_exception("启动界面")
        print()
        print("=" * 62)
        print("启动时发生异常 (详细信息已写入日志):")
        print("=" * 62)
        print(detail)
        print(f"日志文件: {log_path()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
