"""微信风格的图形界面。

布局:
    ┌───────────────────────────────────────────────────────────────┐
    │ 我: 小明 (AB3F-91C2…)      [➕添加联系人] [🔔新朋友] [⚙设置]   │
    ├───────────────────┬───────────────────────────────────────────┤
    │ 搜索框            │  小红的聊天窗口                            │
    │  小红  🔒  (2)    │   [12:01] 小红: 你好                       │
    │  阿强  ○          │   [12:01] 我: 你好呀                       │
    │                   │  [输入框                    ] [发送] [文件]│
    └───────────────────┴───────────────────────────────────────────┘

* 第一次启动: 先设置昵称 (直接画在主窗口里, 不会出现"看不见的对话框")。
* 「添加联系人」: 列出局域网中自动搜索到的人; 可以给对方**出验证问题**, 也可以回答
  对方给你出的问题 (答题不用先选中谁, 直接点『✋ 回答问题』)。
* 「新朋友」: 别人发来的加好友请求, 可 同意 / 拒绝 / 封禁。
* 对方同意后才建立加密会话; 每收发若干条消息自动**重新协商会话密钥**。
* `--self-test`: 同一台机器上开两个**完整的正常窗口**, 自己跟自己聊天。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

from .console import configure_console

# 单独运行这个文件 (python lanchat/gui.py) 时也要先把控制台切成 UTF-8,
# 否则打印 ⚠/✅/🔒 会抛 UnicodeEncodeError 让程序在启动第一步就挂掉。
configure_console()

try:
    from . import __version__, i18n
    from .i18n import t
    from .constants import DEFAULT_DISCOVERY_PORT, DEFAULT_MAX_ANSWER_ATTEMPTS, DEFAULT_TCP_PORT
    from .service import (
        ChatService, Contact, ContactState, EventKind, ServiceEvent, default_download_dir,
        human_size, split_manual_address,
    )
except ImportError:  # 允许直接 python lanchat/gui.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lanchat import __version__  # type: ignore
    from lanchat import i18n  # type: ignore
    from lanchat.i18n import t  # type: ignore
    from lanchat.constants import (  # type: ignore
        DEFAULT_DISCOVERY_PORT, DEFAULT_MAX_ANSWER_ATTEMPTS, DEFAULT_TCP_PORT,
    )
    from lanchat.service import (  # type: ignore
        ChatService, Contact, ContactState, EventKind, ServiceEvent, default_download_dir,
        human_size, split_manual_address,
    )

C = {
    "bg": "#f2f2f2",
    "sidebar": "#e8eaed",
    "sidebar_sel": "#d2e5d6",
    "panel": "#ffffff",
    "border": "#d8dade",
    "text": "#1f2329",
    "muted": "#8a9099",
    "green": "#07c160",
    "green_dark": "#06ad56",
    "warn": "#fa9d3b",
    "err": "#e64340",
}
def _pick_font_family() -> str:
    """按当前语言挑界面字体。

    英文/繁体各自用系统自带的对应字体: 用中文字体渲染英文会显得挤, 用英文字体渲染中文
    会掉字 (Windows 上 Tk 不会自动回退)。语言必须在 import 本模块之前定好 —— 入口
    chat_gui.py 就是这么做的 (见 i18n.set_language 的调用点)。
    """
    if sys.platform != "win32":
        return "Sans"
    families = i18n.font_families()
    return families[0] if families else "Sans"


FONT = (_pick_font_family(), 10)
FONT_BOLD = (FONT[0], 10, "bold")
FONT_SMALL = (FONT[0], 9)


def _btn(parent: tk.Misc, text: str, command, kind: str = "normal", width: int = 0) -> tk.Button:
    """扁平按钮 (ttk 按钮在 Windows 上不方便上色, 这里统一用 tk.Button)。"""
    colors = {
        "normal": (C["panel"], C["text"], C["border"]),
        "primary": (C["green"], "#ffffff", C["green_dark"]),
        "danger": (C["err"], "#ffffff", "#c33333"),
        "ghost": (C["sidebar"], C["text"], C["border"]),
    }[kind]
    bg, fg, active = colors
    button = tk.Button(
        parent, text=text, command=command, font=FONT_SMALL, bg=bg, fg=fg,
        activebackground=active, activeforeground=fg, relief="flat", bd=0,
        padx=10, pady=4, cursor="hand2", highlightthickness=0,
    )
    if width:
        button.configure(width=width)
    return button


# ===========================================================================
# 启动界面: 第一次打开时在主窗口里设置昵称
# ===========================================================================
class NicknameDialog:
    """第一次打开时设置昵称 —— 直接画在主窗口里。

    历史教训: 之前这里是一个以 "被 withdraw 掉的 root" 为父窗口的 Toplevel + transient,
    结果这个对话框一直是 withdrawn (不可见), 而主流程又在 wait_window 上死等它关闭,
    于是表现成"程序运行了但什么窗口都没有"。
    """

    def __init__(self, master: tk.Tk, service: ChatService,
                 on_done: Optional[Any] = None) -> None:
        self.master = master
        self.service = service
        self.on_done = on_done
        self.ok = False
        self._done = False

        master.deiconify()
        master.title(t("局域网聊天 —— 设置昵称"))
        master.geometry(_center_geometry(master, 560, 420))
        master.minsize(480, 360)
        try:
            master.protocol("WM_DELETE_WINDOW", self._cancel)
        except tk.TclError:
            pass

        self.frame = tk.Frame(master, bg=C["panel"], padx=26, pady=20)
        self.frame.pack(fill="both", expand=True)
        frame = self.frame

        tk.Label(frame, text=t("给自己起个昵称"), font=(FONT[0], 16, "bold"),
                 bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(frame, text=t("同一个局域网里的朋友会看到这个名字，用来分辨谁是谁。"),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"]).pack(anchor="w", pady=(2, 14))

        # 注意 master=master: 一个进程里可能同时存在两个 Tk (自测模式)! 不带 master 的
        # StringVar 会绑到"第一个" Tk 解释器上, 第二个窗口里输入框能打字、但 get() 永远
        # 是空字符串 —— 表现就是"明明输入了答案, 却提示没输入"。
        self.name_var = tk.StringVar(master=master,
                                     value="" if service.name == t("未命名用户") else service.name)
        entry = tk.Entry(frame, textvariable=self.name_var, font=(FONT[0], 13),
                         relief="flat", bg=C["bg"], insertbackground=C["text"])
        entry.pack(fill="x", ipady=7)
        entry.focus_set()

        info = tk.Frame(frame, bg=C["panel"])
        info.pack(fill="x", pady=(18, 0))
        tk.Label(info, text=t("本机身份指纹"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(anchor="w")
        tk.Label(info, text=service.fingerprint, font=("Consolas", 12, "bold"),
                 bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(info,
                 text=(t("""已在本机生成长期身份密钥对 (Ed25519) 并保存到:
{0}
每次连接都会用临时密钥协商出新的会话密钥, 聊天全程加密。""").format(os.path.join(service.data_dir, 'identity.json'))),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left").pack(anchor="w",
                                                                                     pady=(6, 0))

        buttons = tk.Frame(frame, bg=C["panel"])
        buttons.pack(fill="x", pady=(18, 0))
        _btn(buttons, t("开始使用"), self._confirm, "primary", width=10).pack(side="right")
        _btn(buttons, t("退出"), self._cancel, "ghost", width=6).pack(side="right", padx=(0, 8))

        master.bind("<Return>", lambda _e: self._confirm())
        master.bind("<Escape>", lambda _e: self._cancel())
        entry.focus_set()

        from . import startup_log

        master.update_idletasks()
        master.update()
        startup_log.step(t("昵称设置窗口已显示: state={0} viewable={1} geometry={2}").format(master.state(), master.winfo_viewable(), master.winfo_geometry()))
        _bring_to_front(master)

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        if self.on_done:
            try:
                self.on_done(self.ok)
            except Exception:  # noqa: BLE001
                pass

    def _confirm(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning(t("昵称"), t("请先填写昵称"), parent=self.master)
            return
        try:
            self.service.set_name(name)
        except ValueError as exc:
            messagebox.showwarning(t("昵称"), str(exc), parent=self.master)
            return
        self.ok = True
        self._finish()

    def _cancel(self) -> None:
        self.ok = False
        self._finish()

    def close(self) -> None:
        """清掉启动界面, 给主界面腾地方。"""
        try:
            self.master.unbind("<Return>")
            self.master.unbind("<Escape>")
        except tk.TclError:
            pass
        for widget in list(self.frame.winfo_children()):
            widget.destroy()
        self.frame.destroy()


def _close_toplevel(window: tk.Misc) -> None:
    """关掉一个弹窗: 先取消给它排的延后任务, 再销毁。

    顺序很重要 —— 直接 destroy 的话, 那些 `after` 回调会在窗口销毁后再触发,
    控制台里就会冒出 `invalid command name "...<lambda>"` 的 Tcl 噪音。
    """
    from .winfocus import cancel_pending

    try:
        cancel_pending(window)
    except Exception:  # noqa: BLE001
        pass
    try:
        window.destroy()
    except tk.TclError:
        pass


def unblock_text(name: str, had_my_block: bool, was_friend: bool, has_question: bool) -> str:
    """解除封禁的结果说明。三种情况的语义完全不同, 不能只说一句"已解禁"。"""
    if not had_my_block:
        return (t("""已清除本机『{0} 封禁了你』的标记。
再发一次加好友请求就能确认对方是否真的解禁了。""").format(name))
    if was_friend:
        return (t("""已解除对 {0} 的封禁。
你们仍然是好友 (封禁只是暂时拒收消息), 正在重新连接;
对方也会收到『已解禁』的通知。""").format(name))
    return (t("""已解除对 {0} 的封禁, 对方会收到通知。
