"""GUI 冒烟测试: 在真实窗口里把微信式界面搭起来并驱动一遍。

检查点:
  * 第一次打开时弹出"设置昵称"对话框, 设置后主界面才出现
  * 左侧联系人列表 / 右侧聊天区 / 顶栏按钮都在, 并显示自己的指纹
  * 收到好友请求 -> 出现在"新朋友"对话框里, 点"同意"后成为好友
  * 收到消息 -> 聊天区显示; 未建立加密连接时发送会给出提示
  * 右键菜单、设置窗口(含黑名单解禁)、添加联系人对话框都能正常打开
  * 关闭窗口能干净退出

需要图形环境; 没有图形环境时打印 SKIP 并返回 0。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import tkinter as tk
import uuid
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat.console import configure_console  # noqa: E402

configure_console()  # Windows 中文控制台默认 GBK, 打印 ✅/❌ 会直接崩

from lanchat import gui as G  # noqa: E402
from lanchat.service import ChatService, ContactState, EventKind, ServiceEvent  # noqa: E402

FAILURES: list[str] = []
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-gui")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def pump(root: tk.Tk, times: int = 3, delay: float = 0.08) -> None:
    for _ in range(times):
        root.update()
        time.sleep(delay)
    root.update()


def all_toplevels(widget: tk.Misc) -> list:
    found = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Toplevel):
            found.append(child)
        found.extend(all_toplevels(child))
    return found


def _button_labels(widget: tk.Misc) -> list:
    """递归收集窗口里所有按钮的文字 (检查某个按钮在不在)。"""
    labels: list = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Button):
            try:
                labels.append(str(child.cget("text")))
            except tk.TclError:
                pass
        labels.extend(_button_labels(child))
    return labels


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-port", type=int, default=50791)
    args = parser.parse_args()

    print("=" * 60)
    print("GUI 冒烟测试 (微信式界面)")
    print("=" * 60)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"  [SKIP] 当前环境没有图形界面: {exc}")
        return 0
    root.withdraw()

    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)

    answers: list = []
    messagebox.askyesno = lambda *a, **k: (answers.append("yesno"), True)[1]  # type: ignore
    messagebox.askokcancel = lambda *a, **k: (answers.append("okcancel"), True)[1]  # type: ignore
    messagebox.showinfo = lambda *a, **k: answers.append("info")  # type: ignore
    messagebox.showwarning = lambda *a, **k: answers.append("warning")  # type: ignore
    messagebox.showerror = lambda *a, **k: answers.append("error")  # type: ignore

    ns = argparse.Namespace(name="", port=0, discovery_port=args.discovery_port,
                            download_dir="", data_dir="", auto_accept=False,
                            no_auto_connect=False)
    service = ChatService(
        name="", data_dir=os.path.join(TMP, "data"), download_dir=os.path.join(TMP, "dl"),
        enable_discovery=False, tcp_port=0,
    )
    app = None

    # ---- 1. 第一次打开: 先设置昵称 (直接画在主窗口里, 必须是可见的) ----
    try:
        setup = G.NicknameDialog(root, service)
        root.update()
        check("第一次打开显示了『设置昵称』界面 (不得是隐藏窗口)",
              service.name == "未命名用户" and bool(setup.frame.winfo_exists()))
        check("设置昵称的主窗口是可见的 (viewable=1)",
              bool(root.winfo_viewable()), f"viewable={root.winfo_viewable()} "
                                           f"state={root.state()}")
        check("主窗口 geometry 合理 (不是 1x1)",
              "x" in root.winfo_geometry() and root.winfo_width() > 100,
              root.winfo_geometry())
        labels = [w.cget("text") for w in setup.frame.winfo_children()
                  if isinstance(w, tk.Label)] + \
                 [w.cget("text") for w in setup.frame.winfo_children()
                  for w in w.winfo_children() if isinstance(w, tk.Label)]
        check("界面上显示本机身份指纹", any(service.fingerprint == t for t in labels),
              str(labels)[:120])

        # 空昵称不允许开始
        answers.clear()
        setup._confirm()
        root.update()
        check("空昵称被拦下", "warning" in answers and not setup.ok)

        setup.name_var.set("小明")
        setup._confirm()
        root.update()
        check("填了昵称后设置完成", setup.ok is True)
        check("昵称已生效", service.name == "小明", service.name)
        setup.close()
        root.update()
        check("开始使用后启动界面被清理 (给主界面腾地方)",
              not setup.frame.winfo_exists())

        # ---- 2. 主界面 ----
        app = G.ChatApp(root, service, ns)
        pump(root, 4)
        check("主界面构建成功", app.winfo_exists() == 1)
        check("顶栏显示我的昵称和指纹",
              "小明" in app.me_label.cget("text") and service.fingerprint in app.fp_label.cget("text"),
              f"{app.me_label.cget('text')} / {app.fp_label.cget('text')}")
        check("聊天区显示了欢迎信息", "欢迎使用" in app.chat.get("1.0", "end"))
        check("服务已启动并分配了 TCP 端口", app.service.tcp_port > 0)
        check("左侧列表初始为空 (还没有好友)", app.contact_list.size() == 0)
        check("『新朋友』按钮存在", "新朋友" in app.requests_btn.cget("text"))
        check("欢迎信息里给出了可核对的指纹", service.fingerprint in app.chat.get("1.0", "end"))

        # ---- 3. 收到好友请求 ----
        peer_id = "lc-" + uuid.uuid4().hex[:32]
        app.events.put(ServiceEvent(kind=EventKind.CONTACTS))
        app.service._contacts[peer_id] = __import__(
            "lanchat.service", fromlist=["Contact"]).Contact(
            peer_id=peer_id, name="小红", state=ContactState.REQUEST_IN.value,
            message="我是小红", card=None)
        app.events.put(ServiceEvent(kind=EventKind.CONTACTS))
        pump(root, 3)
        check("顶栏提示有待处理请求", "1" in app.requests_btn.cget("text"),
              app.requests_btn.cget("text"))

        requests_dialog = G.FriendRequestsDialog(app)
        pump(root, 2)
        rows = [requests_dialog.tree.item(i, "values")
                for i in requests_dialog.tree.get_children()]
        check("新朋友对话框列出请求", len(rows) == 1 and rows[0][0] == "小红", str(rows))
        requests_dialog.tree.selection_set(requests_dialog.tree.get_children()[0])
        requests_dialog.accept()
        pump(root, 3)
        contact = app.service.get_contact(peer_id)
        check("点『同意』后成为好友",
              contact is not None and contact.state == ContactState.FRIEND.value,
              contact.state if contact else "无")
        check("左侧列表出现了这位好友", app.contact_list.size() == 1,
              str(app.contact_list.get(0, "end")))
        check("聊天窗口切到了该好友", app._current == peer_id)
        # 对话开着就告诉服务"现在开着谁": 这样新消息不会再攒未读 (用户报过这个 bug)
        check("打开对话时把『当前会话』告诉了服务",
              app.service._active_peer == peer_id, str(app.service._active_peer))

        # ---- 4. 收到消息 / 未连通时发送 ----
        app.events.put(ServiceEvent(kind=EventKind.MESSAGE, text="你好呀", peer_id=peer_id,
                                    name="小红", data={"direction": "in", "text": "你好呀"}))
        pump(root, 2)
        check("聊天区显示收到的消息", "小红: 你好呀" in app.chat.get("1.0", "end"))

        answers.clear()
        app.entry.delete("1.0", "end")
        app.entry.insert("1.0", "在吗")
        app.send_message()
        pump(root, 2)
        # 行为变了 (以前这里弹窗直接拦住): 现在**照样发** —— 消息先记在本机,
        # 对方一上线连上来就自动补发 (这就是"关后即焚"的会话记录)。
        check("对方没连上时也能发, 消息进了本机记录",
              any(e["text"] == "在吗" for e in app.service.conversation(peer_id)),
              str(app.service.conversation(peer_id)))
        check("聊天区说明了『先存着, 上线补发』",
              "先存在本机" in app.chat.get("1.0", "end"),
              app.chat.get("1.0", "end")[-160:])

        # ---- 5. 文件请求 (弹窗里可以选保存位置) ----
        answers.clear()
        app.events.put(ServiceEvent(
            kind=EventKind.FILE_OFFER, text="", peer_id=peer_id, name="小红",
            data={"transfer_id": "t1", "name": "a.txt", "size": 1024, "direction": "in"}))
        pump(root, 4)
        dialog = app._incoming_file_dialogs.get("t1")
        check("收到文件时弹出可选择的弹窗", dialog is not None and dialog.win.winfo_exists() == 1,
              str(list(app._incoming_file_dialogs)))
        if dialog is not None:
            check("弹窗指向我的接收目录",
                  dialog.default_dir == os.path.abspath(app.service.download_dir),
                  dialog.default_dir)
            labels = _button_labels(dialog.win)
            check("按钮: 接收(接收目录) / 另存到别处 / 拒绝",
                  any("接收目录" in t for t in labels) and any("另存到别处" in t for t in labels)
                  and any("拒绝" in t for t in labels), str(labels))
            dialog.reject()                       # 测试里不真的收文件
            pump(root, 1)
        check("聊天区提示收到文件", "发来文件" in app.chat.get("1.0", "end"))

        # ---- 6. 添加联系人 / 设置 / 右键菜单 ----
        contacts_dialog = G.ContactsDialog(app)
        pump(root, 2)
        check("添加联系人对话框能打开", contacts_dialog.win.winfo_exists() == 1)
        contacts_dialog.refresh()
        check("没有搜索到人时给出排查提示",
              "防火墙" in contacts_dialog.hint.cget("text"), contacts_dialog.hint.cget("text")[:40])

        settings = G.SettingsDialog(app)
        pump(root, 2)
        check("设置窗口能打开", settings.win.winfo_exists() == 1)
        settings.name_var.set("小明2")
        settings.save_name()
        pump(root, 2)
        check("设置里改名生效", app.service.name == "小明2", app.service.name)

        # 本机端口 (手动模式要用固定端口; 不用碰命令行)
        check("设置里有『本机端口』输入框",
              hasattr(settings, "port_var") and settings.port_var.get() == "0",
              settings.port_var.get() if hasattr(settings, "port_var") else "没有")
        answers.clear()
        settings.port_var.set("abc")
        settings.apply_tcp_port()
        pump(root, 1)
        check("端口填了非数字时明确提示", "warning" in answers, str(answers))
        answers.clear()
        settings.port_var.set("50505")           # 和 UDP 发现端口撞了
        settings.apply_tcp_port()
        pump(root, 1)
        check("端口和发现端口冲突时被拦下并提示",
              "info" not in answers and app.service.saved_tcp_port == 0,
              f"{answers} / saved={app.service.saved_tcp_port}")
        answers.clear()
        settings.port_var.set("53000")
        settings.apply_tcp_port()
        pump(root, 1)
        check("端口能保存下来 (重启后生效)", app.service.saved_tcp_port == 53000,
              str(app.service.saved_tcp_port))
        check("保存后有明确提示", "info" in answers, str(answers))
        settings.port_var.set("0")
        settings.apply_tcp_port()                 # 还原, 免得影响后面的用例
        pump(root, 1)

        # 隐身开关 (不允许被自动搜索 / 不回应单播探测)
        check("设置里有『允许被自动搜索』开关",
              hasattr(settings, "discoverable_var") and settings.discoverable_var.get() is True,
              str(getattr(settings, "discoverable_var", None) and settings.discoverable_var.get()))
        answers.clear()
        settings.discoverable_var.set(False)
        settings.toggle_discoverable()
        pump(root, 1)
        check("关掉开关后服务端进入隐身", app.service.discoverable is False)
        check("关掉时提示里说明了别人该怎么加你",
              "info" in answers, str(answers))
        settings.discoverable_var.set(True)
        settings.toggle_discoverable()
        pump(root, 1)
        check("能再打开", app.service.discoverable is True)

        # 设置项越加越多: 内容不能被窗口切掉 (踩过: 新增『本机端口』后整个下半部分
        # 连同"接收目录/关闭"都看不见了, 用户根本点不到)
        pump(root, 2)
        win_bottom = settings.win.winfo_rooty() + settings.win.winfo_height()
        check("『关闭』按钮一直留在窗口里 (不会被内容挤出去)",
              bool(getattr(settings, "close_btn", None))
              and settings.close_btn.winfo_rooty() + settings.close_btn.winfo_height()
              <= win_bottom + 2,
              f"按钮底 {settings.close_btn.winfo_rooty() + settings.close_btn.winfo_height()} "
              f"vs 窗口底 {win_bottom}")
        settings.body_canvas.yview_moveto(1.0)    # 滚到底
        pump(root, 2)
        check("滚到底能看到『本机端口』输入框",
              settings.port_entry.winfo_rooty() + settings.port_entry.winfo_height()
              <= win_bottom + 2,
              f"输入框底 {settings.port_entry.winfo_rooty() + settings.port_entry.winfo_height()} "
              f"vs 窗口底 {win_bottom}")
        check("设置内容确实放在可滚动区域里 (以后再加项也不会被切)",
              hasattr(settings, "body_canvas")
              and settings.body_canvas.bbox("all") is not None,
              str(settings.body_canvas.bbox("all")))

        app.service.block(peer_id)
        pump(root, 2)
        settings.refresh_blocked()
        check("黑名单里出现被封禁的人", settings.blocked_list.size() == 1,
              str(settings.blocked_list.get(0, "end")))
        settings.blocked_list.selection_set(0)
        settings.unblock_selected()
        pump(root, 2)
        check("点解除封禁后黑名单清空", not app.service.blocked_contacts())
        # 封禁是"暂时拒收", 不是删好友: 解禁后他还是好友, 而且绝不该凭空变成
        # 一条没答过题的待处理请求 (旧代码就是这样, 用户报过)
        _contact = app.service.get_contact(peer_id)
        check("解禁后仍然是好友 (封禁不等于删好友)",
              _contact is not None and _contact.state == ContactState.FRIEND.value,
              str([(c.name, c.state) for c in app.service.contacts()]))
        check("解禁后『新朋友』里不会凭空多出一条请求",
              not any(c.peer_id == peer_id for c in app.service.pending_requests()),
              str([(c.name, c.state) for c in app.service.pending_requests()]))

        # ---- 6.5 手动添加 (广播不通的组网: Tailscale / WireGuard / 跨网段) ----
        contacts_dialog.refresh()
        pump(root, 1)
        check("『添加联系人』里有手动填地址的输入框",
              hasattr(contacts_dialog, "manual_var")
              and any("添加并发起加好友请求" in t for t in _button_labels(contacts_dialog.win)),
              str(_button_labels(contacts_dialog.win)))
        answers.clear()
        contacts_dialog.manual_var.set("")
        contacts_dialog.add_manual()
        pump(root, 1)
        check("地址空着时给提示, 不报错", "info" in answers, str(answers))
        answers.clear()
        contacts_dialog.manual_var.set("192.168.1.5")          # 少了端口
        contacts_dialog.add_manual()
        pump(root, 1)
        check("只填 IP 不填端口时明确提示", "warning" in answers, str(answers))
        answers.clear()
        contacts_dialog.manual_var.set("192.168.1.5:abc")      # 端口不是数字
        contacts_dialog.add_manual()
        pump(root, 1)
        check("端口不是数字时明确提示", "warning" in answers, str(answers))

        # 只填 IP -> 走"单播探测"这条路 (不再报格式错)。
        # 真去探一个不存在的地址要等好几秒, 所以把服务端探测换成桩, 只验证界面把参数
        # 正确传下去 (探测本身在联调第 10 组里真跑)。
        answers.clear()
        probed: list = []
        real_add = app.service.add_manual_peer

        def fake_add(host, port=0, name="", probe_port=0):
            probed.append((host, port))
            return None

        app.service.add_manual_peer = fake_add          # type: ignore[assignment]
        try:
            contacts_dialog.manual_var.set("100.64.0.7")
            contacts_dialog.add_manual()
            pump(root, 1)
            check("只填 IP 时按『自动探测端口』处理 (port=0)",
                  probed == [("100.64.0.7", 0)], str(probed))
            check("探测失败时给出可操作的提示", "warning" in answers, str(answers))
            probed.clear()
            contacts_dialog.manual_var.set("100.64.0.7:50606")
            contacts_dialog.add_manual()
            pump(root, 1)
            check("填了 IP:端口 时按『直接连』处理",
                  probed == [("100.64.0.7", 50606)], str(probed))
            # 中文输入法打出来的 "：" (U+FF1A) 和全角数字: 必须自动纠正, 不能让用户
            # 对着"端口不是数字"自己找哪里错了
            probed.clear()
            contacts_dialog.manual_var.set("100.64.0.7：５０６０６")
            contacts_dialog.add_manual()
            pump(root, 1)
            check("中文冒号 + 全角数字自动纠正成 IP:端口",
                  probed == [("100.64.0.7", 50606)], str(probed))
            check("界面上说明了做过自动整理",
                  "已自动整理地址" in app.chat.get("1.0", "end"),
                  app.chat.get("1.0", "end")[-80:])
        finally:
            app.service.add_manual_peer = real_add      # type: ignore[assignment]
        contacts_dialog.manual_var.set("")

        answers.clear()
        contacts_dialog.manual_var.set("127.0.0.1:65000")
        contacts_dialog.add_manual()
        pump(root, 2)
        manual_contacts = [c for c in app.service.contacts() if c.peer_id.startswith("manual:")]
        check("填对格式后立刻建好联系人并发起请求",
              bool(manual_contacts) and manual_contacts[0].state == ContactState.REQUEST_OUT.value,
              str([(c.name, c.peer_id, c.state) for c in app.service.contacts()]))
        check("手动地址存进了联系人 (重启后还能重试)",
              bool(manual_contacts) and manual_contacts[0].manual_address == "127.0.0.1:65000",
              manual_contacts[0].manual_address if manual_contacts else "无")
        check("列表里能看到这个手动添加的人",
              any("手动添加" in str(contacts_dialog.tree.item(i, "values"))
                  for i in contacts_dialog.tree.get_children()),
              str([contacts_dialog.tree.item(i, "values")
                   for i in contacts_dialog.tree.get_children()]))
        check("输入框清空了 (方便连着加下一个人)",
              contacts_dialog.manual_var.get() == "", contacts_dialog.manual_var.get())
        if manual_contacts:                     # 收拾干净, 别影响后面的断言
            app.service.cancel_friend_request(manual_contacts[0].peer_id)
            pump(root, 1)

        for dialog in all_toplevels(root):
            try:
                G._close_toplevel(dialog)      # 先取消延后任务, 避免 Tcl 噪音
            except tk.TclError:
                pass
        pump(root, 1)
        app._popup_menu(type("E", (), {"y": 10, "x_root": 10, "y_root": 10})())
        pump(root, 1)
        check("右键菜单可以弹出", app.list_menu.winfo_exists() == 1)
        app.list_menu.unpost()

        # ---- 7. 与真实实例建立加密连接 ----
        other = ChatService(on_event=lambda e: None, name="影子",
                            data_dir=os.path.join(TMP, "data2"),
                            download_dir=os.path.join(TMP, "dl2"),
                            enable_discovery=False, tcp_port=0)
        other.start()
        app.service.register_manual_peer(other.peer_id, other.name, "127.0.0.1", other.tcp_port)
        # 本地登记的地址必须和"搜到的人"区分开: 否则对方隐身了列表里还照样有人,
        # 用户会以为隐身没生效 (报过这个问题)。
        manual_dialog = G.ContactsDialog(app)
        pump(root, 1)
        manual_dialog.refresh()
        rows = [str(manual_dialog.tree.item(i, "values"))
                for i in manual_dialog.tree.get_children()]
        check("本地登记的地址被标成『手动登记』(不是搜到的人)",
              any("手动登记" in row for row in rows), str(rows))
        G._close_toplevel(manual_dialog.win)
        pump(root, 1)
        app.service.send_friend_request(other.peer_id)
        # 对方(影子)收到请求并同意, 这才符合真实流程
        accepted = False
        deadline = time.time() + 20
        while time.time() < deadline and not accepted:
            pump(root, 2)
            if other.pending_requests():
                other.accept_request(app.service.peer_id)
                accepted = True
        check("对方收到并同意了请求", accepted,
              str([(c.name, c.state) for c in other.contacts()]))
        connected = False
        deadline = time.time() + 25
        while time.time() < deadline and not connected:
            pump(root, 2)
            contact = app.service.get_contact(other.peer_id)
            connected = bool(contact and contact.encrypted)
        check("界面上完成了加密连接", connected,
              str([(c.name, c.encrypted, c.state) for c in app.service.contacts()]))
        if connected:
            app.select_contact(other.peer_id)
            pump(root, 2)
            check("聊天窗口标题是对方昵称", app.chat_title.cget("text") == "影子",
                  app.chat_title.cget("text"))
            check("头部显示已加密连接", "加密" in app.chat_state.cget("text"),
                  app.chat_state.cget("text"))
            app.entry.delete("1.0", "end")
            app.entry.insert("1.0", "界面发出的消息")
            app.send_message()
            pump(root, 3)
            check("界面发送的消息出现在聊天区",
                  "我: 界面发出的消息" in app.chat.get("1.0", "end"))
            # 聊天区内容现在是**按人记在内存里**的: 切走再切回来不该变空
            # (以前 select_contact 会把聊天区清空, 关掉程序更是全没)
            app.select_contact("")
            pump(root, 1)
            check("切到别处后聊天区清空 (那是另一个人的会话)",
                  "界面发出的消息" not in app.chat.get("1.0", "end"))
            check("切走以后新消息要重新算未读",
                  app.service._active_peer == "", str(app.service._active_peer))
            app.select_contact(other.peer_id)
            pump(root, 1)
            check("切回来还能看到刚才的对话 (本会话内记着)",
                  "我: 界面发出的消息" in app.chat.get("1.0", "end"),
                  app.chat.get("1.0", "end")[-120:])
            check("服务端也记着这段对话 (对方重启后靠它补发)",
                  any(e["text"] == "界面发出的消息"
                      for e in app.service.conversation(other.peer_id)),
                  str(app.service.conversation(other.peer_id)))
        other.stop()

        # ---- 8. 关闭 ----
        app.on_close()
        root.update()
        check("关闭窗口不报错", True)
    finally:
        try:
            service.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            for dialog in all_toplevels(root):
                G._close_toplevel(dialog)
            G._close_toplevel(root)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
        return 1
    print("✅ GUI 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