他之后可以重新发送加好友请求""").format(name)
            + (t("(你设的验证问题仍然要答对)") if has_question else "") + "。")


# ===========================================================================
# 添加联系人: 搜索到的局域网用户 (+可选验证问题)
# ===========================================================================
class ContactsDialog:
    """列出局域网中搜索到的人, 可以一键发送加好友请求或封禁。"""

    def __init__(self, app: "ChatApp") -> None:
        self.app = app
        self.svc = app.service
        self.win = tk.Toplevel(app)
        self.win.title(t("添加联系人"))
        self.win.configure(bg=C["panel"])
        self.win.transient(app)

        # 布局要点: 固定高度的区块先 pack(side="bottom"), 让"会伸缩"的人员列表最后占用
        # 剩余空间。否则窗口一矮, 排在最后的按钮会被挤出可视区域 (踩过一次:
        # 底部按钮只露出一半, 用户根本点不到)。
        header = tk.Frame(self.win, bg=C["panel"], padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=t("局域网中的人"), font=FONT_BOLD, bg=C["panel"],
                 fg=C["text"]).pack(side="left")
        self.count_label = tk.Label(header, text="", font=FONT_SMALL, bg=C["panel"], fg=C["muted"])
        self.count_label.pack(side="left", padx=8)
        _btn(header, t("重新搜索"), self.refresh, "ghost").pack(side="right")

        search_bar = tk.Frame(self.win, bg=C["panel"], padx=14)
        search_bar.pack(fill="x")
        tk.Label(search_bar, text="🔍", bg=C["panel"], fg=C["muted"]).pack(side="left")
        self.filter_var = tk.StringVar(master=self.win)
        tk.Entry(search_bar, textvariable=self.filter_var, font=FONT, relief="flat",
                 bg=C["bg"]).pack(fill="x", side="left", ipady=4, padx=6)
        self.filter_var.trace_add("write", lambda *_: self.refresh())

        footer = tk.Frame(self.win, bg=C["panel"], padx=14, pady=12)
        footer.pack(side="bottom", fill="x")
        self.footer = footer          # 测试用: 检查按钮有没有被挤出窗口
        _btn(footer, t("发送加好友请求"), self.send_request, "primary", width=14).pack(side="right")
        _btn(footer, t("封禁此人"), self.block_selected, "danger", width=10).pack(side="right", padx=8)
        _btn(footer, t("解除封禁"), self.unblock_selected, "ghost", width=10).pack(side="right")
        _btn(footer, t("给选中的人出题"), self.set_question_for_selected, "ghost",
             width=14).pack(side="right", padx=8)
        _btn(footer, t("取消出题"), self.clear_question_for_selected, "ghost",
             width=10).pack(side="right")
        _btn(footer, t("关闭"), lambda: _close_toplevel(self.win), "ghost",
             width=6).pack(side="left")

        self.hint = tk.Label(self.win, text="", font=FONT_SMALL, bg=C["panel"], fg=C["muted"],
                             justify="left", padx=14, anchor="w")
        self.hint.pack(side="bottom", fill="x")

        # 出题区: 我给对方出题 (可选)
        qa = tk.LabelFrame(self.win, text=t(" 我给选中的人出题 (可选: 以后他要加我, 必须先答对) "),
                           bg=C["panel"], fg=C["text"], font=FONT_SMALL, padx=12, pady=8)
        qa.pack(side="bottom", fill="x", padx=14, pady=(4, 6))
        row1 = tk.Frame(qa, bg=C["panel"])
        row1.pack(fill="x")
        tk.Label(row1, text=t("问题:"), font=FONT_SMALL, bg=C["panel"], fg=C["muted"]).pack(side="left")
        self.question_var = tk.StringVar(master=self.win)
        tk.Entry(row1, textvariable=self.question_var, font=FONT_SMALL, relief="flat",
                 bg=C["bg"]).pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        row2 = tk.Frame(qa, bg=C["panel"])
        row2.pack(fill="x", pady=(6, 0))
        tk.Label(row2, text=t("答案:"), font=FONT_SMALL, bg=C["panel"], fg=C["muted"]).pack(side="left")
        self.answer_var = tk.StringVar(master=self.win)
        tk.Entry(row2, textvariable=self.answer_var, font=FONT_SMALL, relief="flat",
                 bg=C["bg"], show="●").pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        tk.Label(row2, text=t("答错上限:"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")
        self.attempts_var = tk.StringVar(master=self.win,
                                         value=str(DEFAULT_MAX_ANSWER_ATTEMPTS))
        tk.Spinbox(row2, from_=1, to=10, width=4, textvariable=self.attempts_var,
                   font=FONT_SMALL).pack(side="left", padx=(4, 0))
        self.question_state = tk.Label(qa, text="", font=FONT_SMALL, bg=C["panel"],
                                       fg=C["muted"], justify="left", anchor="w", wraplength=620)
        self.question_state.pack(anchor="w", pady=(6, 0))
        tk.Label(qa, text=(t("答案只在本机参与哈希计算, 不会发送出去; 对方答错到上限会被你自动封禁。\n"
                           "出题: 填好问题/答案 → 『给选中的人出题』; 反悔了就点『取消出题』。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left").pack(anchor="w")

        # 答题区: 对方给我出的题 (不需要在列表里选中谁, 直接点按钮就能答)
        pend = tk.LabelFrame(self.win, text=t(" 对方给我出的验证问题 (答对后对方才会收到你的请求) "),
                             bg=C["panel"], fg=C["text"], font=FONT_SMALL, padx=12, pady=8)
        pend.pack(side="bottom", fill="x", padx=14, pady=(4, 4))
        self.pending_label = tk.Label(pend, text="", font=FONT_SMALL, bg=C["panel"],
                                      fg=C["muted"], justify="left", anchor="w", wraplength=500)
        self.pending_label.pack(side="left", fill="x", expand=True)
        self.answer_btn = _btn(pend, t("✋ 回答问题"), self.answer_pending, "primary", width=11)
        self.answer_btn.pack(side="right")

        # 手动添加: 广播不通的网络 (Tailscale / WireGuard / 跨网段) 只能手填地址
        manual = tk.LabelFrame(
            self.win, text=t(" 手动添加 (对方不在同一网段 / 广播不通时用: Tailscale、WireGuard、跨网段) "),
            bg=C["panel"], fg=C["text"], font=FONT_SMALL, padx=12, pady=8)
        manual.pack(side="bottom", fill="x", padx=14, pady=(4, 4))
        mrow = tk.Frame(manual, bg=C["panel"])
        mrow.pack(fill="x")
        tk.Label(mrow, text=t("对方地址:"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")
        self.manual_var = tk.StringVar(master=self.win)
        self.manual_entry = tk.Entry(mrow, textvariable=self.manual_var, font=FONT_SMALL,
                                     relief="flat", bg=C["bg"])
        self.manual_entry.pack(side="left", fill="x", expand=True, padx=6, ipady=3)
        self.manual_entry.bind("<Return>", lambda _e: self.add_manual())
        # 提示就写在输入框**旁边**: 只填 IP 和 填 IP:端口 是两条不同的路
        tk.Label(mrow, text=t("只填 IP = UDP 单播探测 (自动问出端口)"), font=FONT_SMALL,
                 bg=C["panel"], fg=C["muted"]).pack(side="left")
        tk.Label(mrow, text=" | ", font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")
        tk.Label(mrow, text=t("IP:端口 = 直连 (不探测)"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")
        _btn(mrow, t("添加并发起加好友请求"), self.add_manual, "primary", width=18).pack(side="left",
                                                                                    padx=6)
        tk.Label(manual,
                 text=(t("例: `100.64.0.7` → 单播问一句『你在吗』, 自动拿到对方端口; "
                       "`100.64.0.7:50606` → 直接 TCP 连过去 (对方开了『隐身』或改了发现端口时用)。\n"
                       "打错的标点不用管: 中文冒号「：」、全角数字、多打的空格、"
                       "连 ` 一起粘进来, 都会自动纠正。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 anchor="w", wraplength=640).pack(fill="x", pady=(4, 0))

        # 人员列表: 唯一"会伸缩"的区块, 放最后, 窗口变小只会压缩它
        body = tk.Frame(self.win, bg=C["panel"], padx=14, pady=8)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("name", "addr", "state"), show="headings",
                                 selectmode="browse", height=6)
        self.tree.heading("name", text=t("昵称"))
        self.tree.heading("addr", text=t("地址"))
        self.tree.heading("state", text=t("状态"))
        self.tree.column("name", width=150, anchor="w")
        self.tree.column("addr", width=180, anchor="w")
        self.tree.column("state", width=170, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        # 按内容算出合适的高度, 再用屏幕高度兜底 —— 默认就完整显示所有按钮
        self.win.update_idletasks()
        width = max(700, self.win.winfo_reqwidth())
        height = self.win.winfo_reqheight()
        try:
            screen_h = self.win.winfo_screenheight()
            height = min(height, max(420, screen_h - 140))
        except tk.TclError:
            pass
        self.win.geometry(f"{width}x{height}")
        self.win.minsize(640, 420)

        self.peers: List[Dict[str, Any]] = []
        app.register_hook("lan-peers", self.refresh)
        app.register_hook("contacts", self.refresh)
        app.register_hook("questions", self.refresh_pending)
        self.tree.bind("<Double-1>", lambda _e: self.send_request())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.refresh_question_state())
        self.refresh()

    def refresh(self) -> None:
        if not self.win.winfo_exists():
            self.app.unregister_hook("lan-peers", self.refresh)
            self.app.unregister_hook("contacts", self.refresh)
            self.app.unregister_hook("questions", self.refresh_pending)
            return
        keyword = self.filter_var.get().strip().lower()
        # 列表每 3 秒会被"发现服务"的广播刷一次, 必须把选中的人记住并还原,
        # 否则用户刚选好人、还没来得及点按钮, 选中就被刷掉了 (会看到"请先选择一个人")。
        previous = self.tree.selection()
        keep = previous[0] if previous else ""
        self.peers = self.svc.lan_peers()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for peer in self.peers:
            if keyword and keyword not in peer["name"].lower() \
                    and keyword not in peer["address"].lower():
                continue
            state_label = {
                ContactState.FRIEND.value: t("已是好友"),
                ContactState.REQUEST_IN.value: t("对方请求加你 (待处理)"),
                ContactState.REQUEST_OUT.value: t("已发请求, 等待确认"),
                ContactState.BLOCKED.value: t("已封禁"),
                ContactState.STRANGER.value: t("可以添加"),
            }.get(peer["state"], t("可以添加"))
            if peer.get("manual"):
                # 本地登记的地址 ≠ 搜到的人。标出来, 不然会以为"隐身没用/广播正常"
                state_label = t("手动登记 · ") + state_label
            # iid 用 peer_id: 刷新后能精确还原选中项, 也不会被重名的人弄混
            self.tree.insert("", "end", iid=peer["peer_id"],
                             values=(peer["name"], peer["address"], state_label))
            shown += 1
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
        elif shown == 1:
            self.tree.selection_set(self.tree.get_children()[0])
        self.count_label.configure(text=t("共 {0} 人").format(shown))
        if not self.peers:
            self.hint.configure(
                text=(t("还没有搜索到任何人。请确认:\n"
                      "  • 对方也打开了本程序\n"
                      "  • 双方在同一个局域网 / 同一个 Wi-Fi\n"
                      "  • Windows 防火墙允许本程序 (第一次运行要勾选『专用网络』)\n"
                      "  • 用了 Tailscale / WireGuard 这类组网? 它们不跑广播, "
                      "请在下面『手动添加』里填对方的 IP:端口"))
            )
        else:
            self.hint.configure(
                text=(t("选中某人后点『发送加好友请求』; 对方同意后即可加密聊天。\n"
                      "想确认没有中间人: 和对方核对『设置』里显示的身份指纹。"))
            )
        self.refresh_pending()
        self.refresh_question_state()

    def refresh_question_state(self) -> None:
        """显示"选中的人当前有没有被我出题" —— 出题人也需要能看见并取消。"""
        if not self.win.winfo_exists():
            return
        peer = self._selected_peer()
        if peer is None:
            self.question_state.configure(text=t("选中一个人后, 这里会显示他现在的验证问题。"),
                                          fg=C["muted"])
            return
        contact = self.svc.get_contact(peer["peer_id"])
        if self.svc.has_question_for(peer["peer_id"]):
            name = contact.name if contact is not None else peer["name"]
            self.question_state.configure(
                text=t("已给 {0} 出题: {1} (答错 {2} 次自动封禁) —— 想撤销就点『取消出题』").format(name, self.svc.question_of(peer['peer_id']), self.svc.max_attempts_for(peer['peer_id'])),
                fg=C["text"])
        else:
            self.question_state.configure(text=t("{0} 目前没有验证问题。").format(peer['name']), fg=C["muted"])

    def refresh_pending(self) -> None:
        """刷新"对方给我出的题"这一栏 (与列表选中无关)。"""
        if not self.win.winfo_exists():
            return
        items = self.svc.pending_challenges()
        if not items:
            self.pending_label.configure(text=t("暂时没有。对方给你出了题, 这里会显示题目。"),
                                         fg=C["muted"])
            self.answer_btn.configure(text=t("✋ 回答问题"))
            return
        first = items[0]
        more = t(" (还有 {0} 个人的题, 答完这个再来)").format(len(items) - 1) if len(items) > 1 else ""
        self.pending_label.configure(
            text=t("""共 {0} 条待回答{1}
{2}: {3}""").format(len(items), more, first['name'], first['question']),
            fg=C["text"])
        self.answer_btn.configure(text=t("✋ 回答问题 ({0})").format(len(items)))

    def answer_pending(self) -> None:
        """回答对方的验证问题 (不需要在列表里选人)。"""
        if not self.svc.pending_challenges():
            messagebox.showinfo(
                t("验证问题"),
                t("现在没有需要回答的验证问题。\n\n"
                "如果对方设了题, 你发完加好友请求后会在这里看到题目。"),
                parent=self.win)
            return
        self.app.open_pending_answers()

    def _selected_peer(self) -> Optional[Dict[str, Any]]:
        sel = self.tree.selection()
        if not sel:
            return None
        peer_id = sel[0]
        for peer in self.peers:
            if peer["peer_id"] == peer_id:
                return peer
        return None

    def _attempts(self) -> int:
        try:
            return max(1, int(self.attempts_var.get()))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ANSWER_ATTEMPTS

    def add_manual(self) -> None:
        """手动添加: 填 `IP` (自动探测端口) 或 `IP:TCP端口` (直接连)。

        适用: Tailscale / WireGuard / OpenVPN(tun) 这类三层组网, 以及跨网段 —— 那些
        网络里根本没有广播, 自动发现永远看不到对方。
        """
        raw = self.manual_var.get().strip()
        if not raw:
            messagebox.showinfo(t("手动添加"),
                                t("请填对方地址:\n"
                                "  • 只填 IP (例如 100.64.0.7) —— 程序会自动探测对方端口\n"
                                "  • 或 IP:TCP端口 (例如 192.168.1.5:50606) —— 直接连, "
                                "对方开了『隐身』时用这个"),
                                parent=self.win)
            return
        # 中文输入法下 "：" 和 ":" 看着一样: 这里自动换成英文冒号/去掉全角数字等。
        # (报过: 打成了中文冒号, 结果提示"端口不是数字", 得让人去猜哪里错了)
        host, port, error = split_manual_address(raw)
        if error:
            messagebox.showwarning(t("手动添加"), t("""{0}

你填的是: {1}""").format(error, raw), parent=self.win)
            return
        fixed = f"{host}:{port}" if port else host
        if fixed != raw:
            self.app._append("sys", t("已自动整理地址:「{0}」→「{1}」(中文冒号/全角字符/空格都替你换好了)").format(raw, fixed))
        contact = self.svc.add_manual_peer(host, port)
        if contact is None:
            messagebox.showwarning(
                t("手动添加"),
                t("""没能添加上 {0}。

只填 IP 时要求对方**允许被自动搜索**(设置里的开关开着)、没改发现端口;
对方开了『隐身』的话, 请改成填 `对方IP:对方的TCP端口`(对方在设置里能看到自己的当前端口)。""").format(raw),
                parent=self.win)
            return
        self.manual_var.set("")
        self.refresh()
        self.tree.selection_set(contact.peer_id)
        self.win.after(2500, lambda: self._check_request_sent(contact.peer_id, contact.name))

    def send_request(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            if not self.tree.get_children():
                messagebox.showinfo(t("添加联系人"), t("现在还没搜索到任何人。\n"
                                                  "等对方打开程序后点『重新搜索』。"), parent=self.win)
            else:
                messagebox.showinfo(t("添加联系人"), t("请先在上面的列表里点一下要加的人 (选中后会高亮),\n"
                                                  "再点『发送加好友请求』。"), parent=self.win)
            return
        self.svc.send_friend_request(peer["peer_id"])
        self.refresh()
        # 请求是异步发出去的: 过一会儿看一下有没有发成功, 失败就直接告诉用户,
        # 免得界面上只有"已发请求, 等待确认"却永远等不到对方同意。
        self.win.after(2500, lambda: self._check_request_sent(peer["peer_id"], peer["name"]))

    def _check_request_sent(self, peer_id: str, name: str) -> None:
        if not self.win.winfo_exists():
            return
        contact = self.svc.get_contact(peer_id)
        if contact is None or contact.state != ContactState.REQUEST_OUT.value:
            return
        if contact.dial_state == "failed" and contact.last_error:
            messagebox.showwarning(
                t("加好友请求没发出去"),
                t("""没能联系上 {0}: {1}

请确认: 对方的程序还开着、双方在同一个局域网 / 同一个 Wi-Fi、防火墙允许本程序。然后再点一次『发送加好友请求』。""").format(name, contact.last_error),
                parent=self.win)

    def set_question_for_selected(self) -> None:
        """给选中的人设题: 他以后加我时必须答对。"""
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo(t("设题"), t("请先在上面的列表里点一下要给谁出题 (选中后会高亮)。"),
                                parent=self.win)
            return
        question = self.question_var.get().strip()
        answer = self.answer_var.get().strip()
        if not question or not answer:
            missing = "、".join(part for part, value in
                                ((t("『问题』"), question), (t("『答案』", answer))) if not value)
            messagebox.showwarning(
                t("设题"),
                t("""{0} 还没有填。

请在上面那一栏把问题和答案都写上, 再点『给选中的人设题』。""").format(missing),
                parent=self.win)
            return
        self.svc.set_question(peer["peer_id"], question, answer, self._attempts())
        self.refresh()

    def clear_question_for_selected(self) -> None:
        """取消给选中的人出的题 (对方以后加我就不用答题了)。"""
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo(t("取消出题"), t("请先在上面的列表里点一下要给谁取消出题。"),
                                parent=self.win)
            return
        if not self.svc.has_question_for(peer["peer_id"]):
            messagebox.showinfo(t("取消出题"), t("{0} 现在没有验证问题, 不用取消。").format(peer['name']),
                                parent=self.win)
            return
        if not messagebox.askyesno(
                t("取消出题"),
                t("""取消给 {0} 出的题?

当前题目: {1}
取消后他再申请加你就不需要答题了。""").format(peer['name'], self.svc.question_of(peer['peer_id'])),
                parent=self.win):
            return
        self.svc.clear_question(peer["peer_id"])
        self.refresh()

    def block_selected(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo(t("封禁"), t("请先在上面的列表里点一下要封禁的人。"), parent=self.win)
            return
        contact = self.svc.get_contact(peer["peer_id"])
        keep = bool(contact and contact.is_friend)
        if not messagebox.askyesno(
                t("封禁"),
                t("""确定封禁 {0} 吗?
对方之后发来的加好友请求和消息都会被拒收。""").format(peer['name'])
                + (t("\n\n你们还是好友: 解禁之后好友关系照旧, 消息也能继续发。")
                   if keep else ""),
                parent=self.win):
            return
        self.svc.block(peer["peer_id"])
        self.refresh()

    def unblock_selected(self) -> None:
        """解除封禁 (恢复原来的关系, 并通知对方)。"""
        peer = self._selected_peer()
        if peer is None:
            messagebox.showinfo(t("解除封禁"), t("请先在上面的列表里点一下要解禁的人。"),
                                parent=self.win)
            return
        contact = self.svc.get_contact(peer["peer_id"])
        blocked = bool(contact and (contact.blocked or contact.blocked_by_peer))
        if not blocked and not peer.get("blocked"):
            messagebox.showinfo(t("解除封禁"), t("{0} 没有被封禁。").format(peer['name']), parent=self.win)
            return
        was_friend = bool(contact and (contact.friend_before_block or contact.is_friend))
        had_my_block = bool(contact and contact.blocked)
        if not messagebox.askyesno(
                t("解除封禁"),
                t("""解除对 {0} 的封禁?
""").format(peer['name'])
                + (t("你们是好友, 解禁后关系保留, 对方会收到『已解禁』通知。")
                   if had_my_block and was_friend else
                   t("解禁后对方会收到通知, 可以重新发送加好友请求。")
                   if had_my_block else
                   t("这只是清掉本机『对方封了你』的标记。")),
                parent=self.win):
            return
        if self.svc.unblock(peer["peer_id"]):
            messagebox.showinfo(t("解除封禁"),
                                unblock_text(peer["name"], had_my_block, was_friend,
                                             self.svc.has_question_for(peer["peer_id"])),
                                parent=self.win)
        self.refresh()


# ===========================================================================
# 通知中心: 对面删了你 / 下线了 / 取消请求 这类事都记在这里
# ===========================================================================
class NotificationsDialog:
    """一张"最近发生了什么"的清单。"""

    def __init__(self, app: "ChatApp") -> None:
        self.app = app
        self.win = tk.Toplevel(app)
        self.win.title(t("通知"))
        self.win.configure(bg=C["panel"])
        self.win.transient(app)

        header = tk.Frame(self.win, bg=C["panel"], padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=t("最近发生了什么"), font=FONT_BOLD, bg=C["panel"],
                 fg=C["text"]).pack(side="left")
        self.count_label = tk.Label(header, text="", font=FONT_SMALL, bg=C["panel"], fg=C["muted"])
        self.count_label.pack(side="left", padx=8)

        footer = tk.Frame(self.win, bg=C["panel"], padx=14, pady=12)
        footer.pack(side="bottom", fill="x")
        _btn(footer, t("关闭"), lambda: _close_toplevel(self.win), "ghost",
             width=6).pack(side="right")
        _btn(footer, t("清空"), self.clear, "ghost", width=6).pack(side="right", padx=8)

        tk.Label(self.win, font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 padx=14, anchor="w",
                 text=t("双击一条通知可以跳到对应的人（如果还在联系人列表里）。")).pack(
            side="bottom", fill="x")

        body = tk.Frame(self.win, bg=C["panel"], padx=14, pady=8)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("when", "text"), show="headings",
                                 selectmode="browse", height=12)
        self.tree.heading("when", text=t("时间"))
        self.tree.heading("text", text=t("内容"))
        self.tree.column("when", width=90, anchor="w")
        self.tree.column("text", width=430, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure("warn", foreground=C["warn"])
        self.tree.tag_configure("err", foreground=C["err"])

        self.rows: List[Dict[str, Any]] = []
        self.refresh()
        # 打开就算读过了
        for item in self.app.notices:
            item["read"] = True
        self.app._update_answer_button()
        self.win.update_idletasks()
        width = max(620, self.win.winfo_reqwidth())
        height = min(self.win.winfo_reqheight(), 640)
        self.win.geometry(f"{width}x{height}")
        self.tree.bind("<Double-1>", lambda _e: self.jump())

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows = list(reversed(self.app.notices))     # 最新的在最上面
        for item in self.rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(item["ts"]))
            level = str(item.get("level", "info"))
            tag = level if level in ("warn", "err") else ""
            self.tree.insert("", "end", values=(when, item["text"]), tags=(tag,))
        self.count_label.configure(text=t("共 {0} 条").format(len(self.rows)) if self.rows else t("暂时没有"))

    def jump(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        index = self.tree.index(sel[0])
        if 0 <= index < len(self.rows):
            peer_id = str(self.rows[index].get("peer_id", ""))
            if peer_id and peer_id in self.app._row_ids:
                self.app.select_contact(peer_id)

    def clear(self) -> None:
        self.app.notices.clear()
        self.refresh()
        self.app._update_answer_button()


# ===========================================================================
# 新朋友: 加好友请求
# ===========================================================================
class FriendRequestsDialog:
    """别人发来的加好友请求: 同意 / 拒绝 / 封禁 (通过验证问题的会标注)。"""

    def __init__(self, app: "ChatApp") -> None:
        self.app = app
        self.svc = app.service
        self.win = tk.Toplevel(app)
        self.win.title(t("新朋友"))
        self.win.configure(bg=C["panel"])
        self.win.transient(app)

        header = tk.Frame(self.win, bg=C["panel"], padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=t("加好友请求"), font=FONT_BOLD, bg=C["panel"],
                 fg=C["text"]).pack(side="left")
        self.hint = tk.Label(header, text="", font=FONT_SMALL, bg=C["panel"], fg=C["muted"])
        self.hint.pack(side="left", padx=8)

        # 固定高度的区块先 pack(side="bottom"), 列表最后 → 窗口变矮也不会把按钮挤没
        footer = tk.Frame(self.win, bg=C["panel"], padx=14, pady=12)
        footer.pack(side="bottom", fill="x")
        self.footer = footer          # 测试用: 检查按钮有没有被挤出窗口
        _btn(footer, t("同意"), self.accept, "primary", width=8).pack(side="right")
        _btn(footer, t("拒绝"), self.reject, "ghost", width=8).pack(side="right", padx=8)
        _btn(footer, t("封禁此人"), self.block, "danger", width=10).pack(side="left")
        _btn(footer, t("关闭"), lambda: _close_toplevel(self.win), "ghost",
             width=6).pack(side="left", padx=8)

        tk.Label(self.win, font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 padx=14, anchor="w",
                 text=(t("同意之前建议和对方核对『身份指纹』(对方在『设置』里能看到), "
                       "指纹一致说明中间没有人冒充。"))).pack(side="bottom", fill="x")

        body = tk.Frame(self.win, bg=C["panel"], padx=14, pady=8)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("name", "fp", "note", "when"), show="headings",
                                 selectmode="browse", height=8)
        for col, title, width in (("name", t("昵称"), 120), ("fp", t("身份指纹"), 160),
                                  ("note", t("附言"), 190), ("when", t("时间"), 90)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        self.win.update_idletasks()
        width = max(700, self.win.winfo_reqwidth())
        height = self.win.winfo_reqheight()
        try:
            screen_h = self.win.winfo_screenheight()
            height = min(height, max(360, screen_h - 140))
        except tk.TclError:
            pass
        self.win.geometry(f"{width}x{height}")
        self.win.minsize(600, 340)

        app.register_hook("contacts", self.refresh)
        self.requests: List[Contact] = []
        self.refresh()

    def refresh(self) -> None:
        if not self.win.winfo_exists():
            self.app.unregister_hook("contacts", self.refresh)
            return
        # 同样要保住选中项: 每 3 秒一次的自动刷新不能让用户刚选好的请求"掉选择"
        previous = self.tree.selection()
        keep = previous[0] if previous else ""
        self.requests = self.svc.pending_requests()
        self.tree.delete(*self.tree.get_children())
        for contact in self.requests:
            when = time.strftime("%m-%d %H:%M", time.localtime(contact.updated_at))
            note = contact.message or "-"
            self.tree.insert("", "end", iid=contact.peer_id,
                             values=(contact.name, contact.fingerprint, note, when))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
        elif len(self.requests) == 1:
            self.tree.selection_set(self.tree.get_children()[0])
        self.hint.configure(text=t("{0} 条待处理").format(len(self.requests)) if self.requests
                            else t("暂时没有新的请求"))

    def _selected(self) -> Optional[Contact]:
        sel = self.tree.selection()
        if not sel:
            return None
        peer_id = sel[0]
        for contact in self.requests:
            if contact.peer_id == peer_id:
                return contact
        return None

    def accept(self) -> None:
        contact = self._selected()
        if contact is None:
            messagebox.showinfo(t("新朋友"),
                                t("请先在列表里点一下要同意的那条请求 (选中后会高亮), 再点『同意』。"),
                                parent=self.win)
            return
        self.svc.accept_request(contact.peer_id)
        self.refresh()
        self.app.select_contact(contact.peer_id)

    def reject(self) -> None:
        contact = self._selected()
        if contact is None:
            messagebox.showinfo(t("新朋友"), t("请先在列表里点一下要拒绝的那条请求。"), parent=self.win)
            return
        self.svc.reject_request(contact.peer_id)
        self.refresh()

    def block(self) -> None:
        contact = self._selected()
        if contact is None:
            messagebox.showinfo(t("封禁"), t("请先在列表里点一下要封禁的那条请求。"), parent=self.win)
            return
        if messagebox.askyesno(t("封禁"), t("""封禁 {0}?
对方之后无法再发请求或消息。""").format(contact.name),
                               parent=self.win):
            self.svc.block(contact.peer_id)
            self.refresh()


# ===========================================================================
# 收到文件: 同意 / 另存为 / 拒绝
# ===========================================================================
class IncomingFileDialog:
    """对方发来文件时弹出: 存到"我的接收目录" / 另存到别处 / 拒绝。

    不再有"默认目录"这种说法: 接收目录是用户在『⚙ 设置』里自己定好的那一个
    (统一所有文件的落点), 想临时改就点『另存为…』。
    """

    def __init__(self, app: "ChatApp", peer_id: str, peer_name: str, name: str,
                 size: Any, transfer_id: str) -> None:
        self.app = app
        self.svc = app.service
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.name = name
        self.transfer_id = transfer_id
        self.done = False
        self.default_dir = os.path.abspath(self.svc.download_dir)   # = 我的接收目录
        self.chosen_path = ""            # 用户自己挑的完整路径 (空 = 用接收目录)

        self.win = tk.Toplevel(app)
        self.win.title(t("收到文件"))
        self.win.configure(bg=C["panel"])
        self.win.geometry("640x390")
        self.win.transient(app)

        frame = tk.Frame(self.win, bg=C["panel"], padx=20, pady=16)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=t("{0} 想给你发送文件").format(peer_name), font=FONT_BOLD, bg=C["panel"],
                 fg=C["text"]).pack(anchor="w")
        tk.Label(frame, text=f"📎 {name}   ({human_size(size)})", font=(FONT[0], 13),
                 bg=C["panel"], fg=C["green_dark"], wraplength=580,
                 justify="left").pack(anchor="w", pady=(8, 12))

        tk.Label(frame, text=t("存到哪里?"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(anchor="w")
        buttons = tk.Frame(frame, bg=C["panel"])
        buttons.pack(fill="x", pady=(6, 10))
        _btn(buttons, t("📥 接收 (存到我的接收目录)"), self.accept_default, "primary",
             width=24).pack(side="left")
        _btn(buttons, t("📂 另存到别处…"), self.accept_as, "ghost",
             width=14).pack(side="left", padx=10)
        _btn(buttons, t("拒绝"), self.reject, "danger", width=8).pack(side="left")

        tk.Label(frame, text=t("我的接收目录 (可在『⚙ 设置』里改):"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(anchor="w")
        self.target_label = tk.Label(frame, text="", font=FONT_SMALL, bg=C["panel"],
                                     fg=C["text"], justify="left", anchor="w", wraplength=560)
        self.target_label.pack(anchor="w")
        tk.Label(frame, text=(t("点『另存到别处…』会打开系统的保存对话框, 目录和文件名都能自己改。\n"
                              "文件收完会校验 SHA256; 目标已存在时自动改名, 不会覆盖。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 wraplength=580).pack(anchor="w", pady=(8, 0))
        self._refresh_target()

        self.win.protocol("WM_DELETE_WINDOW", self.reject)
        self._focus_after = self.win.after(60, self._focus)

    def _focus(self) -> None:
        try:
            _bring_to_front(self.win)
            self.win.focus_force()
        except tk.TclError:
            pass

    def _refresh_target(self) -> None:
        if self.chosen_path:
            self.target_label.configure(text=t("""将保存到你自己选的位置:
{0}""").format(self.chosen_path),
                                        fg=C["green_dark"])
        else:
            self.target_label.configure(text=t("""{0}
(文件: {1})""").format(self.default_dir, self.name), fg=C["text"])

    def choose_path(self) -> str:
        """让用户自己挑一个保存位置 (文件名默认用对方发来的名字)。"""
        start_dir = (os.path.dirname(self.chosen_path) if self.chosen_path
                     else (self.app.last_save_dir or self.default_dir))
        path = filedialog.asksaveasfilename(
            parent=self.win, title=t("选择保存位置"), initialfile=self.name,
            initialdir=start_dir or self.default_dir,
            defaultextension=os.path.splitext(self.name)[1])
        if path:
            self.chosen_path = os.path.abspath(path)
            self.app.last_save_dir = os.path.dirname(self.chosen_path)   # 下次默认来这里
            self._refresh_target()
        return self.chosen_path

    def accept_default(self) -> None:
        """存到我设好的接收目录 (设置里可以改)。"""
        self.chosen_path = ""
        self._accept_with(None)

    def accept_as(self) -> None:
        """自己选一个位置; 选好就直接开始接收 (不用再点一次)。"""
        if not self.choose_path():
            return                            # 用户取消了选择, 停在弹窗里
        self._accept_with(self.chosen_path)

    def accept(self) -> None:                 # 兼容旧调用 (测试里用过)
        self._accept_with(self.chosen_path or None)

    def _accept_with(self, save_path: Optional[str]) -> None:
        if self.done:
            return
        self.done = True
        self.svc.accept_file(self.transfer_id, save_path=save_path)
        target = save_path or os.path.join(self.default_dir, self.name)
        if self.app._current == self.peer_id:
            self.app._append("sys", t("已同意接收 {0} -> {1}").format(self.name, target))
        # 记一条通知: 用户随时能在『📢 通知』里查到"这个文件到底存哪了"
        self.app.record_notice(t("接收 {0} -> {1}").format(self.name, target), self.peer_id, "info")
        self.close()

    def reject(self) -> None:
        if not self.done:
            self.done = True
            self.svc.reject_file(self.transfer_id)
            if self.app._current == self.peer_id:
                self.app._append("sys", t("已拒绝接收 {0}").format(self.name))
        self.close()

    def close(self) -> None:
        job = getattr(self, "_focus_after", None)
        if job:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
            self._focus_after = None
        self.app._incoming_file_dialogs.pop(self.transfer_id, None)
        _close_toplevel(self.win)


# ===========================================================================
# 回答对方的验证问题
# ===========================================================================
class AnswerQuestionDialog:
    """收到对方的加好友验证问题时弹出, 让用户回答。"""

    def __init__(self, app: "ChatApp", peer_id: str, question: str,
                 attempts: int = DEFAULT_MAX_ANSWER_ATTEMPTS, nonce: str = "") -> None:
        self.app = app
        self.svc = app.service
        self.peer_id = peer_id
        self.nonce = nonce
        self.win = tk.Toplevel(app)
        self.win.title(t("加好友验证问题"))
        self.win.configure(bg=C["panel"])
        self.win.geometry("500x280")
        self.win.transient(app)

        frame = tk.Frame(self.win, bg=C["panel"], padx=20, pady=16)
        frame.pack(fill="both", expand=True)
        contact = self.svc.get_contact(peer_id)
        name = contact.name if contact else peer_id
        tk.Label(frame, text=t("{0} 的验证问题").format(name), font=FONT_BOLD, bg=C["panel"],
                 fg=C["text"]).pack(anchor="w")
        self.question_label = tk.Label(frame, text=question, font=(FONT[0], 12), bg=C["panel"],
                                       fg=C["green_dark"], wraplength=440, justify="left")
        self.question_label.pack(anchor="w", pady=(8, 12))
        tk.Label(frame, text=(t("""答对之后 {0} 才会收到并处理你的加好友请求; 答错 {1} 次会被对方自动封禁。
答案只在本机计算, 不上网。""").format(name, attempts)),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 wraplength=440).pack(anchor="w")
        self.answer_var = tk.StringVar(master=self.win)
        self.entry = tk.Entry(frame, textvariable=self.answer_var, font=FONT, relief="flat",
                              bg=C["bg"])
        self.entry.pack(fill="x", ipady=6, pady=10)
        self.entry.bind("<Return>", lambda _e: self.submit())
        # 判定结果就地显示, 答错了不用关窗、直接改了再交一次
        self.result_label = tk.Label(frame, text="", font=FONT_SMALL, bg=C["panel"],
                                     fg=C["muted"], justify="left", anchor="w", wraplength=440)
        self.result_label.pack(anchor="w")

        row = tk.Frame(frame, bg=C["panel"])
        row.pack(fill="x", pady=(6, 0))
        self.submit_btn = _btn(row, t("提交答案"), self.submit, "primary", width=10)
        self.submit_btn.pack(side="right")
        _btn(row, t("稍后再答"), self.close, "ghost", width=8).pack(side="right", padx=8)
        _btn(row, t("取消加好友请求"), self.cancel_request, "danger",
             width=14).pack(side="left")
        tk.Label(frame, text=t("不想答了可以点『取消加好友请求』: 对方那边这道题会作废。"),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 wraplength=440).pack(anchor="w", pady=(8, 0))

        self.win.protocol("WM_DELETE_WINDOW", self.close)
        # 关键: 弹窗刚出现时 Windows 可能把键盘焦点留在主窗口上, 用户敲的答案就进了
        # 聊天输入框, 点"提交答案"时这里还是空的 —— 于是提示"请填写答案"。
        # 所以窗口显示后强制抢一次焦点, 并选中输入框内容, 直接打字就是答案。
        self._focus_after = self.win.after(60, self._focus_entry)
        self.waiting = False
        self._wait_job: Any = None

    def _focus_entry(self) -> None:
        try:
            if not self.win.winfo_exists():
                return
            _bring_to_front(self.win)
            self.entry.focus_force()
            self.entry.select_range(0, "end")
        except tk.TclError:
            pass

    def close(self) -> None:
        job = getattr(self, "_focus_after", None)
        if job:
            try:
                self.win.after_cancel(job)
            except tk.TclError:
                pass
            self._focus_after = None
        self.app._forget_answer_dialog(self.peer_id, self)
        _close_toplevel(self.win)

    def cancel_request(self) -> None:
        """不答了: 直接取消这条加好友请求 (对方那边这道题也作废)。"""
        contact = self.svc.get_contact(self.peer_id)
        name = contact.name if contact else self.peer_id
        if not messagebox.askyesno(
                t("取消加好友请求"),
                t("""不再加 {0}, 放弃回答这道题?

对方那边这道验证问题会作废; 以后想加需要重新发请求。""").format(name),
                parent=self.win):
            return
        self.svc.cancel_friend_request(self.peer_id)
        self.close()

    def submit(self) -> None:
        if self.waiting:
            return
        answer = self.answer_var.get().strip()
        if not answer:
            self._recover_from_wrong_box()
            return
        if not self.svc.answer_challenge(self.peer_id, answer):
            # 失败原因 (连接断了 / 提问已失效) 已经由服务通过状态栏和聊天窗口说明
            if messagebox.askyesno(
                    t("答案没发出去"),
                    t("这条提问已经失效, 或者和对方的连接断了 (对方可能已经下线)。\n\n"
                    "要取消这条加好友请求吗? 取消后对方那边这道题也作废。"),
                    parent=self.win):
                self.svc.cancel_friend_request(self.peer_id)
            self.close()
            return
        # 提交完**不关窗**: 等对方的判定结果, 答错了可以当场改了再交
        self.waiting = True
        self.submit_btn.configure(state="disabled", text=t("等待校验…"))
        self.result_label.configure(text=t("已提交, 等待对方校验…"), fg=C["muted"])
        self._wait_job = self.win.after(12000, self._wait_timeout)

    def _wait_timeout(self) -> None:
        """对方一直没回话 (可能掉线了): 解除等待, 让用户自己决定重试还是取消。"""
        self._wait_job = None
        if not self.waiting:
            return
        self.waiting = False
        try:
            self.submit_btn.configure(state="normal", text=t("提交答案"))
            self.result_label.configure(text=t("对方一直没有回应 (可能已经下线), 可以再试一次或取消请求。"),
                                        fg=C["warn"])
        except tk.TclError:
            pass

    def on_result(self, ok: bool, attempts_left: int) -> None:
        """对方对这次答案的判定 (由 ChatApp 转达)。"""
        if self._wait_job:
            try:
                self.win.after_cancel(self._wait_job)
            except tk.TclError:
                pass
            self._wait_job = None
        self.waiting = False
        try:
            if not self.win.winfo_exists():
                return
        except tk.TclError:
            return
        if ok:
            self.submit_btn.configure(state="disabled")
            self.entry.configure(state="disabled")
            self.result_label.configure(text=t("✅ 答案正确! 等对方在他的『🔔 新朋友』里同意即可。"),
                                        fg=C["green_dark"])
            self._close_job = self.win.after(1500, self.close)
            return
        self.submit_btn.configure(state="normal", text=t("再交一次"))
        self.answer_var.set("")
        if attempts_left > 0:
            self.result_label.configure(
                text=t("❌ 答案不对, 还剩 {0} 次机会 —— 改一下再点『再交一次』。").format(attempts_left),
                fg=C["err"])
        else:
            self.result_label.configure(
                text=t("❌ 机会用完了, 对方已经自动封禁你。\n可以点『取消加好友请求』, "
                     "等对方解禁后再重新申请。"),
                fg=C["err"])
            self.submit_btn.configure(state="disabled")
        self._focus_entry()

    def _recover_from_wrong_box(self) -> None:
        """答案框是空的: 多半是刚才的输入跑到主窗口的聊天输入框里去了。"""
        self._focus_entry()
        stray = ""
        try:
            stray = self.app.entry.get("1.0", "end").strip()
        except tk.TclError:
            stray = ""
        if stray and "\n" not in stray:
            if messagebox.askyesno(
                    t("答案框是空的"),
                    t("""这个弹窗里的答案框没有内容。

你刚输入的「{0}」在聊天输入框里, 把它当作答案提交吗?""").format(stray[:40]),
                    parent=self.win):
                self.answer_var.set(stray)
                self.submit()
            return
        messagebox.showwarning(
            t("还差一步: 填答案"),
            t("这个弹窗里的输入框还是空的, 请把答案填进去再点『提交答案』。\n"
            "(弹窗没抢到键盘焦点时, 打字会跑到主窗口的聊天输入框里)"),
            parent=self.win)


# ===========================================================================
# 主窗口
# ===========================================================================
class ChatApp(tk.Frame):
    """微信式主界面。"""

    def __init__(self, master: tk.Tk, service: ChatService, args: argparse.Namespace,
                 own_service: bool = True, on_close_hook: Any = None) -> None:
        super().__init__(master, bg=C["panel"])
        self.master_window = master
        self.service = service
        self.args = args
        self.own_service = own_service          # True = 服务由本窗口创建, 关闭时一起停掉
        self.on_close_hook = on_close_hook      # 自测模式: 关一个窗口就一起收摊
        self.events: "queue.Queue[ServiceEvent]" = queue.Queue()
        self._hooks: Dict[str, List[Any]] = {}
        self._row_ids: List[str] = []
        self._closing = False
        self._current: Optional[str] = None
        self._last_progress_at: Dict[str, float] = {}
        self._after_jobs: List[str] = []         # 自己排的定时任务, 关窗口时一起取消
        self._answer_dialogs: Dict[str, "AnswerQuestionDialog"] = {}   # 正在答题的人 -> 弹窗
        self._incoming_file_dialogs: Dict[str, "IncomingFileDialog"] = {}  # 待确认的收文件弹窗
        self.notices: List[Dict[str, Any]] = []   # 通知中心 (对面删了你/下线了/取消请求…)
        self.last_save_dir = ""                   # 上次自己选的保存目录 (下次默认来这里)
        # 每个联系人一份聊天区内容 (内存里): 以前切换联系人就把聊天区清空了, 关掉程序更是
        # 一点不留。现在至少在本程序运行期间, 切来切去还能看到刚才聊了什么; 对方重启后
        # 由服务端补发历史 (见 service 的 history-*), 那些也会追加到这里。
        self._chat_log: Dict[str, List[tuple]] = {}
        self._history_note: Dict[str, str] = {}   # 每个人最后一条"系统提示", 重绘时放最前面

        self.service.on_event = self.events.put   # 后台线程 -> 队列 -> 界面线程

        self._build()
        from . import startup_log

        startup_log.step(t("主界面: 控件搭建完成"))
        master.configure(bg=C["panel"])
        master.protocol("WM_DELETE_WINDOW", self.on_close)

        if self.own_service:
            self.service.start()
            startup_log.step(t("主界面: 服务已启动"))
        self._drain_job = self.after(80, self._drain)
        self._tick_job = self.after(1000, self._tick)
        self.refresh_contacts()
        self._show_welcome()
        self._update_answer_button()
        startup_log.step(t("主界面: 首次渲染完成"))

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.master_window.rowconfigure(0, weight=1)
        self.master_window.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        top = tk.Frame(self, bg=C["sidebar"], height=52)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)
        me = tk.Frame(top, bg=C["sidebar"])
        me.pack(side="left", padx=12)
        self.me_label = tk.Label(me, text="", font=FONT_BOLD, bg=C["sidebar"], fg=C["text"])
        self.me_label.pack(anchor="w")
        self.fp_label = tk.Label(me, text="", font=("Consolas", 8), bg=C["sidebar"], fg=C["muted"])
        self.fp_label.pack(anchor="w")

        tools = tk.Frame(top, bg=C["sidebar"])
        tools.pack(side="right", padx=12)
        self.requests_btn = _btn(tools, t("🔔 新朋友"), self.open_requests, "primary")
        self.requests_btn.pack(side="right")
        _btn(tools, t("➕ 添加联系人"), self.open_add_contacts, "ghost").pack(side="right", padx=8)
        self.answer_btn = _btn(tools, t("✋ 回答问题"), self.open_pending_answers, "ghost")
        self.answer_btn.pack(side="right")
        self.notice_btn = _btn(tools, t("📢 通知"), self.open_notices, "ghost")
        self.notice_btn.pack(side="right", padx=8)
        _btn(tools, t("⚙ 设置"), self.open_settings, "ghost").pack(side="right")
        if getattr(self.args, "self_test", False):
            # 只在自测模式里出现的按钮: 真的把本窗口下线几秒, 再让它自己回来
            self.offline_btn = _btn(tools, t("🧪 掉线 6 秒"), self.simulate_offline, "ghost")
            self.offline_btn.pack(side="right", padx=8)

        body = tk.Frame(self, bg=C["panel"])
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        left = tk.Frame(body, bg=C["sidebar"], width=250)
        left.grid(row=0, column=0, sticky="nsw")
        left.grid_propagate(False)
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        search = tk.Frame(left, bg=C["sidebar"], padx=8, pady=8)
        search.grid(row=0, column=0, sticky="ew")
        tk.Label(search, text="🔍", bg=C["sidebar"], fg=C["muted"]).pack(side="left")
        self.filter_var = tk.StringVar(master=self.master_window)
        tk.Entry(search, textvariable=self.filter_var, font=FONT_SMALL, relief="flat",
                 bg=C["panel"]).pack(fill="x", side="left", ipady=3, padx=4)
        self.filter_var.trace_add("write", lambda *_: self.refresh_contacts())

        self.filter_label = tk.Label(left, text="", font=FONT_SMALL, bg=C["sidebar"], fg=C["muted"],
                                     anchor="w", padx=10)
        self.filter_label.grid(row=1, column=0, sticky="ew")

        self.contact_list = tk.Listbox(left, font=FONT, bg=C["sidebar"], fg=C["text"],
                                       selectbackground=C["sidebar_sel"], selectforeground=C["text"],
                                       relief="flat", highlightthickness=0, activestyle="none",
                                       exportselection=False, borderwidth=0)
        self.contact_list.grid(row=2, column=0, sticky="nsew")
        self.contact_list.bind("<<ListboxSelect>>", self._on_select)
        self.contact_list.bind("<Button-3>", self._popup_menu)

        self.list_menu = tk.Menu(self, tearoff=0)
        self.list_menu.add_command(label=t("发送文件"), command=self.send_file)
        self.list_menu.add_command(label=t("重新连接"), command=self._menu_reconnect)
        self.list_menu.add_separator()
        self.list_menu.add_command(label=t("取消加好友请求"), command=self._menu_cancel_request)
        self.list_menu.add_command(label=t("设置验证问题"), command=self._menu_set_question)
        self.list_menu.add_command(label=t("删除好友"), command=self._menu_remove)
        self.list_menu.add_command(label=t("封禁此人"), command=self._menu_block)
        self.list_menu.add_command(label=t("解除封禁"), command=self._menu_unblock)

        right = tk.Frame(body, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        chat_header = tk.Frame(right, bg=C["panel"], height=44)
        chat_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        chat_header.grid_propagate(False)
        self.chat_title = tk.Label(chat_header, text="", font=FONT_BOLD, bg=C["panel"],
                                   fg=C["text"])
        self.chat_title.pack(side="left", padx=14)
        self.chat_state = tk.Label(chat_header, text="", font=FONT_SMALL, bg=C["panel"],
                                   fg=C["muted"])
        self.chat_state.pack(side="left")
        self.chat_fp = tk.Label(chat_header, text="", font=("Consolas", 8), bg=C["panel"],
                                fg=C["muted"])
        self.chat_fp.pack(side="right", padx=14)
        tk.Frame(right, bg=C["border"], height=1).grid(row=0, column=0, columnspan=2, sticky="sew")

        self.chat = tk.Text(right, wrap="word", state="disabled", font=FONT, relief="flat",
                            bg=C["bg"], padx=14, pady=10, cursor="arrow")
        self.chat.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(right, orient="vertical", command=self.chat.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scroll.set)
        for tag, color in (("out", C["text"]), ("in", C["text"]), ("meta", C["muted"]),
                           ("warn", C["warn"]), ("err", C["err"]), ("sys", C["muted"]),
                           ("link", "#576b95")):
            self.chat.tag_configure(tag, foreground=color)
        self.chat.tag_configure("out", justify="right")
        self.chat.tag_bind("link", "<Button-1>", self._open_link)
        self.chat.tag_bind("link", "<Enter>", lambda _e: self.chat.configure(cursor="hand2"))
        self.chat.tag_bind("link", "<Leave>", lambda _e: self.chat.configure(cursor="arrow"))

        bottom = tk.Frame(right, bg=C["panel"])
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.entry = tk.Text(bottom, height=4, font=FONT, relief="flat", bg=C["panel"],
                             padx=10, pady=8, wrap="word")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", self._on_enter)
        buttons = tk.Frame(bottom, bg=C["panel"], padx=8, pady=6)
        buttons.grid(row=0, column=1, sticky="s")
        _btn(buttons, t("发送"), self.send_message, "primary", width=6).pack(side="right")
        _btn(buttons, t("📎 文件"), self.send_file, "ghost", width=7).pack(side="right", padx=6)
        self.progress_label = tk.Label(bottom, text="", font=FONT_SMALL, bg=C["panel"],
                                       fg=C["muted"], anchor="w", padx=10)
        self.progress_label.grid(row=1, column=0, columnspan=2, sticky="ew")
        # 传输进度条 + 速度 (平时隐藏, 有传输时才出现)
        self.progress_bar = ttk.Progressbar(bottom, orient="horizontal", mode="determinate",
                                            maximum=100, length=200)
        self._transfers: Dict[str, Dict[str, float]] = {}   # 传输状态: 算速度/剩余时间

        self.status = tk.Label(self, text="", font=FONT_SMALL, bg=C["sidebar"], fg=C["muted"],
                               anchor="w", padx=12)
        self.status.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # 联系人列表
    # ------------------------------------------------------------------
    def _visible_contacts(self) -> List[Contact]:
        keyword = self.filter_var.get().strip().lower()
        items = [c for c in self.service.contacts()
                 if c.is_friend or c.state in (ContactState.REQUEST_OUT.value,
                                               ContactState.BLOCKED.value)]
        if keyword:
            items = [c for c in items
                     if keyword in c.name.lower() or keyword in c.fingerprint.lower()]
        return items

    def refresh_contacts(self) -> None:
        contacts = self._visible_contacts()
        self.contact_list.delete(0, "end")
        self._row_ids = []
        for contact in contacts:
            self.contact_list.insert("end", self._format_row(contact))
            self._row_ids.append(contact.peer_id)
        if self._current and self._current in self._row_ids:
            index = self._row_ids.index(self._current)
            self.contact_list.selection_clear(0, "end")
            self.contact_list.selection_set(index)
        if self._current and self._current not in self._row_ids:
            self._current = None
            self.service.set_active_peer("")      # 这个人不在列表里了, 新消息要重新算未读
            self._show_welcome()
        pending = len(self.service.pending_requests())
        self.filter_label.configure(text=t("好友 {0} 人").format(len(self.service.friends()))
                                         + (t(" · 待处理请求 {0}").format(pending) if pending else ""))
        self.me_label.configure(text=t("我: {0}").format(self.service.name))
        self.fp_label.configure(text=t("指纹 {0}").format(self.service.fingerprint))
        self.requests_btn.configure(text=t("🔔 新朋友 ({0})").format(pending) if pending else t("🔔 新朋友"))
        self._update_header()
        self._call_hooks("contacts")

    def _format_row(self, contact: Contact) -> str:
        if contact.blocked:
            mark = "🚫"
        elif contact.blocked_by_peer:
            mark = "⛔"
        elif contact.encrypted:
            mark = "🔒"
        elif contact.connected:
            mark = "🔑"
        elif contact.is_friend:
            mark = "●" if contact.dial_state == "connecting" else "○"
        else:
            mark = "…"
        parts: List[str] = []
        if contact.state == ContactState.REQUEST_OUT.value:
            parts.append(t("[等待验证]"))
        if contact.blocked_by_peer:
            parts.append(t("[对方封禁了你]"))
        elif contact.blocked:
            parts.append(t("[已封禁]") + (t("(解禁后仍是好友)") if contact.friend_before_block else ""))
        if contact.is_friend and not contact.connected:
            parts.append(t("[离线]"))           # 好友离线也要看得出来
        if self.service.has_question_for(contact.peer_id):
            parts.append("🧩")
        suffix = (" " + " ".join(parts)) if parts else ""
        unread = f"  ({contact.unread})" if contact.unread else ""
        return f" {mark} {contact.name}{suffix}{unread}"

    def _on_select(self, _event: Any = None) -> None:
        selection = self.contact_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index >= len(self._row_ids):
            return
        peer_id = self._row_ids[index]
        if peer_id == self._current:
            return
        self._current = peer_id
        self.service.set_active_peer(peer_id)     # 正开着的对话不攒未读
        self.service.mark_read(peer_id)
        self._render_history()
        self._update_header()

    def select_contact(self, peer_id: str) -> None:
        self._current = peer_id
        # 告诉服务"现在开着谁的对话": 消息会直接显示在眼前, 不该再攒未读
        self.service.set_active_peer(peer_id)
        self.refresh_contacts()
        self.service.mark_read(peer_id)
        self._render_history()
        self._update_header()

    def _current_contact(self) -> Optional[Contact]:
        return self.service.get_contact(self._current) if self._current else None

    def _update_header(self) -> None:
        contact = self._current_contact()
        if contact is None:
            self.chat_title.configure(text="")
            self.chat_state.configure(text="")
            self.chat_fp.configure(text="")
            return
        self.chat_title.configure(text=contact.name)
        if contact.blocked_by_peer:
            state, color = t("对方把你封禁了 (对方解禁时会通知你)"), C["err"]
        elif contact.blocked:
            state = t("已封禁") + (t(" (解禁后仍是好友)") if contact.friend_before_block else "")
            color = C["err"]
        elif contact.encrypted:
            session = contact.connection.session_id if contact.connection else ""
            rekeys = contact.connection.rekeys_done if contact.connection else 0
            state = t("🔒 加密连接中 (会话 {0}").format(session)
            state += t(", 本会话已轮换密钥 {0} 次)").format(rekeys) if rekeys else ")"
            color = C["green_dark"]
        elif contact.connected:
            state, color = t("🔑 通道已建立 (等待对方确认好友)"), C["warn"]
        elif contact.state == ContactState.REQUEST_OUT.value:
            state, color = t("等待对方同意加好友"), C["warn"]
        elif contact.state == ContactState.REQUEST_IN.value:
            state, color = t("对方请求加你为好友 (在『新朋友』里处理)"), C["warn"]
        elif contact.dial_state == "connecting":
            state, color = t("正在连接…"), C["muted"]
        else:
            detail = f" · {contact.last_error}" if contact.last_error else ""
            state, color = t("未连接{0}").format(detail), C["muted"]
        self.chat_state.configure(text=state, fg=color)
        self.chat_fp.configure(text=t("指纹 {0}").format(contact.fingerprint))

    # ------------------------------------------------------------------
    # 聊天记录
    # ------------------------------------------------------------------
    def _render_history(self) -> None:
        contact = self._current_contact()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        if contact is not None:
            # 先把本程序记下的这段对话画出来 (切换联系人/补发历史后回来还能看到)
            for tag, text, ts in self._chat_log.get(contact.peer_id, []):
                self._insert_line(tag, text, ts)
            if contact.state == ContactState.REQUEST_IN.value:
                self._append("sys", t("{0} 请求加你为好友, 点上方『🔔 新朋友』处理。同意之后才会建立加密会话。").format(contact.name))
            elif contact.state == ContactState.REQUEST_OUT.value:
                self._append("sys", t("已向 {0} 发出加好友请求, 等待对方确认。").format(contact.name))
            elif contact.blocked_by_peer:
                self._append("sys", t("{0} 把你封禁了。对方解禁时会通知你, 之后你可以重新点『➕ 添加联系人』发送加好友请求。").format(contact.name))
            elif contact.blocked:
                self._append("sys", t("你已封禁 {0} (暂时拒收他的消息和请求)。").format(contact.name)
                                    + (t("解禁后你们仍然是好友。") if contact.friend_before_block
                                       else t("解禁后他可以重新申请加你。"))
                                    + t("右键联系人可解除封禁。"))
            elif contact.is_friend and not contact.connected:
                self._append("sys", t("对方不在线。对方上线后会自动重新建立加密连接。"))
            if self.service.has_question_for(contact.peer_id):
                self._append("sys", t("已设置验证问题: {0} (答错 {1} 次自动封禁)").format(self.service.question_of(contact.peer_id), self.service.max_attempts_for(contact.peer_id)))
            if contact.encrypted and contact.connection is not None:
                self._append("sys", t("本次会话已用临时密钥协商: {0} (第 {1} 轮密钥)").format(contact.connection.session_id, contact.connection.cipher.epoch + 1))
        self.chat.configure(state="disabled")

    def _append(self, tag: str, text: str, extra: tuple = (), ts: Optional[float] = None,
                peer_id: Optional[str] = None) -> None:
        """往聊天区加一行; 同时记进这个联系人的内存记录 (peer_id 给了才记)。"""
        if peer_id:
            self._remember(peer_id, tag, text, ts)
            if peer_id != self._current:
                return                      # 不在当前会话里: 只记录, 不画
        self._insert_line(tag, text, ts, extra)

    def _insert_line(self, tag: str, text: str, ts: Optional[float] = None,
                     extra: tuple = ()) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else time.strftime("%H:%M:%S")
        self.chat.configure(state="normal")
        self.chat.insert("end", f"[{stamp}] ", ("meta",))
        self.chat.insert("end", text + "\n", (tag,) + extra)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def _remember(self, peer_id: str, tag: str, text: str, ts: Optional[float] = None) -> None:
        """记一行到该联系人的内存聊天记录 (最多留 500 行, 关掉程序就没了)。"""
        if not peer_id or not text:
            return
        log = self._chat_log.setdefault(peer_id, [])
        log.append((tag, text, ts or time.time()))
        if len(log) > 500:
            del log[:-500]

    def _open_link(self, event: tk.Event) -> None:
        index = self.chat.index(f"@{event.x},{event.y}")
        ranges = self.chat.tag_ranges("link")
        for start, end in zip(ranges[::2], ranges[1::2]):
            if self.chat.compare(start, "<=", index) and self.chat.compare(index, "<", end):
                self._reveal_path(self.chat.get(start, end))
                return

    def _reveal_path(self, path: str) -> None:
        directory = os.path.dirname(path)
        try:
            if sys.platform == "win32":
                os.startfile(directory)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", directory])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", directory])
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo(t("文件位置"), f"{path}\n({exc})")

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _drain(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self._closing:
            self._drain_job = self.after(80, self._drain)

    def _handle(self, event: ServiceEvent) -> None:
        kind = event.kind
        # 需要让用户看见的事 (被删好友 / 被封禁 / 对方取消请求 / 上下线) 统一进通知中心
        if kind in (EventKind.CONNECTED, EventKind.DISCONNECTED, EventKind.ERROR,
                    EventKind.INFO):
            self._handle_notice_event(event)
        if kind is EventKind.CONTACTS:
            self.refresh_contacts()
            return
        if kind is EventKind.LAN_PEERS:
            self._call_hooks("lan-peers")
            return
        if kind is EventKind.MESSAGE:
            self._on_message_event(event)
            return
        if kind is EventKind.HISTORY:
            # 对方补发的历史对话 (我重启过): 记一笔提示, 具体内容跟着一条条 MESSAGE 事件来
            self._append("sys", event.text, peer_id=event.peer_id)
            self.status.configure(text=event.text)
            return
        if kind is EventKind.FILE_OFFER:
            self._on_file_offer(event)
            return
        if kind is EventKind.FILE_PROGRESS:
            self._on_file_progress(event)
            return
        if kind is EventKind.FILE_DONE:
            path = str(event.data.get("path", ""))
            self._remember(event.peer_id, "in", t("📥 已接收文件: {0}").format(path))
            if self._current == event.peer_id:
                self.chat.configure(state="normal")
                self.chat.insert("end", f"[{time.strftime('%H:%M:%S')}] ", ("meta",))
                self.chat.insert("end", t("📥 已接收文件: "), ("in",))
                self.chat.insert("end", path, ("link",))
                self.chat.insert("end", "\n")
                self.chat.see("end")
                self.chat.configure(state="disabled")
            self.status.configure(text=t("文件已保存: {0}").format(path))
            self.record_notice(t("文件已保存: {0}").format(path), event.peer_id, "info")
            return
        if kind is EventKind.FILE_FAILED:
            self._append("err", event.text, peer_id=event.peer_id)
            return
        if kind is EventKind.QUESTION:
            self._on_question(event)
            return
        if kind is EventKind.QUESTION_RESULT:
            dialog = self._answer_dialogs.get(event.peer_id)
            if dialog is not None:
                dialog.on_result(bool(event.data.get("ok")),
                                 int(event.data.get("attempts_left", 0) or 0))
            if event.text:
                self.record_notice(event.text, event.peer_id,
                                   "info" if event.data.get("ok") else "warn")
                self._append("sys" if event.data.get("ok") else "err", event.text,
                             peer_id=event.peer_id)
            return
        if kind in (EventKind.CONNECTED, EventKind.DISCONNECTED):
            self._append("sys", event.text, peer_id=event.peer_id)
            self.refresh_contacts()
            return
        if kind is EventKind.STATUS:
            self.refresh_contacts()
            return
        if kind in (EventKind.PRESENCE, EventKind.READY, EventKind.INFO, EventKind.ERROR):
            if event.text:
                self.status.configure(text=event.text)
            if event.peer_id and kind is not EventKind.PRESENCE:
                self._append("err" if kind is EventKind.ERROR else "sys", event.text,
                             peer_id=event.peer_id)
            return

    def _on_message_event(self, event: ServiceEvent) -> None:
        direction = event.data.get("direction", "in")
        contact = self.service.get_contact(event.peer_id)
        name = contact.name if contact else event.name
        text = str(event.data.get("text", event.text))
        ts = float(event.data.get("ts", 0) or 0) or None
        line = f"{name}: {text}" if direction == "in" else t("我: {0}").format(text)
        self._append("in" if direction == "in" else "out", line,
                     ts=ts, peer_id=event.peer_id)
        if self._current != event.peer_id:
            self.status.configure(
                text=t("{0} — {1}: {2}").format(t('收到') if direction == 'in' else t('发出'), name, text[:24]))

    def _on_question(self, event: ServiceEvent) -> None:
        """对方出了验证问题 -> 弹窗让用户回答。

        同一次提问只弹一个窗; 对方重新提问(答错之后再发请求)会带着新的随机数过来,
        这时要再弹一次 —— 否则答错一次就永远没机会重答了。
        """
        question = str(event.data.get("question", ""))
        nonce = str(event.data.get("nonce", ""))
        if not question:
            return
        existing = self._answer_dialogs.get(event.peer_id)
        if existing is not None:
            try:
                alive = bool(existing.win.winfo_exists())
            except tk.TclError:
                alive = False
            if alive and (not nonce or existing.nonce == nonce):
                existing.win.lift()
                return
            if alive:
                existing.close()
        if self._current == event.peer_id:
            self._append("warn", t("{0} 的验证问题: {1}").format(event.name, question), peer_id=event.peer_id)
        dialog = AnswerQuestionDialog(
            self, event.peer_id, question,
            int(event.data.get("max_attempts", DEFAULT_MAX_ANSWER_ATTEMPTS)), nonce=nonce)
        self._answer_dialogs[event.peer_id] = dialog
        self._call_hooks("questions")
        self._update_answer_button()

    def open_pending_answers(self) -> None:
        """『✋ 回答问题』: 打开还没回答的验证问题 (不用先选中谁)。"""
        items = self.service.pending_challenges()
        if not items:
            messagebox.showinfo(
                t("验证问题"),
                t("现在没有需要回答的验证问题。\n\n"
                "对方设了验证问题时, 你发完加好友请求就会自动弹出题目, "
                "也可以随时点这个按钮来回答。"))
            return
        item = items[0]
        dialog = self._answer_dialogs.get(item["peer_id"])
        if dialog is not None:
            try:
                if dialog.win.winfo_exists():
                    dialog.win.lift()
                    return
            except tk.TclError:
                pass
        self._on_question(ServiceEvent(kind=EventKind.QUESTION, text=item["question"],
                                       peer_id=item["peer_id"], name=item["name"],
                                       data={"question": item["question"],
                                             "nonce": item["nonce"],
                                             "max_attempts": item["max_attempts"]}))

    def _forget_answer_dialog(self, peer_id: str, dialog: "AnswerQuestionDialog") -> None:
        if self._answer_dialogs.get(peer_id) is dialog:
            self._answer_dialogs.pop(peer_id, None)
        self._update_answer_button()
        self._call_hooks("questions")

    def _update_answer_button(self) -> None:
        try:
            pending = len(self.service.pending_challenges())
        except Exception:  # noqa: BLE001 - 界面刷新不能影响主流程
            pending = 0
        if pending:
            self.answer_btn.configure(text=t("✋ 回答问题 ({0})").format(pending), bg=C["warn"], fg="#ffffff",
                                      activebackground="#e08b2a")
        else:
            self.answer_btn.configure(text=t("✋ 回答问题"), bg=C["sidebar"], fg=C["text"],
                                      activebackground=C["border"])
        unread = sum(1 for item in self.notices if not item["read"])
        self.notice_btn.configure(text=t("📢 通知 ({0})").format(unread) if unread else t("📢 通知"),
                                  bg=C["warn"] if unread else C["sidebar"],
                                  fg="#ffffff" if unread else C["text"],
                                  activebackground="#e08b2a" if unread else C["border"])

    # ------------------------------------------------------------------
    # 通知中心: "对面把你删了/下线了/取消了请求" 这类事要看得见
    # ------------------------------------------------------------------
    def record_notice(self, text: str, peer_id: str = "", level: str = "info",
                      popup: bool = False) -> None:
        """记一条通知; popup=True 时同时弹窗 (重要的事不能只写在小字状态栏里)。"""
        if not text:
            return
        self.notices.append({"ts": time.time(), "text": text, "peer_id": peer_id,
                             "level": level, "read": False})
        del self.notices[:-100]                # 只留最近 100 条
        self._update_answer_button()
        if popup:
            title = {"warn": t("提示"), "err": t("注意")}.get(level, t("通知"))
            try:
                messagebox.showinfo(title, text)
            except tk.TclError:
                pass

    def open_notices(self) -> None:
        NotificationsDialog(self)

    def _handle_notice_event(self, event: ServiceEvent) -> bool:
        """把"需要让用户看见"的事件收进通知中心; 返回是否已处理。"""
        data = event.data or {}
        if not data.get("notice"):
            return False
        text = event.text or ""
        if not text:
            return False
        # 上线/下线这类只在通知列表里记一笔 (每次断线都弹窗太吵),
        # 被删除好友 / 被封禁 / 对方取消请求这类必须弹窗, 否则用户只看到"人没了"。
        self.record_notice(text, event.peer_id, str(data.get("level", "info")),
                           popup=not data.get("silent"))
        return True

    def _on_file_offer(self, event: ServiceEvent) -> None:
        name = str(event.data.get("name"))
        size = human_size(event.data.get("size", 0))
        if event.data.get("direction") == "out":
            self._append("out", t("我: 📎 发送文件 {0} ({1})").format(name, size), peer_id=event.peer_id)
            return
        self._append("in", t("{0}: 📎 发来文件 {1} ({2})").format(event.name, name, size), peer_id=event.peer_id)
        if event.data.get("auto_accepted"):
            # 设置里开了"自动接收": 不再弹窗, 但也不能悄无声息地存下去
            self.record_notice(
                t("已自动接收 {0} 的文件 {1} ({2}), 保存到 {3} (可在『⚙ 设置』里关掉自动接收, 改成每次自己选位置)").format(event.name, name, size, self.service.download_dir),
                event.peer_id, "info")
            return
        # 让用户可以自己选保存位置 (默认目录 / 选个位置 / 拒绝)
        dialog = IncomingFileDialog(self, event.peer_id, str(event.name), name,
                                    event.data.get("size", 0),
                                    str(event.data.get("transfer_id", "")))
        self._incoming_file_dialogs[dialog.transfer_id] = dialog

    def _after(self, ms: int, func: Any) -> str:
        """排一个定时任务并记下来: 窗口关掉后不再触发 (否则 Tcl 会报 invalid command name)。"""
        job = self.after(ms, func)
        self._after_jobs.append(job)
        return job

    def _cancel_after_jobs(self) -> None:
        for job in list(self._after_jobs):
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._after_jobs.clear()

    def _clear_progress(self) -> None:
        try:
            self.progress_label.configure(text="")
            self.progress_bar.grid_remove()
            self.progress_bar.configure(value=0)
        except tk.TclError:
            pass
        self._transfers.clear()

    def _on_file_progress(self, event: ServiceEvent) -> None:
        key = str(event.data.get("transfer_id"))
        now = time.time()
        progress = float(event.data.get("progress", 0)) * 100
        if now - self._last_progress_at.get(key, 0.0) < 0.3 and progress < 100:
            return
        self._last_progress_at[key] = now
        direction = t("发送") if event.data.get("direction") == "out" else t("接收")
        sent = float(event.data.get("sent", 0) or 0)
        size = float(event.data.get("size", 0) or 0)

        # 速度: 用两次采样的差值算, 再做指数平滑, 避免数字乱跳
        state = self._transfers.setdefault(key, {"sent": 0.0, "t": now, "speed": 0.0})
        dt = now - state["t"]
        if dt > 0.05:
            instant = max(0.0, (sent - state["sent"]) / dt)
            state["speed"] = instant if not state["speed"] else state["speed"] * 0.6 + instant * 0.4
            state["sent"] = sent
            state["t"] = now
        speed = state["speed"]
        eta = ""
        if speed > 1 and size > sent:
            left = (size - sent) / speed
            eta = t(" · 剩余约 {0}s").format(int(left)) if 1 <= left < 3600 else ""
        self.progress_label.configure(
            text=f"{direction} {event.data.get('name')}: {progress:.0f}% "
                 f"({human_size(sent)}/{human_size(size)})"
                 + (f" · {human_size(speed)}/s" if speed > 1 else "") + eta)
        try:
            self.progress_bar.configure(value=max(0.0, min(100.0, progress)))
            self.progress_bar.grid(row=1, column=2, sticky="e", padx=10)
        except tk.TclError:
            pass
        if progress >= 100:
            speed_txt = t(" · 平均 {0}/s").format(human_size(sent)) if sent and state["t"] else ""
            self.progress_label.configure(
                text=t("{0} {1} 完成 ({2}){3}").format(direction, event.data.get('name'), human_size(sent), speed_txt))
            self._after(2500, self._clear_progress)

    def _tick(self) -> None:
        if self._closing:
            return
        online = [c for c in self.service.friends() if c.connected]
        # 注意: 轮换次数是"每一条会话"各自的计数 (重连就重新开始), 所以这里不跨会话求和 ——
        # 求和出来的数字跟对方窗口对不上, 会让人以为出错了。要看次数请看聊天窗口标题旁那一行。
        rekey = self.service.rekey_every
        rekey_tip = t(" · 密钥轮换 每 {0} 条").format(rekey) if rekey else t(" · 密钥轮换已关闭")
        stealth = "" if self.service.discoverable else t(" · 🕶 隐身中 (别人搜不到你)")
        self.status.configure(
            text=t("我是「{0}」 · 好友 {1} 人 · 已连接 {2} 人 · TCP 端口 {3}{4}{5} · 接收目录 {6}").format(self.service.name, len(self.service.friends()), len(online), self.service.tcp_port, rekey_tip, stealth, self.service.download_dir)
        )
        self._update_answer_button()
        self._tick_job = self.after(1000, self._tick)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _show_welcome(self) -> None:
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.insert("end", t("欢迎使用局域网聊天\n\n"), ("sys",))
        self.chat.insert("end", (
            t("""1. 点右上角「➕ 添加联系人」查看局域网里自动搜索到的人
2. 选中某人发送加好友请求, 对方在「🔔 新朋友」里同意后即可聊天
3. 对方设了验证问题时会自动弹窗提问; 关掉了也能点「✋ 回答问题」继续答
4. 聊天与文件全部端到端加密, 每收发若干条消息会重新协商会话密钥
5. 右键左侧联系人可以 发送文件 / 重连 / 设验证问题 / 删除 / 封禁 / 解禁

我的身份指纹: {0} (可在『⚙ 设置』里复制给对方核对)
""").format(self.service.fingerprint)
        ), ("sys",))
        self.chat.configure(state="disabled")

    def _on_enter(self, event: tk.Event) -> str:
        if event.state & 0x0001:      # Shift+Enter 换行
            return ""
        self.send_message()
        return "break"

    def send_message(self) -> None:
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        contact = self._current_contact()
        if contact is None:
            messagebox.showinfo(t("发送"), t("请先在左侧选择一位好友"))
            return
        if contact.state == ContactState.REQUEST_IN.value:
            messagebox.showinfo(t("还不是好友"),
                                t("{0} 请求加你为好友, 请先在『🔔 新朋友』里同意。").format(contact.name))
            return
        if contact.blocked_by_peer:
            messagebox.showinfo(t("发送"),
                                t("""对方({0})把你封禁了, 现在发不出去。
对方解禁时会通知你, 之后可以再试。""").format(contact.name))
            return
        if contact.blocked:
            messagebox.showinfo(t("发送"), t("你已封禁 {0}").format(contact.name))
            return
        if not contact.is_friend:
            messagebox.showinfo(t("发送"), t("{0} 还不是你的好友, 请先发送加好友请求。").format(contact.name))
            return
        # 是好友但此刻没连上: **照样发** —— 消息会记在本会话记录里, 对方一上线自动补发
        # (以前这里直接拦住, 用户只能等对方上线再重打一遍)。
        self.entry.delete("1.0", "end")
        self.service.send_text(text, contact.peer_id)

    def send_file(self) -> None:
        contact = self._current_contact()
        if contact is None:
            messagebox.showinfo(t("发送文件"), t("请先在左侧选择一位好友"))
            return
        if not contact.encrypted:
            messagebox.showinfo(t("发送文件"), t("与 {0} 还没有可用的加密连接").format(contact.name))
            return
        path = filedialog.askopenfilename(title=t("选择要发送的文件"))
        if not path:
            return
        if not messagebox.askyesno(
                t("发送文件"),
                t("""把 {0} ({1}) 发给 {2}?
对方同意接收后才会开始传输。""").format(os.path.basename(path), human_size(os.path.getsize(path)), contact.name)):
            return
        self.service.send_file(path, contact.peer_id)

    def open_add_contacts(self) -> None:
        ContactsDialog(self)

    def open_requests(self) -> None:
        FriendRequestsDialog(self)

    def open_settings(self) -> None:
        SettingsDialog(self)

    def simulate_offline(self) -> None:
        """自测专用: 让这个窗口**真的**掉线几秒再自己回来。

        验证的东西: 对面会看到"对方不在线" → 到点我重新监听+广播 → 双方自动重连 →
        对面把这期间缺的聊天记录补发给我 (见 README 5.7)。
        """
        if not self.service.simulate_offline(6.0):
            self._append("sys", t("现在不能模拟掉线 (服务没在跑, 或者上一次还没结束)"))
            return
        self._append("sys", t("🧪 模拟掉线 6 秒: 这几秒里对面会显示『对方不在线』; "
                            "到点我会重新上线, 双方自动重连, 对面还会把缺的对话补发给我。"))

    def _menu_reconnect(self) -> None:
        contact = self._current_contact()
        if contact:
            self.service.connect_now(contact.peer_id)

    def _menu_set_question(self) -> None:
        contact = self._current_contact()
        if contact:
            SetQuestionDialog(self, contact)

    def _menu_cancel_request(self) -> None:
        """取消自己发出的加好友请求 (对方还没同意时)。"""
        contact = self._current_contact()
        if contact is None:
            messagebox.showinfo(t("取消请求"), t("请先在左侧选中一个人"))
            return
        if contact.is_friend:
            messagebox.showinfo(t("取消请求"), t("{0} 已经是你的好友了").format(contact.name))
            return
        if not messagebox.askyesno(
                t("取消加好友请求"),
                t("""取消发给 {0} 的加好友请求?
对方那边这道验证问题也会作废, 以后想加需要重新发请求。""").format(contact.name)):
            return
        self.service.cancel_friend_request(contact.peer_id)

    def _menu_remove(self) -> None:
        contact = self._current_contact()
        if contact and messagebox.askyesno(t("删除好友"), t("确定删除好友 {0}?").format(contact.name)):
            self.service.remove_friend(contact.peer_id)

    def _menu_block(self) -> None:
        contact = self._current_contact()
        if contact and messagebox.askyesno(
                t("封禁"), t("""封禁 {0}?
对方之后发来的请求和消息都会被拒收。""").format(contact.name)
                + (t("\n\n你们还是好友: 解禁之后关系照旧, 消息也能继续发。")
                   if contact.is_friend else "")):
            self.service.block(contact.peer_id)

    def _menu_unblock(self) -> None:
        contact = self._current_contact()
        if contact and (contact.blocked or contact.blocked_by_peer):
            had_my_block = bool(contact.blocked)
            was_friend = bool(contact.friend_before_block or contact.is_friend)
            name = contact.name
            if self.service.unblock(contact.peer_id):
                messagebox.showinfo(
                    t("解除封禁"),
                    unblock_text(name, had_my_block, was_friend,
                                 self.service.has_question_for(contact.peer_id)))

    def _popup_menu(self, event: tk.Event) -> None:
        index = self.contact_list.nearest(event.y)
        if 0 <= index < len(self._row_ids):
            self.contact_list.selection_clear(0, "end")
            self.contact_list.selection_set(index)
            self._on_select()
            self.list_menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------
    # 子窗口刷新钩子
    # ------------------------------------------------------------------
    def register_hook(self, name: str, callback: Any) -> None:
        self._hooks.setdefault(name, []).append(callback)

    def unregister_hook(self, name: str, callback: Any) -> None:
        if callback in self._hooks.get(name, []):
            self._hooks[name].remove(callback)

    def _call_hooks(self, name: str) -> None:
        for callback in list(self._hooks.get(name, [])):
            try:
                callback()
            except tk.TclError:
                self.unregister_hook(name, callback)

    # ------------------------------------------------------------------
    def on_close(self) -> None:
        if not messagebox.askokcancel(t("退出"), t("确定要退出吗? 退出后别人将看不到你。")):
            return
        self._closing = True
        self.service.set_active_peer("")     # 关窗口后不该再把新消息当"已读"
        self._cancel_after_jobs()
        try:
            from .winfocus import cancel_pending
            cancel_pending(self.master_window)
        except Exception:  # noqa: BLE001
            pass
        for job in (getattr(self, "_drain_job", None), getattr(self, "_tick_job", None)):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.status.configure(text=t("正在退出…"))
        self.update_idletasks()
        if self.own_service:
            threading.Thread(target=self.service.stop, daemon=True).start()
        if self.on_close_hook:
            try:
                self.on_close_hook()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.15)
        try:
            self.master_window.destroy()
        except tk.TclError:
            # 自测模式里两个窗口共用一个进程: 另一个窗口可能已经把解释器关掉了
            pass


# ===========================================================================
# 设置
# ===========================================================================
class SettingsDialog:
    def __init__(self, app: "ChatApp") -> None:
        self.app = app
        self.svc = app.service
        self.win = tk.Toplevel(app)
        self.win.title(t("设置"))
        self.win.configure(bg=C["panel"])
        width, height = 620, 880
        try:                       # 内容有 870 多像素高: 屏幕够大就一次显示完, 不够才滚动
            height = min(height, max(420, self.win.winfo_screenheight() - 140))
            width = min(width, max(500, self.win.winfo_screenwidth() - 80))
        except tk.TclError:
            pass
        self.win.geometry(f"{width}x{height}")
        self.win.minsize(560, 400)
        self.win.transient(app)

        # 设置项越加越多, 内容会比窗口高 —— 以前是写死 580x600, 结果新增的"本机端口"
        # 和下面的接收目录/关闭按钮全被切掉, 用户根本点不到 (报过)。
        # 现在: 内容放进可滚动区域, 底部『关闭』固定住, 以后再加东西也不会被挤没。
        outer = tk.Frame(self.win, bg=C["panel"])
        outer.pack(fill="both", expand=True)
        self.body_canvas = tk.Canvas(outer, bg=C["panel"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.body_canvas.yview)
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.body_canvas.pack(side="left", fill="both", expand=True)

        frame = tk.Frame(self.body_canvas, bg=C["panel"], padx=18, pady=16)
        self._body_item = self.body_canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>",
                   lambda _e: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))
        self.body_canvas.bind(
            "<Configure>",
            lambda e: self.body_canvas.itemconfigure(self._body_item, width=e.width))

        footer = tk.Frame(self.win, bg=C["panel"], padx=18, pady=10)
        footer.pack(side="bottom", fill="x")
        self.close_btn = _btn(footer, t("关闭"), lambda: _close_toplevel(self.win), "ghost", width=6)
        self.close_btn.pack(side="right")

        tk.Label(frame, text=t("昵称"), font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        row = tk.Frame(frame, bg=C["panel"])
        row.pack(fill="x", pady=(4, 12))
        self.name_var = tk.StringVar(master=self.win, value=self.svc.name)
        tk.Entry(row, textvariable=self.name_var, font=FONT, relief="flat",
                 bg=C["bg"]).pack(side="left", fill="x", expand=True, ipady=4)
        _btn(row, t("保存"), self.save_name, "primary").pack(side="left", padx=8)

        tk.Label(frame, text=t("我的身份指纹 (发给朋友核对, 可确认没有中间人)"),
                 font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        fp_row = tk.Frame(frame, bg=C["panel"])
        fp_row.pack(fill="x", pady=(4, 12))
        tk.Label(fp_row, text=self.svc.fingerprint, font=("Consolas", 13, "bold"),
                 bg=C["panel"], fg=C["text"]).pack(side="left")
        _btn(fp_row, t("复制"), self.copy_fingerprint, "ghost").pack(side="left", padx=8)

        tk.Label(frame, text=t("界面语言 (改完点『应用』, 重启后生效)"), font=FONT_BOLD,
                 bg=C["panel"], fg=C["text"]).pack(anchor="w")
        lang_row = tk.Frame(frame, bg=C["panel"])
        lang_row.pack(fill="x", pady=(4, 12))
        self._lang_codes = i18n.available()
        self.lang_var = tk.StringVar(
            master=self.win,
            value=i18n.display_name(self.svc.language or i18n.current_language()))
        combo = ttk.Combobox(lang_row, state="readonly", width=30, font=FONT_SMALL,
                             textvariable=self.lang_var,
                             values=[i18n.display_name(code) for code in self._lang_codes])
        combo.pack(side="left")
        _btn(lang_row, t("应用"), self.apply_language, "ghost").pack(side="left", padx=8)
        tk.Label(frame, text=(t("语言选择会记进 settings.json, 下次启动生效 "
                               "(不影响已经收到的消息和文件)。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(0, 12))

        opts = tk.Frame(frame, bg=C["panel"])
        opts.pack(fill="x", pady=(0, 12))
        self.auto_file_var = tk.BooleanVar(master=self.win, value=self.svc.auto_accept_files)
        tk.Checkbutton(opts, text=t("自动接收文件 (好友发来的文件不再询问)"),
                       variable=self.auto_file_var, bg=C["panel"], font=FONT_SMALL,
                       activebackground=C["panel"], command=self.toggle_file).pack(anchor="w")
        self.auto_conn_var = tk.BooleanVar(master=self.win, value=self.svc.auto_connect_friends)
        tk.Checkbutton(opts, text=t("自动连接好友 (对方上线后自动建立加密通道)"),
                       variable=self.auto_conn_var, bg=C["panel"], font=FONT_SMALL,
                       activebackground=C["panel"], command=self.toggle_conn).pack(anchor="w")

        tk.Label(frame, text=t("密钥轮换 (每收发多少条消息重新协商一次会话密钥)"), font=FONT_BOLD,
                 bg=C["panel"], fg=C["text"]).pack(anchor="w")
        rekey_row = tk.Frame(frame, bg=C["panel"])
        rekey_row.pack(fill="x", pady=(4, 4))
        self.rekey_var = tk.StringVar(master=self.win, value=str(self.svc.rekey_every))
        tk.Spinbox(rekey_row, from_=0, to=500, increment=5, width=6,
                   textvariable=self.rekey_var, font=FONT_SMALL).pack(side="left")
        tk.Label(rekey_row, text=t("条 (0 = 关闭; 轮换后旧密钥立即作废)"), font=FONT_SMALL,
                 bg=C["panel"], fg=C["muted"]).pack(side="left", padx=8)
        _btn(rekey_row, t("应用"), self.apply_rekey, "ghost").pack(side="left")
        tk.Label(frame, text=(t("和对方设置不同时这样协商: 任意一方填 0 就不自动轮换, "
                              "否则取更严格(更小)的那个; 改完会立即通知对方一起生效。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(0, 12))

        tk.Label(frame, text=t("黑名单 (封禁只是暂时拒收消息和请求, 不会删好友)"),
                 font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        self.blocked_list = tk.Listbox(frame, height=5, font=FONT_SMALL, relief="flat",
                                       bg=C["bg"], highlightthickness=0)
        self.blocked_list.pack(fill="both", expand=True, pady=(4, 8))
        _btn(frame, t("解除封禁 (恢复原来的关系, 对方会收到通知)"), self.unblock_selected,
             "ghost").pack(anchor="w")
        tk.Label(frame, text=(t("想彻底断开一个人请用『删除好友』(对方会收到提示); "
                              "封禁 + 解禁不会删掉好友关系。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(4, 8))

        tk.Label(frame, text=t("本机端口 (0 = 每次随机; 手动添加时对方要填这个端口)"),
                 font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        port_row = tk.Frame(frame, bg=C["panel"])
        port_row.pack(fill="x", pady=(4, 2))
        self.port_var = tk.StringVar(master=self.win, value=str(self.svc.saved_tcp_port or 0))
        self.port_entry = tk.Entry(port_row, textvariable=self.port_var, width=8, font=FONT_SMALL,
                                   relief="flat", bg=C["bg"])
        self.port_entry.pack(side="left", ipady=3)
        _btn(port_row, t("保存"), self.apply_tcp_port, "ghost").pack(side="left", padx=6)
        tk.Label(port_row, text=t("当前实际端口: {0}").format(self.svc.tcp_port), font=FONT_SMALL,
                 bg=C["panel"], fg=C["muted"]).pack(side="left", padx=6)
        self.discoverable_var = tk.BooleanVar(master=self.win, value=self.svc.discoverable)
        tk.Checkbutton(frame,
                       text=t("允许被自动搜索 (关掉后别人搜不到我: 不广播也不回应单播探测)"),
                       variable=self.discoverable_var, bg=C["panel"], font=FONT_SMALL,
                       activebackground=C["panel"],
                       command=self.toggle_discoverable).pack(anchor="w", pady=(2, 2))
        tk.Label(frame, text=(t("关掉之后, 别人只能『手动添加』填 `你的IP:上面那个端口` 来加你; "
                              "你自己仍然能看到别人、也能正常聊天。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(0, 8))
        tk.Label(frame, text=(t("广播能通的网络不用管端口 (端口会随广播告诉对方)。"
                              "手动模式: 只填对方 IP 就能自动探测出端口, 双方都不用固定端口。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(0, 12))

        tk.Label(frame, text=t("我的接收目录 (所有收到的文件都存这里)"), font=FONT_BOLD,
                 bg=C["panel"], fg=C["text"]).pack(anchor="w")
        dir_row = tk.Frame(frame, bg=C["panel"])
        dir_row.pack(fill="x", pady=(4, 12))
        self.dir_label = tk.Label(dir_row, text=self.svc.download_dir, font=FONT_SMALL,
                                 bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                                 wraplength=440)
        self.dir_label.pack(side="left", fill="x", expand=True)
        _btn(dir_row, t("改目录…"), self.choose_download_dir, "ghost").pack(side="left", padx=6)
        _btn(dir_row, t("用系统下载目录"), self.use_system_download_dir, "ghost").pack(side="left")
        _btn(dir_row, t("打开"), self.open_download_dir, "ghost").pack(side="left", padx=6)
        tk.Label(frame, text=(t("默认就是系统的『下载』目录; 这个设置会存进 settings.json, "
                              "下次启动继续沿用。想恢复默认点『用系统下载目录』。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left", anchor="w",
                 wraplength=460).pack(fill="x", pady=(0, 10))

        tk.Label(frame,
                 text=(t("""数据目录: {0}
身份密钥保存在 identity.json; 删除它会生成新身份 (等于换了个人)。""").format(self.svc.data_dir)),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], justify="left",
                 anchor="w").pack(fill="x", pady=(10, 8))

        # 鼠标滚轮: Tk 的滚轮事件不会自动冒泡到 Canvas, 得给每个子控件都绑上
        def _wheel(event: Any) -> str:
            step = -1 if getattr(event, "delta", 0) > 0 else 1
            self.body_canvas.yview_scroll(step * 3, "units")
            return "break"

        def _bind_wheel(widget: tk.Misc) -> None:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(seq, _wheel)
            for child in widget.winfo_children():
                _bind_wheel(child)

        _bind_wheel(frame)

        app.register_hook("contacts", self.refresh_blocked)
        self._blocked: List[Contact] = []
        self.refresh_blocked()

    def refresh_blocked(self) -> None:
        if not self.win.winfo_exists():
            self.app.unregister_hook("contacts", self.refresh_blocked)
            return
        self.blocked_list.delete(0, "end")
        self._blocked = self.svc.blocked_contacts()
        for contact in self._blocked:
            self.blocked_list.insert("end", f" {contact.name}   ({contact.fingerprint})")
        if not self._blocked:
            self.blocked_list.insert("end", t(" (没有封禁任何人)"))

    def save_name(self) -> None:
        try:
            self.svc.set_name(self.name_var.get())
        except ValueError as exc:
            messagebox.showwarning(t("昵称"), str(exc), parent=self.win)
            return
        try:
            self.app.master_window.title(t("局域网聊天 -- {0}").format(self.svc.name))
        except tk.TclError:
            pass

    def copy_fingerprint(self) -> None:
        self.win.clipboard_clear()
        self.win.clipboard_append(self.svc.fingerprint)
        messagebox.showinfo(t("指纹"), t("已复制到剪贴板"), parent=self.win)

    def apply_language(self) -> None:
        """保存界面语言。**重启后生效** (运行时重建整棵控件树风险太大, 见 service.set_language)。"""
        name = self.lang_var.get()
        code = next((item for item in self._lang_codes
                     if i18n.display_name(item) == name), None)
        if code is None:
            return
        self.svc.set_language(code)
        if code == i18n.current_language():
            messagebox.showinfo(t("界面语言"), t("已经是这个语言了, 不用重启。"), parent=self.win)
            return
        messagebox.showinfo(
            t("界面语言"),
            t("已保存: 界面语言改成 {0}。重启程序后生效。").format(i18n.display_name(code)),
            parent=self.win)

    def toggle_discoverable(self) -> None:
        """隐身开关: 关掉后不广播、不回应单播探测 (别人搜不到我)。"""
        value = bool(self.discoverable_var.get())
        self.svc.set_discoverable(value)
        if not value:
            messagebox.showinfo(
                t("隐身"),
                t("""已关掉『允许被自动搜索』:
• 别人在『局域网中的人』里看不到你;
• 别人用『手动添加』只填 IP 也探测不到你;
• 别人只能填 `你的IP:{0}` 直接加你(端口在设置里能看到, 也可以在下面固定一个, 免得重启后变了)。

你自己仍然能看到别人、也能正常聊天。""").format(self.svc.tcp_port),
                parent=self.win)

    def apply_tcp_port(self) -> None:
        """保存"本机固定端口" (手动模式用; 重启后生效)。"""
        try:
            value = int(self.port_var.get().strip() or "0")
        except ValueError:
            messagebox.showwarning(t("本机端口"), t("请填数字: 0 = 每次随机, 或 1024~65535"),
                                   parent=self.win)
            return
        if not self.svc.set_tcp_port(value):
            return
        messagebox.showinfo(
            t("本机端口"),
            (t("""已保存: 本机端口固定为 {0}
重启程序后生效。

手动添加时让对方填: 你的IP:{1}""").format(value, value)) if value else
            t("已保存: 本机端口改回自动分配 (每次启动随机)。重启程序后生效。"),
            parent=self.win)

    def choose_download_dir(self) -> None:
        """改接收目录: 所有收到的文件都存这里。"""
        path = filedialog.askdirectory(parent=self.win, title=t("选择接收目录"),
                                       initialdir=self.svc.download_dir or None)
        if not path:
            return
        if self.svc.set_download_dir(path):
            self.dir_label.configure(text=self.svc.download_dir, fg=C["text"])
        else:
            messagebox.showwarning(t("接收目录"), t("这个目录不能用 (没有写权限?), 换一个试试。"),
                                   parent=self.win)

    def use_system_download_dir(self) -> None:
        """一键回到默认: 系统的『下载』目录 (不用去删 .lanchat 里的 settings.json)。"""
        target = default_download_dir()
        if os.path.abspath(target) == os.path.abspath(self.svc.download_dir):
            messagebox.showinfo(t("接收目录"), t("""现在用的就是系统下载目录:
{0}""").format(target),
                                parent=self.win)
            return
        if self.svc.set_download_dir(target):
            self.dir_label.configure(text=self.svc.download_dir, fg=C["text"])
            messagebox.showinfo(t("接收目录"), t("""已改回系统下载目录:
{0}""").format(self.svc.download_dir),
                                parent=self.win)
        else:
            messagebox.showwarning(t("接收目录"), t("""系统下载目录不可用 (没有写权限?):
{0}
可以点『改目录…』自己挑一个。""").format(target), parent=self.win)

    def open_download_dir(self) -> None:
        self.app._reveal_path(os.path.join(self.svc.download_dir, "."))

    def toggle_file(self) -> None:
        self.svc.set_auto_accept_files(bool(self.auto_file_var.get()))

    def toggle_conn(self) -> None:
        self.svc.set_auto_connect_friends(bool(self.auto_conn_var.get()))

    def apply_rekey(self) -> None:
        try:
            value = max(0, int(self.rekey_var.get()))
        except (TypeError, ValueError):
            messagebox.showwarning(t("密钥轮换"), t("请填数字"), parent=self.win)
            return
        self.svc.set_rekey_every(value)
        messagebox.showinfo(t("密钥轮换"),
                            t("已设置为每 {0} 条消息轮换一次").format(value) if value else t("已关闭密钥轮换"),
                            parent=self.win)

    def unblock_selected(self) -> None:
        selection = self.blocked_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self._blocked):
            contact = self._blocked[index]
            was_friend = bool(contact.friend_before_block or contact.is_friend)
            name = contact.name
            if self.svc.unblock(contact.peer_id):
                messagebox.showinfo(
                    t("解除封禁"),
                    unblock_text(name, True, was_friend,
                                 self.svc.has_question_for(contact.peer_id)),
                    parent=self.win)
            self.refresh_blocked()


class SetQuestionDialog:
    """给某个人设置加好友验证问题。"""

    def __init__(self, app: "ChatApp", contact: Contact) -> None:
        self.app = app
        self.svc = app.service
        self.contact = contact
        self.win = tk.Toplevel(app)
        self.win.title(t("给 {0} 设置验证问题").format(contact.name))
        self.win.configure(bg=C["panel"])
        self.win.geometry("540x340")
        self.win.transient(app)

        frame = tk.Frame(self.win, bg=C["panel"], padx=18, pady=16)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=t("对方想加你为好友时, 必须先答对这道题"),
                 font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(frame, text=(t("答案只在本机参与哈希计算, 不会发送给对方; 每次提问都用新的随机数, "
                              "所以抓包也无法重放旧答案。")),
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], wraplength=470,
                 justify="left").pack(anchor="w", pady=(4, 10))

        tk.Label(frame, text=t("问题"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(anchor="w")
        self.question_var = tk.StringVar(master=self.win,
                                         value=app.service.question_of(contact.peer_id))
        tk.Entry(frame, textvariable=self.question_var, font=FONT, relief="flat",
                 bg=C["bg"]).pack(fill="x", ipady=5)
        tk.Label(frame, text=t("答案"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(anchor="w", pady=(10, 0))
        self.answer_var = tk.StringVar(master=self.win)
        tk.Entry(frame, textvariable=self.answer_var, font=FONT, relief="flat",
                 bg=C["bg"], show="●").pack(fill="x", ipady=5)
        row = tk.Frame(frame, bg=C["panel"])
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text=t("答错上限"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")
        self.attempts_var = tk.StringVar(
            master=self.win,
            value=str(app.service.max_attempts_for(contact.peer_id)
                      if app.service.has_question_for(contact.peer_id)
                      else DEFAULT_MAX_ANSWER_ATTEMPTS))
        tk.Spinbox(row, from_=1, to=10, width=4, textvariable=self.attempts_var,
                   font=FONT_SMALL).pack(side="left", padx=6)
        tk.Label(row, text=t("(到了就自动封禁)"), font=FONT_SMALL, bg=C["panel"],
                 fg=C["muted"]).pack(side="left")

        buttons = tk.Frame(frame, bg=C["panel"])
        buttons.pack(fill="x", pady=(16, 0))
        _btn(buttons, t("保存"), self.save, "primary", width=8).pack(side="right")
        _btn(buttons, t("取消设题"), self.clear, "ghost", width=10).pack(side="right", padx=8)
        _btn(buttons, t("关闭"), lambda: _close_toplevel(self.win), "ghost",
             width=6).pack(side="left")

    def save(self) -> None:
        question = self.question_var.get().strip()
        answer = self.answer_var.get().strip()
        if not question or not answer:
            missing = "、".join(part for part, value in
                                ((t("『问题』"), question), (t("『答案』", answer))) if not value)
            messagebox.showwarning(t("验证问题"), t("{0} 还没有填 (想取消设题请点『取消设题』)").format(missing),
                                   parent=self.win)
            return
        try:
            attempts = max(1, int(self.attempts_var.get()))
        except (TypeError, ValueError):
            attempts = DEFAULT_MAX_ANSWER_ATTEMPTS
        self.svc.set_question(self.contact.peer_id, question, answer, attempts)
        _close_toplevel(self.win)

    def clear(self) -> None:
        self.svc.clear_question(self.contact.peer_id)
        _close_toplevel(self.win)


# ===========================================================================
# 自测模式: 同一台机器上开两个"完整的正常窗口"
# ===========================================================================
SELFTEST_DISCOVERY_GRACE = 9.0     # 秒: 自测里等"对方真的出现在发现层"的最长时间


def make_self_test_service(who: str, index: int, base: str, shared_downloads: str,
                           args: argparse.Namespace) -> ChatService:
    """自测模式里的一个实例 (抽成函数是为了让测试能直接检查它的配置)。"""
    return ChatService(
        name=t("{0}(本机)").format(who),
        data_dir=os.path.join(base, who),
        download_dir=os.path.join(shared_downloads, who),
        # 两个实例**都**开发现层: 以前只让"甲"发广播、乙连收都不收, 结果
        # ① 自测里"局域网中的人"永远是空的 (全靠内部登记, 根本没走发现);
        # ② 新加的"隐身"开关在自测里看不出任何效果 (用户报过)。
        # 自己的 beacon 会被 peer_id 过滤掉, 同机不会"自己发现自己"。
        enable_discovery=True,
        discovery_port=args.discovery_port,
        tcp_port=0,
        # 自测模式**不要**"自动接收": 否则收到文件不弹窗, 用户会以为"没提示就存默认目录了"
        # (踩过这个坑: 想选保存位置却没弹窗)
        auto_accept_files=False,
        auto_connect_friends=True,
        reconnect_cooldown=0.5,
        rekey_every=args.rekey_after,
    )


def selftest_connect_pair(services: List[ChatService], grace: float = SELFTEST_DISCOVERY_GRACE,
                          poll: float = 0.25) -> List[Dict[str, Any]]:
    """让自测模式的两个实例互相看得见 —— **优先靠真实的广播发现**。

    为什么不能直接登记: 以前自测一启动就 `register_manual_peer()` 把对方塞进
    "局域网中的人", 于是 ① 列表里永远有对方, 看不出广播到底工作没有;
    ② 新加的"隐身"(别人搜不到我) 在自测里完全看不出效果 —— 对方隐身后, 本地登记
    那条记录还在, 列表里照样有人。用户报的就是这个。

    现在: 先等发现层**自己**学到对方 (两边都开发现层, 同机广播照常工作);
    等不到 (广播被防火墙拦了 / 只有 VPN 网卡) 才用本地登记兜底, 并如实说明
    "隐身测不了"。返回每人一条:
      {"peer_id","name","address","source","seen"}, source ∈ {"discovery","manual"}。
    """
    ready = [False] * len(services)
    deadline = time.time() + max(0.0, grace)
    while not all(ready):
        for index, service in enumerate(services):
            if not ready[index] and service.is_discovered(services[1 - index].peer_id):
                ready[index] = True
        if all(ready) or time.time() >= deadline:
            break
        time.sleep(poll)

    results: List[Dict[str, Any]] = []
    for index, service in enumerate(services):
        other = services[1 - index]
        source = "discovery" if ready[index] else "manual"
        if source == "manual":
            service.register_manual_peer(other.peer_id, other.name, "127.0.0.1", other.tcp_port)
        info = next((p for p in service.lan_peers() if p["peer_id"] == other.peer_id), None)
        results.append({
            "peer_id": other.peer_id,
            "name": other.name,
            "address": info["address"] if info else "",
            "source": source,
            "seen": info is not None,
            "manual_flag": bool(info and info.get("manual")),
        })
    return results


def selftest_peer_note(info: Dict[str, Any], other_name: str) -> str:
    """自测窗口里显示的"我是怎么看到对方的"说明 (顺便教怎么测隐身)。"""
    if info["source"] == "discovery":
        return (t("""🧪 我是靠**广播发现**看到「{0}」的 ({1})。
    想验证『隐身』: 在『⚙ 设置』里关掉『允许被自动搜索』, 然后打开对面窗口的『➕ 添加联系人』—— 大约 12 秒后(离线超时)我会从那个列表里消失, 重新打开又会出现。""").format(other_name, info['address']))
    return (t("""⚠ 这条网络里**广播不通**(或者被防火墙/安全软件拦了 UDP), 我已经用内部登记兜底看到「{0}」({1})。
    两个窗口照样能加好友 / 聊天 / 传文件, 但『隐身』在自测里看不出效果 —— 隐身要靠广播, 请在两台真机上验证。""").format(other_name, info['address']))


def launch_self_test(args: argparse.Namespace) -> None:
    """自测模式。

    启动**两个完整独立的实例**, 每个实例都有一个和正常使用完全一样的窗口
    (联系人列表 / 新朋友 / 设置 / 右键菜单 / 文件传输), 只是它们跑在同一台机器上。
    你可以在一个窗口点「添加联系人」发请求, 在另一个窗口点「同意」, 完整走一遍真实流程。
    """
    from . import startup_log

    base = os.path.join(args.data_dir or os.path.join(os.path.expanduser("~"), ".lanchat"),
                        "selftest")
    # 接收目录也走"系统下载目录"这一套 (不再塞进数据目录 .lanchat 里)
    shared_downloads = args.download_dir or os.path.join(
        os.path.expanduser("~"), "Downloads", "lanchat-自测")

    startup_log.step(t("自测模式: 准备两个完整实例 (数据目录 {0})").format(base))
    services: List[ChatService] = []
    apps: List["ChatApp"] = []
    windows: List[tk.Tk] = []
    closing = {"done": False}

    # 并排放置: 先按屏幕算出一半宽度, 保证两个窗口不会互相压住
    try:
        probe = tk.Tk()
        screen_w = probe.winfo_screenwidth()
        probe.destroy()
    except tk.TclError:
        screen_w = 1280
    half_w = max(480, min(900, (screen_w - 40) // 2))
    offset = half_w // 2 + 8

    for index, who in enumerate((t("甲"), t("乙"))):
        service = make_self_test_service(who, index, base, shared_downloads, args)
        services.append(service)

        window = tk.Tk()
        window.title(t("[自测 {0}] 局域网聊天 {1} — {2}").format(who, __version__, service.name))
        geometry = _shift_geometry(window, half_w, 620, -offset if index == 0 else offset, 0)
        window.geometry(geometry)
        window.minsize(640, 460)
        windows.append(window)

    # 服务先跑起来 (TCP 端口是在 start() 里才绑定的), 然后**靠真实的广播发现**对方。
    # 以前这里无条件 register_manual_peer(), 等于把对方硬塞进"局域网中的人":
    # 列表里永远有对方 -> 既看不出广播到底通不通, 也没法验证隐身 (对方隐身了,
    # 但本地登记还在, 列表里照样有人)。现在只在广播确实不通时才兜底 (见下)。
    from .discovery import warm_interfaces_async

    warm_interfaces_async()      # 网卡枚举先丢后台 (打包后要 2~3 秒, 别卡在两个窗口的启动上)
    for service in services:
        service.start()

    def close_all() -> None:
        if closing["done"]:
            return
        closing["done"] = True
        startup_log.step(t("自测模式: 关闭两个实例"))
        for svc in services:
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
        for win in windows:
            try:
                win.destroy()
            except tk.TclError:
                pass

    for index, (window, service) in enumerate(zip(windows, services)):
        app = ChatApp(window, service, args, own_service=False, on_close_hook=close_all)
        apps.append(app)
        app._append("sys", t("🧪 自测模式: 这台机器上同时跑着两个完整实例 (都是正常窗口)。"))
        app._append("sys", t("另一个窗口是「{0}」, 正在等广播发现它 (最多 {1:.0f} 秒, 广播不通就自动兜底); 找到后在那边的『➕ 添加联系人』里能看到我 —— 加好友 / 验证问题 / 聊天 / 传文件都按平时那样操作。").format(services[1 - index].name, SELFTEST_DISCOVERY_GRACE))

    deadline = {"at": time.time() + SELFTEST_DISCOVERY_GRACE}

    def resolve_pair() -> None:
        """等广播发现对方 —— 在 Tk 事件循环里轮询 (不另开线程, 免得跨线程操作窗口)。

        两个都发现 -> 立刻收工; 到点还没发现 -> 兜底登记, 并如实说明隐身测不了。
        """
        try:
            if not windows[0].winfo_exists():
                return
        except tk.TclError:
            return
        found = all(svc.is_discovered(services[1 - i].peer_id)
                    for i, svc in enumerate(services))
        if not found and time.time() < deadline["at"]:
            windows[0].after(300, resolve_pair)
            return
        for index, info in enumerate(selftest_connect_pair(services, grace=0.0)):
            other = services[1 - index]
            where = t("广播发现") if info["source"] == "discovery" else t("内部登记兜底")
            startup_log.step(t("自测模式: {0} 看到的 {1} = {2} (来源: {3})").format(services[index].name, other.name, info['address'] or t('⚠ 没有登记上'), where))
            try:
                apps[index]._append("sys", selftest_peer_note(info, other.name))
            except tk.TclError:
                pass

    windows[0].after(300, resolve_pair)

    # 先把"窗口已显示"写进日志, 再去抢前台
    # 外部脚本 (含启动回归测试) 看到窗口后可能马上读日志, 顺序反了就抓不到这行。
    startup_log.step(t("自测模式: 两个窗口已显示, 进入事件循环"))
    for window in windows:
        _bring_to_front(window)
    windows[0].mainloop()
    close_all()


# ===========================================================================
def _bring_to_front(window: tk.Misc) -> None:
    """把窗口带到最前面并抢焦点。

    从 IDE / 批处理 / 自动化环境启动时, Windows 会拦截"抢焦点"请求, 窗口会停在
    别的窗口后面, 用户看到的就是"程序运行了但没有窗口"。这里用 Win32 强制置前
    (见 lanchat/winfocus.py), 并短暂置顶 + 闪任务栏。
    """
    from .winfocus import bring_to_front

    try:
        bring_to_front(window)
    except Exception:  # noqa: BLE001 - 提不到前台也不能影响程序
        pass


def _fit(window: tk.Misc, width: int, height: int) -> tuple:
    """按屏幕大小收窄窗口尺寸, 保证不会超出屏幕。"""
    try:
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
    except tk.TclError:
        return width, height, 0, 0
    if screen_w <= 100 or screen_h <= 100:
        return width, height, 0, 0
    width = min(width, max(640, screen_w - 80))
    height = min(height, max(480, screen_h - 120))
    return width, height, screen_w, screen_h


def _center_geometry(window: tk.Misc, width: int, height: int) -> str:
    """算出居中并保证在屏幕内的几何尺寸 (多显示器/小屏也不会跑到看不见的地方)。"""
    width, height, screen_w, screen_h = _fit(window, width, height)
    if not screen_w:
        return f"{width}x{height}"
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    return f"{width}x{height}+{x}+{y}"


def _shift_geometry(window: tk.Misc, width: int, height: int, dx: int, dy: int) -> str:
    """在居中位置基础上左右偏移, 方便自测模式并排放两个窗口。"""
    width, height, screen_w, screen_h = _fit(window, width, height)
    if not screen_w:
        return f"{width}x{height}"
    x = max(0, min(screen_w - width, (screen_w - width) // 2 + dx))
    y = max(0, (screen_h - height) // 4 + dy)
    return f"{width}x{height}+{x}+{y}"


def _apply_cli_language(argv: Optional[List[str]] = None) -> str:
    """从命令行/设置/系统语言定下界面语言 (幂等)。

    入口 chat_gui.py 在建界面之前已经设过一次 (它必须先设, 否则连"缺少 tkinter"这类
    启动失败提示都是中文); 这里再算一次是为了 `python lanchat/gui.py` 和测试直接调用
    `gui.main()` 的场合。优先级: `--lang` > settings.json 里存过的 > 系统语言。
    """
    items = [str(item) for item in (argv if argv is not None else [])]
    lang, data_dir = "", ""
    for index, item in enumerate(items):
        if item == "--lang" and index + 1 < len(items):
            lang = items[index + 1]
        elif item.startswith("--lang="):
            lang = item.split("=", 1)[1]
        elif item == "--data-dir" and index + 1 < len(items):
            data_dir = items[index + 1]
        elif item.startswith("--data-dir="):
            data_dir = item.split("=", 1)[1]
    from .identity import resolve_app_dir

    target = (data_dir or "").strip() or resolve_app_dir()
    return i18n.set_language(i18n.resolve_initial_language(lang, target))


def prepare(argv: Optional[List[str]] = None):
    """建窗口 + 建服务 + 完成首次昵称设置。

    返回 (root, service, args, cancelled)。拆成函数是为了让入口脚本能分步记录日志,
    卡在哪一步一目了然。
    """
    parser = argparse.ArgumentParser(prog="chat_gui.py",
                                     description=t("局域网聊天工具 (微信风格界面)"))
    parser.add_argument("--name", "-n", default="", help=t("昵称 (不填则弹窗询问)"))
    parser.add_argument("--port", type=int, default=DEFAULT_TCP_PORT, help=t("本机 TCP 端口, 0=自动"))
    parser.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT,
                        help=t("UDP 自动发现端口 (默认 {0})").format(DEFAULT_DISCOVERY_PORT))
    parser.add_argument("--download-dir", default="", help=t("接收文件的保存目录"))
    parser.add_argument("--data-dir", default="", help=t("身份/好友数据的保存目录"))
    parser.add_argument("--lang", default="", metavar=t("语言代码"),
                        help=t("界面语言: {0} (默认跟随系统语言, 也可以在设置里改)").format(
                            " / ".join(i18n.available())))
    parser.add_argument("--auto-accept", action="store_true", help=t("自动接收文件"))
    parser.add_argument("--no-auto-connect", action="store_true", help=t("不自动连接好友"))
    parser.add_argument("--no-focus", action="store_true",
                        help=t("不要把窗口强制提到最前 (某些窗口管理器不喜欢)"))
    parser.add_argument("--rekey-after", type=int, default=30, metavar=t("条数"),
                        help=t("每收发多少条消息重新协商一次会话密钥 (0=关闭, 默认 30)"))
    parser.add_argument("--window-test", type=int, default=0, metavar=t("秒"),
                        help=t("只弹一个测试窗口, 显示指定秒数后自动关闭 (用于确认窗口能不能显示)"))
    parser.add_argument("--version", action="version", version=f"lanchat {__version__}")
    args = parser.parse_args(argv)

    from . import startup_log

    if args.window_test:
        startup_log.step(t("窗口测试: 打开一个标题为『窗口测试』的窗口, {0} 秒后自动关闭…").format(args.window_test))
        window = tk.Tk()
        window.title(t("窗口测试 —— 看到这个窗口就说明图形界面正常"))
        window.geometry(_center_geometry(window, 520, 260))
        tk.Label(window, text=t("如果你能看到这个窗口,\n说明图形界面本身没有问题。"),
                 font=(FONT[0], 13), bg=C["panel"], fg=C["text"], justify="center").pack(
            expand=True, fill="both")
        tk.Label(window, text=t("{0} 秒后自动关闭").format(args.window_test), font=FONT_SMALL,
                 bg=C["panel"], fg=C["muted"]).pack(pady=(0, 12))
        _bring_to_front(window)
        startup_log.step(t("测试窗口已显示: viewable={0} geometry={1}").format(window.winfo_viewable(), window.winfo_geometry()))
        window.after(int(args.window_test * 1000), window.destroy)
        window.mainloop()
        startup_log.step(t("测试窗口已关闭 —— 图形界面工作正常"))
        return window, None, args, True  # type: ignore[return-value]

    startup_log.step(t("创建 Tk 主窗口…"))
    root = tk.Tk()
    root.title(t("局域网聊天 {0}").format(__version__))
    root.geometry(_center_geometry(root, 1000, 660))
    root.minsize(820, 520)
    root.update_idletasks()
    root.deiconify()
    _bring_to_front(root)
    startup_log.step(t("Tk 主窗口已创建 (Tk {0}, 屏幕 {1}x{2}, viewable={3})").format(root.tk.call('info', 'patchlevel'), root.winfo_screenwidth(), root.winfo_screenheight(), root.winfo_viewable()))

    startup_log.step(t("初始化聊天服务 (身份密钥 / 数据目录)…"))
    # 网卡枚举 (Windows 上是一次 ipconfig 子进程, 打包后实测 2.5 秒) 先在后台跑起来:
    # 用户慢慢填昵称的这几秒足够它跑完, 之后建发现层/显示本机地址就一点都不卡了。
    from .discovery import warm_interfaces_async

    warm_interfaces_async()
    service = ChatService(
        name=args.name,
        discovery_port=args.discovery_port,
        tcp_port=args.port,
        download_dir=args.download_dir or None,
        auto_accept_files=args.auto_accept,
        auto_connect_friends=not args.no_auto_connect,
        data_dir=args.data_dir or None,
        rekey_every=args.rekey_after,
    )
    startup_log.step(t("服务就绪: 昵称 {0} | 数据目录 {1}").format(service.name, service.data_dir))

    # 第一次打开 (或还没设置过昵称): 在主窗口里先设置昵称
    need_setup = (not args.name.strip()) or service.name == t("未命名用户")
    if need_setup:
        startup_log.step(t("第一次使用: 在主窗口里显示『设置昵称』(填完点开始使用)…"))
        finished = tk.BooleanVar(master=root, value=False)
        setup = NicknameDialog(root, service, on_done=lambda _ok: finished.set(True))
        root.wait_variable(finished)     # 等用户在界面上操作, 事件循环照常跑
        if not setup.ok:
            startup_log.step(t("用户取消了昵称设置, 退出"))
            service.stop()
            root.destroy()
            return root, service, args, True
        setup.close()
        startup_log.step(t("昵称设置完成: {0}").format(service.name))

    startup_log.step(t("构建主界面…"))
    root.title(t("局域网聊天 {0} — {1}").format(__version__, service.name))
    root.geometry(_center_geometry(root, 1000, 660))
    ChatApp(root, service, args, own_service=True)
    if not args.no_focus:
        _bring_to_front(root)
    startup_log.step(t("主界面已显示 (geometry {0}, viewable={1}), 进入事件循环").format(root.winfo_geometry(), root.winfo_viewable()))
    print(t("窗口应该已经出现: 标题『局域网聊天 {0} — {1}』").format(__version__, service.name))
    print(t("如果看不到窗口, 检查任务栏 (可能被别的窗口挡住), 或换普通终端重跑。"))
    return root, service, args, False


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _apply_cli_language(args)        # 必须在建任何界面/写任何提示之前
    if "--self-test" in args:
        parser = argparse.ArgumentParser(prog="chat_gui.py --self-test")
        parser.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT)
        parser.add_argument("--data-dir", default="")
        parser.add_argument("--download-dir", default="")
        parser.add_argument("--rekey-after", type=int, default=30)
        parser.add_argument("--no-discovery", action="store_true")
        parser.add_argument("--no-focus", action="store_true")
        parser.add_argument("--name", "-n", default="")
        parser.add_argument("--port", type=int, default=0)
        parser.add_argument("--lang", default="")
        parser.add_argument("--version", action="version", version=f"lanchat {__version__}")
        ns, _unknown = parser.parse_known_args(args)
        ns.self_test = True          # 界面据此显示"🧪 掉线 6 秒"这类自测按钮
        try:
            launch_self_test(ns)
        except KeyboardInterrupt:
            pass
        return 0

    root, service, args, cancelled = prepare(argv)
    if cancelled or service is None:
        return 0
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - 运行中出问题也要留下痕迹
        import traceback

        detail = traceback.format_exc()
        try:
            print(detail, flush=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(os.path.join(service.data_dir, "last-run.log"), "a",
                      encoding="utf-8") as fh:
                fh.write(detail + "\n")
        except OSError:
            pass
        try:
            messagebox.showerror(t("程序出错"), t("""发生异常, 详情见:
{0}

{1}""").format(os.path.join(service.data_dir, 'last-run.log'), detail.splitlines()[-1]))
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
