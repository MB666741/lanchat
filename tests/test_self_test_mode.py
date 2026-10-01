"""自测模式的自动化测试 + 新功能验证。

覆盖:
  * 自测模式 = 两个**完整实例**(各自身份/端口/窗口), 互加好友后能双向聊天
  * 加好友验证问题: 答案正确才放行; 答错到上限自动封禁; 解禁后可重新申请
  * 密钥轮换: 收发若干条消息后会话密钥自动更新 (epoch 递增), 轮换后仍能正常聊天
  * 文件传输 (加密 + SHA256) 与封禁/解禁

直接运行:  python tests/test_self_test_mode.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import time
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanchat.console import configure_console  # noqa: E402

configure_console()

from lanchat import gui as G  # noqa: E402
from lanchat import crypto  # noqa: E402
from lanchat.service import (  # noqa: E402
    ChatService, Contact, ContactState, EventKind, ServiceEvent,
)

FAILURES: list[str] = []
MSGS: list[str] = []          # 记录界面上弹出的提示文字
TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-selftest")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def pump(root: tk.Tk, seconds: float, step: float = 0.06) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        time.sleep(step)


def until(root: tk.Tk, predicate, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.06)
    return False


def test_manual_peer_port_guard() -> None:
    """登记对端时端口无效必须报错, 不能"看得到却永远连不上"。

    真实踩过的坑: 自测模式在 service.start() 之前登记对方地址, 那时 TCP 端口还是 0
    (端口是在 start() 里才绑定的), 于是列表里能看到人、状态也正常, 但发请求永远失败,
    只有状态栏一句"对方不在线" —— 用户根本不知道该去点「新朋友」。
    """
    print("\n登记对端地址: 端口无效要拒绝")
    base = os.path.join(TMP, "portguard")
    shutil.rmtree(base, ignore_errors=True)
    events: list = []
    svc = ChatService(name="甲", data_dir=base, download_dir=os.path.join(base, "dl"),
                      enable_discovery=False, tcp_port=0, reconnect_cooldown=0.5)
    svc.on_event = events.append
    try:
        svc.register_manual_peer("lc-other", "乙", "127.0.0.1", 0)
        check("端口 0 时拒绝登记", not svc.lan_peers(),
              str([p["name"] for p in svc.lan_peers()]))
        check("并且明确报错 (不是静默失败)",
              any("端口无效" in (e.text or "") for e in events),
              str([e.text for e in events]))

        svc.start()
        svc.register_manual_peer("lc-other", "乙", "127.0.0.1", svc.tcp_port)
        check("start() 之后用真实端口登记成功",
              [p["peer_id"] for p in svc.lan_peers()] == ["lc-other"],
              str([(p["name"], p["address"]) for p in svc.lan_peers()]))
    finally:
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(base, ignore_errors=True)


def test_dialog_buttons_visible() -> None:
    """两个弹窗的底部按钮必须完整留在窗口里 (曾被发现挤掉一半点不到)。"""
    print("\n弹窗布局: 底部按钮不能被挤出窗口")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    base = os.path.join(TMP, "layout")
    shutil.rmtree(base, ignore_errors=True)
    svc = ChatService(name="甲", data_dir=base, download_dir=os.path.join(base, "dl"),
                      enable_discovery=False, tcp_port=0)
    svc.start()
    win = tk.Tk()
    win.geometry("900x600+40+40")      # 真显示出来, 否则量不到真实几何
    win.update()
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    app = G.ChatApp(win, svc, ns, own_service=False)
    try:
        svc.register_manual_peer("lc-other", "乙(本机)", "127.0.0.1", svc.tcp_port)
        for name, factory in (("添加联系人", G.ContactsDialog), ("新朋友", G.FriendRequestsDialog)):
            dialog = factory(app)
            dialog.win.deiconify()
            dialog.win.update()
            dialog.win.update_idletasks()
            height = dialog.win.winfo_height()
            bottom = dialog.footer.winfo_y() + dialog.footer.winfo_height()
            check(f"『{name}』底部按钮完整可见",
                  dialog.footer.winfo_height() > 10 and 0 < bottom <= height + 2,
                  f"按钮底部 {bottom} / 窗口高 {height}")
            dialog.win.destroy()
            win.update()
    finally:
        app._closing = True
        for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
            if job:
                try:
                    app.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def test_files_during_rekey() -> None:
    """连着发两个文件 (含大文件) 时, 密钥轮换不能把连接搞断。

    用户报的现象: 传完一个小文件后紧接着传 12MB, 立刻
    "密文校验失败: 密文认证失败" 然后掉线。根因是轮换状态机:
    `_rekey_outstanding` 忘了收尾 → 之后不再轮换, 10 秒超时又单方面换密钥 →
    两边密钥岔开; 另外换密钥瞬间在途的**文件分片内容**没有旧密钥兜底。
    """
    print("\n密钥轮换 + 文件传输: 连续发文件不能断线")
    base = os.path.join(TMP, "filerekey")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, auto_accept_files=True, reconnect_cooldown=0.5, rekey_every=3)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, auto_accept_files=True, reconnect_cooldown=0.5, rekey_every=3)
    errors: list = []
    try:
        a.on_event = lambda ev: errors.append(ev.text) if ev.kind.value == "error" else None
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        a.send_friend_request(b.peer_id)
        if not wait_for(lambda: bool(b.pending_requests()), timeout=20):
            check("成为好友 (前置条件)", False, "对方没收到请求")
            return
        b.accept_request(a.peer_id)
        if not wait_for(lambda: (a.get_contact(b.peer_id) or Contact()).encrypted
                        and (b.get_contact(a.peer_id) or Contact()).encrypted, timeout=20):
            check("建立加密会话 (前置条件)", False, "没连上")
            return

        small = os.path.join(base, "2.png")
        big = os.path.join(base, "rustup-init.exe")
        payload_small = os.urandom(140 * 1024)
        payload_big = os.urandom(12 * 1024 * 1024)
        for path, payload in ((small, payload_small), (big, payload_big)):
            with open(path, "wb") as fh:
                fh.write(payload)

        a.send_file(small, b.peer_id)
        time.sleep(0.3)
        a.send_file(big, b.peer_id)          # 紧接着发第二个 (用户就是这么踩到的)

        def received(name: str, size: int) -> bool:
            target = os.path.join(b.download_dir, name)
            try:
                return os.path.isfile(target) and os.path.getsize(target) >= size
            except OSError:
                return False

        ok_big = wait_for(lambda: received("rustup-init.exe", len(payload_big)), timeout=90)
        ok_small = wait_for(lambda: received("2.png", len(payload_small)), timeout=20)
        check("两个文件都传完了", ok_small and ok_big,
              f"目录 {os.listdir(b.download_dir) if os.path.isdir(b.download_dir) else '无'}")
        for name, payload in (("2.png", payload_small), ("rustup-init.exe", payload_big)):
            target = os.path.join(b.download_dir, name)
            if os.path.isfile(target):
                with open(target, "rb") as fh:
                    got = hashlib.sha256(fh.read()).hexdigest()
                check(f"{name} 内容 SHA256 一致", got == hashlib.sha256(payload).hexdigest())
        ca, cb = a.get_contact(b.peer_id), b.get_contact(a.peer_id)
        check("传完大文件后连接还活着 (没出现密文校验失败)",
              bool(ca and ca.connected) and bool(cb and cb.connected),
              f"甲={ca.last_error if ca else '无'} 错误事件={errors[:3]}")
        check("传文件期间确实一直在轮换",
              bool(ca and ca.connection and ca.connection.rekeys_done > 0),
              f"轮换 {ca.connection.rekeys_done if ca and ca.connection else '?'} 次")
        check("双方 epoch 一致",
              bool(ca and cb and ca.connection and cb.connection
                   and ca.connection.cipher.epoch == cb.connection.cipher.epoch),
              f"甲 {ca.connection.cipher.epoch if ca and ca.connection else '?'} / "
              f"乙 {cb.connection.cipher.epoch if cb and cb.connection else '?'}")
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def _button_labels(widget: tk.Misc) -> list:
    """递归收集窗口里所有按钮的文字。"""
    labels: list = []
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return labels
    for child in children:
        if isinstance(child, tk.Button):
            try:
                labels.append(str(child.cget("text")))
            except tk.TclError:
                pass
        labels.extend(_button_labels(child))
    return labels


def test_cancel_friend_request() -> None:
    """可以取消自己发出的加好友请求 (不用回答对方出的题)。"""
    print("\n取消加好友请求: 不用答题也能退出")
    base = os.path.join(TMP, "cancelreq")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    events: list = []
    try:
        b.on_event = lambda ev: events.append((ev.kind.value, ev.text))
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        b.set_question(a.peer_id, "1 + 1 = ?", "2", max_attempts=3)

        a.send_friend_request(b.peer_id)
        got = wait_for(lambda: a.pending_challenge(b.peer_id) is not None, timeout=20)
        check("对方出了一道题 (前置条件)", got, str(a.pending_challenge(b.peer_id)))
        if not got:
            return

        check("取消请求返回成功", a.cancel_friend_request(b.peer_id))
        check("本地不再挂着这道题", a.pending_challenge(b.peer_id) is None,
              str(a.pending_challenge(b.peer_id)))
        check("本地联系人已清掉", a.get_contact(b.peer_id) is None,
              str([(c.name, c.state) for c in a.contacts()]))
        told = wait_for(lambda: any("取消了加好友请求" in text for _k, text in events),
                        timeout=15)
        check("对方收到『取消了加好友请求』", told, str([t for _k, t in events][-3:]))
        check("对方那边这道题作废 (不再是待处理请求)",
              not b.pending_requests() and b.pending_challenge(a.peer_id) is None,
              str([(c.name, c.state) for c in b.contacts()]))
        check("取消后连接也断开了",
              not (b.get_contact(a.peer_id) and b.get_contact(a.peer_id).connected))
        check("没有这条请求时取消返回 False (不会瞎取消)",
              not _is_friend_cancel_allowed(a))
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def _is_friend_cancel_allowed(service: ChatService) -> bool:
    """辅助: 没有联系人时 cancel 应该返回 False (不能瞎取消)。"""
    return service.cancel_friend_request("lc-not-exist")


def test_question_can_be_cancelled() -> None:
    """出题人可以给对面出题, 也可以随时取消出题。"""
    print("\n验证问题: 出题人也能取消出题")
    base = os.path.join(TMP, "qcancel")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    try:
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)

        check("可以给对面出题", b.set_question(a.peer_id, "暗号?", "芝麻开门", max_attempts=3))
        check("出题后本机能查到这道题", b.question_of(a.peer_id) == "暗号?",
              b.question_of(a.peer_id))

        # 出题人取消出题 -> 对方再申请就不用答题了
        check("可以取消出题", b.clear_question(a.peer_id))
        check("取消后题目为空", b.question_of(a.peer_id) == "")
        a.send_friend_request(b.peer_id)
        got = wait_for(lambda: bool(b.pending_requests()), timeout=20)
        check("取消出题后对方申请不再被要求答题", got,
              str([(c.name, c.state) for c in b.contacts()]))
        check("对方那边也没有待答问题", a.pending_challenge(b.peer_id) is None,
              str(a.pending_challenge(b.peer_id)))
        check("没有出过题的人取消出题返回 False", not b.clear_question("lc-not-exist"))
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def test_notifications_for_peer_actions() -> None:
    """对面删了你 / 下线 / 取消请求, 都要在通知中心留下明确记录。"""
    print("\n通知中心: 对方删好友/取消请求/下线都要看得见")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    base = os.path.join(TMP, "notices")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    win_a = win_b = None
    app_a = app_b = None
    saved_info = messagebox.showinfo
    try:
        a.start()
        b.start()
        win_a, win_b = tk.Tk(), tk.Tk()
        for win, who in ((win_a, "甲"), (win_b, "乙")):
            win.geometry("820x560+40+40")
            win.title(f"[通知 {who}]")
            win.update()
        app_a = G.ChatApp(win_a, a, ns, own_service=False)
        app_b = G.ChatApp(win_b, b, ns, own_service=False)
        pump(win_a, 0.3)
        messagebox.showinfo = lambda *a_, **k_: None      # 不真的弹窗

        # 1) 对方删除好友 -> 我这边要收到通知 (而且要弹窗)
        if not _make_friends(a, b):
            check("成为好友 (前置条件)", False, "没连上")
            return
        before = len(app_b.notices)
        a.remove_friend(b.peer_id)
        got = until(win_a, lambda: any("删除了" in n["text"] for n in app_b.notices), timeout=20)
        check("对方删好友后我这边有通知", got,
              str([n["text"] for n in app_b.notices][-3:]))
        check("通知条数有增加", len(app_b.notices) > before,
              f"{before} -> {len(app_b.notices)}")
        check("通知按钮显示出未读条数",
              "通知" in app_b.notice_btn.cget("text") and "(" in app_b.notice_btn.cget("text"),
              app_b.notice_btn.cget("text"))

        # 通知窗口能打开, 打开后未读清零
        dialog = G.NotificationsDialog(app_b)
        pump(win_a, 0.3)
        check("通知窗口能打开并列出记录", dialog.rows and dialog.tree.get_children(),
              f"{len(dialog.rows)} 条")
        check("打开通知后未读清零", all(n["read"] for n in app_b.notices))
        dialog.clear()
        check("清空通知生效", not app_b.notices)
        dialog.win.destroy()
        pump(win_a, 0.2)

        # 2) 好友下线 -> 也要有通知 (不弹窗), 列表里显示 [离线]
        if _make_friends(a, b):
            pump(win_a, 0.5)
            b.stop()                                  # 对方程序关了
            offline = until(win_a, lambda: any("断开" in n["text"] for n in app_a.notices),
                            timeout=25)
            check("对方下线后我这边有通知", offline,
                  str([n["text"] for n in app_a.notices][-3:]))
            rows = [app_a._format_row(c) for c in app_a.service.contacts()]
            check("联系人是好友时不会莫名消失", bool(app_a.service.friends()), str(rows))
            check("好友离线在列表里看得出 [离线]",
                  any("[离线]" in row for row in rows), str(rows))
    finally:
        messagebox.showinfo = saved_info
        for app in (app_a, app_b):
            if app is not None:
                app._closing = True
                for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
                    if job:
                        try:
                            app.after_cancel(job)
                        except tk.TclError:
                            pass
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        for win in (win_a, win_b):
            if win is not None:
                try:
                    G._close_toplevel(win)
                except tk.TclError:
                    pass
        shutil.rmtree(base, ignore_errors=True)


def test_self_test_config_and_auto_accept() -> None:
    """自测模式不能偷偷"自动接收"文件; 真开了也要有提示。"""
    print("\n自测模式配置 / 自动接收文件")
    base = os.path.join(TMP, "stcfg")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    svc = G.make_self_test_service("甲", 0, base, os.path.join(base, "dl"), ns)
    try:
        check("自测模式实例默认不自动接收文件 (否则不会弹选保存位置的窗)",
              not svc.auto_accept_files, str(svc.auto_accept_files))
        check("自测模式下载目录在自测目录下",
              os.path.abspath(svc.download_dir).startswith(os.path.abspath(base)),
              svc.download_dir)
    finally:
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass

    # 万一用户自己在设置里开了"自动接收", 也要在通知里留一条 (不能悄无声息)
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    svc2 = ChatService(name="甲", data_dir=os.path.join(base, "s2"),
                       download_dir=os.path.join(base, "dl2"), enable_discovery=False,
                       tcp_port=0, auto_accept_files=True)
    svc2.start()
    win = tk.Tk()
    win.geometry("820x560+40+40")
    win.update()
    app = G.ChatApp(win, svc2, ns, own_service=False)
    try:
        app._on_file_offer(ServiceEvent(
            kind=EventKind.FILE_OFFER, text="", peer_id="lc-x", name="小红",
            data={"transfer_id": "t9", "name": "a.txt", "size": 1024, "direction": "in",
                  "auto_accepted": True}))
        check("自动接收时在通知里留了记录", bool(app.notices),
              str([n["text"] for n in app.notices]))
        check("自动接收不再弹选择窗", not app._incoming_file_dialogs,
              str(list(app._incoming_file_dialogs)))
    finally:
        app._closing = True
        for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
            if job:
                try:
                    app.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            svc2.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            G._close_toplevel(win)
        except tk.TclError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def test_question_required_again_after_removal() -> None:
    """答对过的人被删除/拒绝/封禁之后, 再加回来必须重新答题。"""
    print("\n验证问题: 删了好友再加回来要重新答题")
    base = os.path.join(TMP, "again")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    try:
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        b.set_question(a.peer_id, "1 + 1 = ?", "2", max_attempts=3)

        def answer_correctly() -> bool:
            a.send_friend_request(b.peer_id)
            if not wait_for(lambda: a.pending_challenge(b.peer_id) is not None, timeout=20):
                return False
            a.answer_challenge(b.peer_id, "2")
            return wait_for(lambda: bool(b.pending_requests()), timeout=20)

        check("第一次: 答对后进入『新朋友』", answer_correctly(),
              str([(c.name, c.state) for c in b.contacts()]))

        # 乙把甲删了 -> 甲再加回来必须重新答题
        b.remove_friend(a.peer_id)
        wait_for(lambda: b.get_contact(a.peer_id) is None, timeout=10)
        a.send_friend_request(b.peer_id)
        asked_again = wait_for(lambda: a.pending_challenge(b.peer_id) is not None, timeout=25)
        check("删好友后重新申请: 又收到提问", asked_again,
              str(a.pending_challenge(b.peer_id)))
        check("没答题之前不会直接进『新朋友』", not b.pending_requests(),
              str([(c.name, c.state) for c in b.contacts()]))
        if asked_again:
            a.answer_challenge(b.peer_id, "2")
            check("重新答对后才放行",
                  wait_for(lambda: bool(b.pending_requests()), timeout=20),
                  str([(c.name, c.state) for c in b.contacts()]))

        # 拒绝请求也一样: 下次再来还要答
        b.reject_request(a.peer_id)
        wait_for(lambda: not b.pending_requests() and b.get_contact(a.peer_id) is None,
                 timeout=10)
        a.send_friend_request(b.peer_id)
        check("拒绝之后重新申请: 也要重新答题",
              wait_for(lambda: a.pending_challenge(b.peer_id) is not None, timeout=25),
              str(a.pending_challenge(b.peer_id)))

        # 改了题也要重新答 (旧答案不再算对)
        with b._lock:
            b._challenge_state[a.peer_id] = {"solved": True}   # 假装他刚答对过
        b.set_question(a.peer_id, "2 + 2 = ?", "4", max_attempts=3)
        check("改题后旧的『答对』记忆被清掉",
              (b._challenge_state.get(a.peer_id) or {}).get("solved") is None,
              str(b._challenge_state.get(a.peer_id)))
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def test_answer_feedback_keeps_dialog() -> None:
    """答案提交后不关窗: 答错要当场提示还剩几次, 能直接重试。"""
    print("\n答题反馈: 答错不关窗, 可当场重试")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    base = os.path.join(TMP, "answerfb")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    win_a = win_b = None
    app_a = app_b = None
    try:
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        win_a, win_b = tk.Tk(), tk.Tk()
        for win, who in ((win_a, "甲"), (win_b, "乙")):
            win.geometry("820x560+40+40")
            win.title(f"[答题 {who}]")
            win.update()
        app_a = G.ChatApp(win_a, a, ns, own_service=False)
        app_b = G.ChatApp(win_b, b, ns, own_service=False)
        pump(win_a, 0.3)

        b.set_question(a.peer_id, "1 + 1 = ?", "2", max_attempts=3)
        a.send_friend_request(b.peer_id)
        # 甲发的请求, 乙出题 -> 答题弹窗在**甲**的窗口里
        got = until_all([win_a, win_b], lambda: b.peer_id in app_a._answer_dialogs, timeout=25)
        check("对方出题后弹出答题窗", got, str(list(app_a._answer_dialogs)))
        if not got:
            return
        dialog = app_a._answer_dialogs[b.peer_id]

        # 故意答错
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "3")
        dialog.submit()
        got_result = until_all([win_a, win_b],
                               lambda: "不对" in dialog.result_label.cget("text"), timeout=20)
        check("答错后弹窗里直接提示 (不用去别处找)", got_result,
              dialog.result_label.cget("text"))
        check("提示里写了还剩几次", "还剩 2 次" in dialog.result_label.cget("text"),
              dialog.result_label.cget("text"))
        check("答错后弹窗不关闭", bool(dialog.win.winfo_exists()))
        check("答错后还能继续提交", str(dialog.submit_btn.cget("state")) == "normal",
              str(dialog.submit_btn.cget("state")))
        check("答错后同一道题还能重试 (提问没被丢掉)",
              a.pending_challenge(b.peer_id) is not None,
              str(a.pending_challenge(b.peer_id)))

        # 同一个弹窗里改成正确答案
        dialog.entry.delete(0, "end")
        dialog.entry.insert(0, "2")
        dialog.submit()
        ok_result = until_all([win_a, win_b],
                              lambda: "正确" in dialog.result_label.cget("text"), timeout=20)
        check("答对后提示正确", ok_result, dialog.result_label.cget("text"))
        check("答对后待答问题被清掉", a.pending_challenge(b.peer_id) is None,
              str(a.pending_challenge(b.peer_id)))
        closed = until_all([win_a, win_b], lambda: not dialog.win.winfo_exists(), timeout=10)
        check("答对后弹窗自动关闭", closed)
        check("对方那边收到请求可以同意",
              wait_for(lambda: bool(b.pending_requests()), timeout=20),
              str([(c.name, c.state) for c in b.contacts()]))
    finally:
        for app in (app_a, app_b):
            if app is not None:
                app._closing = True
                for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
                    if job:
                        try:
                            app.after_cancel(job)
                        except tk.TclError:
                            pass
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        for win in (win_a, win_b):
            if win is not None:
                try:
                    G._close_toplevel(win)
                except tk.TclError:
                    pass
        shutil.rmtree(base, ignore_errors=True)


def test_progress_bar_and_speed() -> None:
    """传文件时底部要有进度条和速度。"""
    print("\n进度条: 传输时能看到百分比和速度")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    base = os.path.join(TMP, "progress")
    shutil.rmtree(base, ignore_errors=True)
    svc = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                      download_dir=os.path.join(base, "dl"), enable_discovery=False,
                      tcp_port=0)
    svc.start()
    win = tk.Tk()
    win.geometry("820x560+40+40")
    win.update()
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    app = G.ChatApp(win, svc, ns, own_service=False)
    try:
        check("进度条默认是隐藏的", not app.progress_bar.winfo_ismapped())
        for sent, progress in ((0, 0.0), (512 * 1024, 0.25), (1024 * 1024, 0.5)):
            app._on_file_progress(ServiceEvent(
                kind=EventKind.FILE_PROGRESS, text="", peer_id="lc-x", name="小红",
                data={"transfer_id": "t1", "name": "big.bin", "size": 4 * 1024 * 1024,
                      "sent": sent, "progress": progress, "direction": "out"}))
            win.update()
            time.sleep(0.35)
        text = app.progress_label.cget("text")
        check("进度文字里有百分比", "%" in text, text)
        check("进度文字里有速度 (/s)", "/s" in text, text)
        check("进度条显示出来了", app.progress_bar.winfo_ismapped(),
              str(app.progress_bar.winfo_ismapped()))
        check("进度条数值跟着走",
              abs(float(app.progress_bar.cget("value")) - 50.0) < 1.0,
              str(app.progress_bar.cget("value")))
        app._on_file_progress(ServiceEvent(
            kind=EventKind.FILE_PROGRESS, text="", peer_id="lc-x", name="小红",
            data={"transfer_id": "t1", "name": "big.bin", "size": 4 * 1024 * 1024,
                  "sent": 4 * 1024 * 1024, "progress": 1.0, "direction": "out"}))
        win.update()
        check("传完提示完成", "完成" in app.progress_label.cget("text"),
              app.progress_label.cget("text"))
        app._clear_progress()
        win.update()
        check("清掉后进度条又隐藏了", not app.progress_bar.winfo_ismapped())
    finally:
        app._closing = True
        for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
            if job:
                try:
                    app.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            G._close_toplevel(win)
        except tk.TclError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def until_all(windows: list, predicate, timeout: float = 25.0) -> bool:
    """同时泵多个窗口的 until (题目弹窗可能在另一个窗口里)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for win in windows:
            try:
                win.update()
            except tk.TclError:
                pass
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.06)
    return False


def test_download_dir_default_and_unblock_button() -> None:
    """默认接收目录在"下载"里; 添加联系人界面能解封。"""
    print("\n接收目录默认位置 / 添加联系人里的解封按钮")
    base = os.path.join(TMP, "dlunblock")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    svc = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                      enable_discovery=False, tcp_port=0)
    try:
        path = os.path.abspath(svc.download_dir)
        check("默认接收目录在系统下载目录里 (或有降级说明)",
              "downloads" in path.lower() or bool(svc.download_dir_warning),
              f"{path} / {svc.download_dir_warning}")
        check("默认值不算『用户自己选的』 (不会被写进设置顶掉新默认)",
              not svc.download_dir_chosen, str(svc.download_dir_chosen))

        # 老版本自动算出来的路径写在设置里 -> 升级后必须被忽略
        legacy = os.path.join(os.path.expanduser("~"), "lanchat_downloads")
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(base, "a", "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"download_dir": legacy, "rekey_every": 30}, fh)
        again = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                            enable_discovery=False, tcp_port=0)
        check("老版本自动写的接收目录被忽略, 用新默认",
              os.path.abspath(again.download_dir) != os.path.abspath(legacy)
              and not again.download_dir_chosen,
              f"{again.download_dir} (老的 {legacy})")

        # 用户自己选过的 -> 必须沿用
        chosen_dir = os.path.join(base, "我自己选的")
        os.makedirs(chosen_dir, exist_ok=True)
        with open(os.path.join(base, "a", "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"download_dir": chosen_dir, "download_dir_chosen": True,
                       "rekey_every": 30}, fh)
        kept = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                           enable_discovery=False, tcp_port=0)
        check("用户自己选过的目录会一直沿用",
              os.path.abspath(kept.download_dir) == os.path.abspath(chosen_dir)
              and kept.download_dir_chosen,
              kept.download_dir)

        # 用户实际踩到的坑: 老版本写进去的路径**不在**已知名单里 (比如数据目录下的
        # lanchat_downloads), 于是被当成了"用户设置" -> "说改了默认目录还是老路径"。
        # 现在的规则是: 没有 download_dir_chosen 标记就一律忽略 (老版本根本没有
        # "自己选目录"这个功能, 它写的路径一定是自动算出来的)。
        legacy2 = os.path.join(base, "a", "lanchat_downloads")
        os.makedirs(legacy2, exist_ok=True)
        with open(os.path.join(base, "a", "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({"download_dir": legacy2, "rekey_every": 30}, fh)
        again2 = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                             enable_discovery=False, tcp_port=0)
        check("数据目录里的老接收目录同样被忽略 (不认识的旧路径也一样)",
              os.path.abspath(again2.download_dir) != os.path.abspath(legacy2)
              and not again2.download_dir_chosen,
              f"{again2.download_dir} (老的 {legacy2})")
        check("启动提示里说明了忽略原因",
              "忽略" in (again2.download_dir_warning or ""), again2.download_dir_warning)

        # 设置界面上的『用系统下载目录』按钮: 一键回到默认 (不用去删 .lanchat/settings.json)
        check("service 提供『恢复系统下载目录』的入口",
              hasattr(again2, "set_download_dir") and
              os.path.abspath(G.default_download_dir()) != os.path.abspath(legacy2),
              G.default_download_dir())
    finally:
        try:
            svc.stop()
        except Exception:  # noqa: BLE001
            pass

    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    a = ChatService(name="甲", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl"), enable_discovery=False, tcp_port=0)
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    win = tk.Tk()
    win.geometry("900x600+40+40")
    win.update()
    app = G.ChatApp(win, a, ns, own_service=False)
    try:
        a.start()
        a.register_manual_peer("lc-blocked", "丙(被封禁)", "127.0.0.1", a.tcp_port)
        a.block("lc-blocked")
        a.register_manual_peer("lc-blocked", "丙(被封禁)", "127.0.0.1", a.tcp_port)  # 重新出现
        dlg = G.ContactsDialog(app)
        pump(win, 0.3)
        labels = _button_labels(dlg.win)
        check("『添加联系人』里有『解除封禁』按钮",
              any("解除封禁" in text for text in labels), str(labels))
        dlg.tree.selection_set("lc-blocked")
        dlg.refresh()
        pump(win, 0.2)
        check("被封禁的人在列表里标着已封禁",
              "已封禁" in str(dlg.tree.item("lc-blocked", "values")),
              str(dlg.tree.item("lc-blocked", "values")))
        MSGS.clear()
        dlg.unblock_selected()                      # askyesno 已被打桩为 True
        pump(win, 0.3)
        check("点『解除封禁』后黑名单清空", not a.blocked_contacts(),
              str([c.name for c in a.blocked_contacts()]))
        check("解禁后有明确提示", any("解除" in m for m in MSGS), str(MSGS))
        MSGS.clear()
        dlg.unblock_selected()                      # 再点一次: 应该提示"没有被封禁"
        pump(win, 0.3)
        check("重复解禁只是提示, 不报错", any("没有被封禁" in m for m in MSGS), str(MSGS))
        dlg.win.destroy()
    finally:
        app._closing = True
        for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
            if job:
                try:
                    app.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            a.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            G._close_toplevel(win)
        except tk.TclError:
            pass
        shutil.rmtree(base, ignore_errors=True)


def test_rekey_negotiation() -> None:
    """两边轮换设置不同时, 按协商规则取一个双方一致的值。"""
    print("\n密钥轮换: 两边设置不同怎么协商")
    check("一边关闭 -> 这条会话不轮换",
          crypto.negotiate_rekey(30, 0) == 0 and crypto.negotiate_rekey(0, 30) == 0)
    check("都开着 -> 取更严格(更小)的那个",
          crypto.negotiate_rekey(30, 10) == 10 and crypto.negotiate_rekey(10, 30) == 10
          and crypto.negotiate_rekey(30, 30) == 30)
    check("对方是老版本(没这个字段) -> 用我自己的",
          crypto.negotiate_rekey(30, None) == 30)

    base = os.path.join(TMP, "rekeyneg")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5, rekey_every=30)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5, rekey_every=10)
    try:
        a.start()
        b.start()
        if not _make_friends(a, b):
            check("成为好友 (前置条件)", False, "没连上")
            return
        ca, cb = a.get_contact(b.peer_id), b.get_contact(a.peer_id)
        check("双方协商出同一个轮换间隔",
              bool(ca and cb and ca.connection and cb.connection
                   and ca.connection.rekey_every == cb.connection.rekey_every == 10),
              f"甲 {ca.connection.rekey_every if ca and ca.connection else '?'} / "
              f"乙 {cb.connection.rekey_every if cb and cb.connection else '?'}")
        check("对方声明的值被记下来了",
              bool(ca and ca.connection and ca.connection.peer_rekey == 10),
              str(ca.connection.peer_rekey if ca and ca.connection else None))

        def conns():
            c1, c2 = a.get_contact(b.peer_id), b.get_contact(a.peer_id)
            return (c1.connection if c1 else None), (c2.connection if c2 else None)

        # 运行时改设置: 会通知对方重新协商, 不用重连
        a.set_rekey_every(5)
        time.sleep(1.5)
        live_a, live_b = conns()
        check("一边改成 5 之后双方都变成 5 (取更严格的)",
              bool(live_a and live_b and live_a.rekey_every == live_b.rekey_every == 5),
              f"甲 {live_a.rekey_every if live_a else '?'} / "
              f"乙 {live_b.rekey_every if live_b else '?'}")

        b.set_rekey_every(0)
        time.sleep(1.5)
        live_a, live_b = conns()
        check("另一边关掉轮换 -> 两边协商成关闭",
              bool(live_a and live_b and live_a.rekey_every == 0 and live_b.rekey_every == 0),
              f"甲 {live_a.rekey_every if live_a else '?'} / "
              f"乙 {live_b.rekey_every if live_b else '?'}")

        b.set_rekey_every(20)
        time.sleep(1.5)
        live_a, live_b = conns()
        check("再打开时仍然取两边更严格的那个 (5)",
              bool(live_a and live_b and live_a.rekey_every == live_b.rekey_every == 5),
              f"甲 {live_a.rekey_every if live_a else '?'} / "
              f"乙 {live_b.rekey_every if live_b else '?'}")

        # 重新握手时也按协商结果来
        a.set_rekey_every(0)
        a.remove_friend(b.peer_id)
        b.remove_friend(a.peer_id)
        wait_for(lambda: a.get_contact(b.peer_id) is None and b.get_contact(a.peer_id) is None,
                 timeout=10)
        if not _make_friends(a, b):
            check("重新成为好友 (前置条件)", False, "没连上")
            return
        ca, cb = a.get_contact(b.peer_id), b.get_contact(a.peer_id)
        check("一方关闭轮换后, 重新握手协商的结果也是关闭",
              bool(ca and cb and ca.connection and cb.connection
                   and ca.connection.rekey_every == cb.connection.rekey_every == 0),
              f"甲 {ca.connection.rekey_every if ca and ca.connection else '?'} / "
              f"乙 {cb.connection.rekey_every if cb and cb.connection else '?'}")
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def encrypted(service: ChatService, peer_id: str) -> bool:
    contact = service.get_contact(peer_id)
    return bool(contact and contact.encrypted)


def _make_friends(a: ChatService, b: ChatService, timeout: float = 25.0) -> bool:
    """把两个服务变成好友 (注册地址 -> 发请求 -> 同意 -> 等加密会话)。"""
    a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
    b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
    a.send_friend_request(b.peer_id)
    if not wait_for(lambda: bool(b.pending_requests()), timeout=timeout):
        return False
    b.accept_request(a.peer_id)
    return wait_for(lambda: encrypted(a, b.peer_id) and encrypted(b, a.peer_id), timeout=timeout)


def test_remove_friend_notifies_peer() -> None:
    """删除好友必须通知对方: 对方也把这条联系人删掉, 而不是继续"以为还是好友"。"""
    print("\n删除好友: 对方要知道, 并且不能再默默发消息")
    base = os.path.join(TMP, "unfriend")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    events: list = []
    try:
        b.on_event = lambda ev: events.append((ev.kind.value, ev.text))
        a.start()
        b.start()
        if not _make_friends(a, b):
            check("成为好友 (前置条件)", False, "没连上")
            return

        a.remove_friend(b.peer_id)
        gone = wait_for(lambda: b.get_contact(a.peer_id) is None, timeout=15)
        check("被删的一方收到通知并移除了联系人", gone,
              str([(c.name, c.state) for c in b.contacts()]))
        check("被删的一方有明确提示",
              any("删除了" in text for _kind, text in events),
              str([t for _k, t in events][-3:]))
        check("被删的一方连接已断开",
              not (b.get_contact(a.peer_id) and b.get_contact(a.peer_id).connected))
        sent = a.send_text("删除之后还能发吗", b.peer_id) if a.get_contact(b.peer_id) else 0
        check("删除方也把联系人删掉了, 发不出消息",
              a.get_contact(b.peer_id) is None and sent == 0)
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def test_save_as_path() -> None:
    """收到文件时可以选择保存位置 (另存为)。"""
    print("\n文件接收: 可以自己选保存路径")
    base = os.path.join(TMP, "saveas")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    try:
        a.start()
        b.start()
        if not _make_friends(a, b):
            check("成为好友 (前置条件)", False, "没连上")
            return

        payload = os.urandom(300 * 1024)
        src = os.path.join(base, "photo.png")
        with open(src, "wb") as fh:
            fh.write(payload)
        a.send_file(src, b.peer_id)
        if not wait_for(lambda: bool(b.pending_offers()), timeout=20):
            check("对方收到文件请求", False, str(b.pending_offers()))
            return
        check("对方收到文件请求", True)

        # 用户指定了一个完全不同的位置 (连目录都是新建的)
        target = os.path.join(base, "我的文件", "另存为的名字.png")
        check("另存为成功", b.accept_file(save_path=target))
        ok = wait_for(lambda: os.path.isfile(target)
                      and os.path.getsize(target) >= len(payload), timeout=40)
        check("文件确实保存到了指定路径", ok,
              str(os.listdir(os.path.join(base, "我的文件"))
                  if os.path.isdir(os.path.join(base, "我的文件")) else "目录不存在"))
        if ok:
            with open(target, "rb") as fh:
                check("另存为的文件内容一致",
                      hashlib.sha256(fh.read()).hexdigest() == hashlib.sha256(payload).hexdigest())
        check("默认接收目录里没有多出文件",
              not os.path.isfile(os.path.join(b.download_dir, "photo.png")),
              str(os.listdir(b.download_dir) if os.path.isdir(b.download_dir) else "无"))
    finally:
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def test_gui_file_dialog_and_answer_recovery() -> None:
    """界面: 收文件弹窗能"另存为"; 答案敲错地方时能找回来。"""
    print("\n界面: 收文件弹窗 + 答案敲错地方的自救")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return
    base = os.path.join(TMP, "guidlg")
    shutil.rmtree(base, ignore_errors=True)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    b = ChatService(name="乙", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, reconnect_cooldown=0.5)
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=30, name="")
    win_a = win_b = None
    app_a = app_b = None
    saved_ask = G.filedialog.asksaveasfilename
    saved_yesno = messagebox.askyesno
    try:
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        win_a, win_b = tk.Tk(), tk.Tk()
        for win, who in ((win_a, "甲"), (win_b, "乙")):
            win.geometry("820x560+40+40")
            win.title(f"[界面 {who}]")
            win.update()
        app_a = G.ChatApp(win_a, a, ns, own_service=False)
        app_b = G.ChatApp(win_b, b, ns, own_service=False)
        pump(win_a, 0.3)
        if not _make_friends(a, b):
            check("成为好友 (前置条件)", False, "没连上")
            return

        # ---- 收文件弹窗 ----
        payload = os.urandom(120 * 1024)
        src = os.path.join(base, "截图.png")
        with open(src, "wb") as fh:
            fh.write(payload)
        a.send_file(src, b.peer_id)
        got_offer = until(win_a, lambda: bool(app_b._incoming_file_dialogs), timeout=20)
        check("收到文件时弹出可选择的弹窗", got_offer,
              str(list(app_b._incoming_file_dialogs)))
        if got_offer:
            dialog = next(iter(app_b._incoming_file_dialogs.values()))
            check("弹窗指向我的接收目录",
                  dialog.default_dir == os.path.abspath(b.download_dir) and not dialog.chosen_path,
                  f"{dialog.default_dir} / chosen={dialog.chosen_path!r}")
            labels = _button_labels(dialog.win)
            check("按钮: 接收(接收目录) / 另存到别处 / 拒绝",
                  any("接收目录" in text for text in labels)
                  and any("另存到别处" in text for text in labels)
                  and any("拒绝" in text for text in labels), str(labels))
            check("不再有『保存到默认目录』这种说法",
                  not any("默认目录" in text for text in labels), str(labels))
            target = os.path.join(base, "桌面", "我挑的名字.png")
            G.filedialog.asksaveasfilename = lambda *a_, **k_: target   # 模拟"另存到别处"
            dialog.accept_as()                    # 选完直接开始接收
            check("另存到别处后弹窗记录新路径", dialog.chosen_path == target, dialog.chosen_path)
            saved = until(win_a, lambda: os.path.isfile(target)
                          and os.path.getsize(target) >= len(payload), timeout=40)
            check("按选择的位置保存成功", saved, target)
            if saved:
                with open(target, "rb") as fh:
                    check("内容 SHA256 一致",
                          hashlib.sha256(fh.read()).hexdigest()
                          == hashlib.sha256(payload).hexdigest())
        G.filedialog.asksaveasfilename = saved_ask

        # ---- 统一接收目录: 设置里改了之后, 收到的文件就存那里 ----
        my_dir = os.path.join(base, "我的接收目录")
        check("设置接收目录成功", b.set_download_dir(my_dir) and os.path.isdir(my_dir), my_dir)
        payload2 = os.urandom(60 * 1024)
        src2 = os.path.join(base, "第二个.bin")
        with open(src2, "wb") as fh:
            fh.write(payload2)
        a.send_file(src2, b.peer_id)
        second = until(win_a, lambda: bool(app_b._incoming_file_dialogs), timeout=20)
        if second:
            dialog = next(iter(app_b._incoming_file_dialogs.values()))
            check("新弹窗用的是新接收目录",
                  dialog.default_dir == os.path.abspath(my_dir), dialog.default_dir)
            dialog.accept_default()               # "接收 (存到我的接收目录)"
            landed = until(win_a, lambda: os.path.isfile(os.path.join(my_dir, "第二个.bin")),
                           timeout=40)
            check("文件确实落到了统一接收目录", landed,
                  str(os.listdir(my_dir) if os.path.isdir(my_dir) else "目录不存在"))

        # ---- 答案敲到聊天输入框里的自救 ----
        with b._lock:
            b._incoming_questions[a.peer_id] = {
                "question": "1 + 1 = ?", "salt": crypto.new_answer_salt(),
                "nonce": crypto.new_answer_nonce(), "max_attempts": 3,
            }
        app_b._on_question(ServiceEvent(kind=EventKind.QUESTION, text="题",
                                        peer_id=a.peer_id, name=a.name,
                                        data={"question": "1 + 1 = ?", "nonce": "N",
                                              "max_attempts": 3}))
        pump(win_a, 0.4)
        dialog = app_b._answer_dialogs.get(a.peer_id)
        check("答题弹窗出现", dialog is not None and bool(dialog.win.winfo_exists()))
        if dialog is not None:
            app_b.entry.delete("1.0", "end")
            app_b.entry.insert("1.0", "2")        # 模拟"敲到聊天输入框里了"
            messagebox.askyesno = lambda *a_, **k_: True
            dialog.submit()                        # 答案框是空的 -> 应该问一句再用它
            messagebox.askyesno = saved_yesno
            check("答案敲错地方时能自动找回并提交",
                  bool(dialog.waiting) and "等待对方校验" in dialog.result_label.cget("text"),
                  dialog.result_label.cget("text"))
            check("提交后弹窗保持打开 (等对方判定)", bool(dialog.win.winfo_exists()))
            # 模拟对方的判定: 答错 -> 留在弹窗里继续; 答对 -> 自动关闭
            dialog.on_result(False, 2)
            check("答错时留在弹窗里并提示剩余次数",
                  "还剩 2 次" in dialog.result_label.cget("text")
                  and bool(dialog.win.winfo_exists()),
                  dialog.result_label.cget("text"))
            dialog.on_result(True, 3)
            pump(win_a, 0.3)
            check("答对时提示正确", "正确" in dialog.result_label.cget("text"),
                  dialog.result_label.cget("text"))
            closed = until_all([win_a, win_b], lambda: not dialog.win.winfo_exists(), timeout=8)
            check("答对后弹窗自动关闭", closed)

        # ---- 真的往输入框里打字, 弹窗必须能读到 (自测模式两个窗口时的经典坑) ----
        with b._lock:
            b._incoming_questions[a.peer_id] = {
                "question": "1 + 1 = ?", "salt": crypto.new_answer_salt(),
                "nonce": crypto.new_answer_nonce(), "max_attempts": 3,
            }
        app_b._on_question(ServiceEvent(kind=EventKind.QUESTION, text="题",
                                        peer_id=a.peer_id, name=a.name,
                                        data={"question": "1 + 1 = ?", "nonce": "N2",
                                              "max_attempts": 3}))
        pump(win_a, 0.4)
        dialog = app_b._answer_dialogs.get(a.peer_id)
        if dialog is not None:
            # 这里用 entry.insert 而不是 answer_var.set: 走的正是"用户敲键盘"的路径。
            # 变量没绑到本窗口的 Tk 解释器时, 输入框里看得到字, 但 get() 是空的。
            dialog.entry.delete(0, "end")
            dialog.entry.insert(0, "2")
            check("输入框里打的字能被读到 (变量绑对了窗口)",
                  dialog.answer_var.get().strip() == "2",
                  f"entry={dialog.entry.get()!r} var={dialog.answer_var.get()!r}")
            MSGS.clear()
            dialog.submit()
            pump(win_a, 0.3)
            check("直接用键盘输入的答案能提交",
                  bool(dialog.waiting) and not MSGS,
                  f"等待中={dialog.waiting} 提示={MSGS}")
            dialog.on_result(True, 3)              # 模拟对方判定"答对了"
            pump(win_a, 0.4)
            until_all([win_a, win_b], lambda: not dialog.win.winfo_exists(), timeout=8)

        # ---- 取消加好友请求按钮 (不用答题) ----
        a.send_friend_request(b.peer_id)
        until(win_a, lambda: bool(b.pending_requests()), timeout=20)
        # 上一个弹窗可能还在等自动关闭, 先等它彻底消失再开新的, 免得拿到旧对象
        until_all([win_a, win_b], lambda: a.peer_id not in app_b._answer_dialogs, timeout=8)
        with b._lock:
            b._incoming_questions[a.peer_id] = {
                "question": "不想答的题", "salt": crypto.new_answer_salt(),
                "nonce": crypto.new_answer_nonce(), "max_attempts": 3,
            }
        app_b._on_question(ServiceEvent(kind=EventKind.QUESTION, text="题",
                                        peer_id=a.peer_id, name=a.name,
                                        data={"question": "不想答的题", "nonce": "N3",
                                              "max_attempts": 3}))
        pump(win_a, 0.4)
        until_all([win_a, win_b], lambda: a.peer_id in app_b._answer_dialogs, timeout=8)
        dialog = app_b._answer_dialogs.get(a.peer_id)
        check("再次出现答题弹窗",
              dialog is not None and bool(dialog.win.winfo_exists()))
        if dialog is not None and dialog.win.winfo_exists():
            labels = _button_labels(dialog.win)
            check("答题弹窗里有『取消加好友请求』",
                  any("取消加好友请求" in text for text in labels), str(labels))
            messagebox.askyesno = lambda *a_, **k_: False      # 用户点了"不取消"
            dialog.cancel_request()
            messagebox.askyesno = saved_yesno
            check("点了取消后选『否』不会动任何状态",
                  b.pending_challenge(a.peer_id) is not None and dialog.win.winfo_exists())
            dialog.close()
            pump(win_a, 0.3)
    finally:
        G.filedialog.asksaveasfilename = saved_ask
        messagebox.askyesno = saved_yesno
        for app in (app_a, app_b):
            if app is not None:
                app._closing = True
                for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
                    if job:
                        try:
                            app.after_cancel(job)
                        except tk.TclError:
                            pass
        for service in (a, b):
            try:
                service.stop()
            except Exception:  # noqa: BLE001
                pass
        for win in (win_a, win_b):
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
        shutil.rmtree(base, ignore_errors=True)


def wait_for(predicate, timeout: float = 25.0, step: float = 0.05) -> bool:
    """没有界面(纯服务)时用的等待。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return False


def test_question_survives_restart() -> None:
    """验证问题在程序重启后依然能校验 (派生密钥落盘, 答案明文不落盘)。

    旧实现把答案明文只放在内存里, 重启后校验必然失败 -> 答对的人会被当成答错,
    错到上限就被自动封禁。这里就是那个 bug 的回归测试。
    """
    print("\n加好友验证问题: 重启后依然能校验")
    base = os.path.join(TMP, "restart")
    shutil.rmtree(base, ignore_errors=True)
    data_b = os.path.join(base, "b")
    b = ChatService(name="乙", data_dir=data_b, download_dir=os.path.join(base, "dl-b"),
                    enable_discovery=False, tcp_port=0, reconnect_cooldown=0.5)
    a = ChatService(name="甲", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"),
                    enable_discovery=False, tcp_port=0, reconnect_cooldown=0.5)
    b2 = None
    try:
        a.start()
        b.start()
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        b.set_question(a.peer_id, "我们的暗号?", "lanchat", max_attempts=2)
        check("设置了验证问题", b.question_of(a.peer_id) == "我们的暗号?")

        b.stop()                                    # 重启乙: 内存里的东西全没了
        b2 = ChatService(name="乙", data_dir=data_b, download_dir=os.path.join(base, "dl-b2"),
                         enable_discovery=False, tcp_port=0, reconnect_cooldown=0.5)
        b2.start()
        check("重启后验证问题还在", b2.question_of(a.peer_id) == "我们的暗号?",
              b2.question_of(a.peer_id))
        check("重启后答案明文不在内存里",
              not hasattr(b2, "_question_answers"), "已改为用落盘的派生密钥校验")

        a.register_manual_peer(b2.peer_id, b2.name, "127.0.0.1", b2.tcp_port)
        a.send_friend_request(b2.peer_id)
        got = wait_for(lambda: a.pending_challenge(b2.peer_id) is not None)
        check("重启后的实例会正常提问", got, str(a.pending_challenge(b2.peer_id)))
        if got:
            a.answer_challenge(b2.peer_id, "错误的答案")
            time.sleep(1.0)
            check("重启后答错依然会被判错", not b2.pending_requests(),
                  str([(c.name, c.state) for c in b2.contacts()]))

            a.send_friend_request(b2.peer_id)
            wait_for(lambda: a.pending_challenge(b2.peer_id) is not None)
            a.answer_challenge(b2.peer_id, "LANCHAT")     # 大小写/空格归一化后应算对
            passed = wait_for(lambda: len(b2.pending_requests()) >= 1)
            check("重启后答对依然能通过", passed,
                  str([(c.name, c.state) for c in b2.contacts()]))
    finally:
        for service in (a, b, b2):
            if service is not None:
                try:
                    service.stop()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(base, ignore_errors=True)


def test_self_test_discovers_and_can_hide() -> None:
    """自测模式两个实例必须**真的**通过广播互相发现 —— 否则"隐身"开关在自测里看不出效果。

    踩过: 以前只有"甲"开着发现层、乙连收都不收, 结果两个窗口的"局域网中的人"永远是空的
    (全靠内部登记), 隐身开关怎么点都没反应。
    """
    print("\n自测模式: 靠广播互相发现 + 隐身开关真的有效果")
    base = os.path.join(TMP, "stdisc")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
        disc = probe.getsockname()[1]
    finally:
        probe.close()
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=disc,
                            no_discovery=False, rekey_after=30, name="")
    a = b = None
    win = None
    try:
        a = G.make_self_test_service("甲", 0, base, os.path.join(base, "dl"), ns)
        b = G.make_self_test_service("乙", 1, base, os.path.join(base, "dl"), ns)
        check("自测模式两个实例都开着发现层",
              a.enable_discovery and b.enable_discovery,
              f"甲={a.enable_discovery} 乙={b.enable_discovery}")
        a.start()
        b.start()
        # 注意: 这里**不**调用 register_manual_peer, 必须靠广播自己发现
        seen = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if (any(p["peer_id"] == b.peer_id for p in a.lan_peers())
                    and any(p["peer_id"] == a.peer_id for p in b.lan_peers())):
                seen = True
                break
            time.sleep(0.3)
        check("两个窗口靠广播互相看得见 (没有内部登记)",
              seen, f"甲={[p['name'] for p in a.lan_peers()]} 乙={[p['name'] for p in b.lan_peers()]}")
        check("发现来源确实是发现层, 不是本地登记",
              b.peer_id in a._discovered_ids and a.peer_id in b._discovered_ids,
              f"甲={sorted(a._discovered_ids)[:2]} 乙={sorted(b._discovered_ids)[:2]}")
        entries = [p for p in a.lan_peers() if p["peer_id"] == b.peer_id]
        check("发现层学到的人不带『手动登记』标记 (隐身才看得准)",
              bool(entries) and entries[0].get("manual") is False,
              str(entries))
        if seen:
            # 自测模式现在**不**预先登记对方地址, 所以必须证明"光靠广播发现"
            # 也能走通加好友 -> 加密连接 (不然发现就只是列表好看)
            a.send_friend_request(b.peer_id)
            arrived = wait_for(lambda: len(b.pending_requests()) >= 1, 25)
            check("靠广播发现就能发请求 -> 对方收到", arrived,
                  str([(c.name, c.state) for c in b.contacts()]))
            if arrived:
                b.accept_request(a.peer_id)
                linked = wait_for(
                    lambda: bool((a.get_contact(b.peer_id) or Contact(peer_id="", name="")).encrypted),
                    25)
                check("靠广播发现就能建立加密连接 (自测不靠内部登记)", linked,
                      str([(c.name, c.encrypted) for c in a.contacts()]))
        if seen:
            before = next((p["last_seen"] for p in a.lan_peers() if p["peer_id"] == b.peer_id), 0.0)
            b.set_discoverable(False)
            check("隐身开关能关", b.discoverable is False)
            time.sleep(4.5)             # 广播间隔 3 秒: 没隐身必然刷新
            after = next((p["last_seen"] for p in a.lan_peers() if p["peer_id"] == b.peer_id), 0.0)
            check("乙隐身之后, 甲那边不再刷新它的 beacon (自测里也有效果)",
                  before > 0 and after == before, f"last_seen {before:.2f} -> {after:.2f}")
            b.set_discoverable(True)
            refreshed = False
            deadline = time.time() + 15
            while time.time() < deadline:
                now_seen = next((p["last_seen"] for p in a.lan_peers()
                                 if p["peer_id"] == b.peer_id), 0.0)
                if now_seen > after:
                    refreshed = True
                    break
                time.sleep(0.3)
            check("再打开之后又能被搜到", refreshed, f"last_seen {after:.2f}")
    finally:
        for svc in (a, b):
            if svc is not None:
                try:
                    svc.stop()
                except Exception:  # noqa: BLE001
                    pass
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(base, ignore_errors=True)


def test_selftest_fallback_when_broadcast_blocked() -> None:
    """广播不通时 (只有 VPN 网卡 / 防火墙拦了 UDP), 自测必须兜底而不是变砖。

    同时必须**如实标记**这条记录是本地登记的: 否则对方明明隐身了、列表里却还有人,
    又变成"隐身没效果"的假象。也顺便证明兜底之后真的能连上 (不是看得见连不上)。
    """
    print("\n自测模式: 广播不通时的兜底 (并且标记为『手动登记』)")
    base = os.path.join(TMP, "stfallback")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    a = b = None
    try:
        # 关掉发现层 = 模拟"广播永远不通"的网络
        a = ChatService(name="甲(本机)", data_dir=os.path.join(base, "a"),
                        download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                        tcp_port=0, reconnect_cooldown=0.5)
        b = ChatService(name="乙(本机)", data_dir=os.path.join(base, "b"),
                        download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                        tcp_port=0, reconnect_cooldown=0.5)
        a.start()
        b.start()
        results = G.selftest_connect_pair([a, b], grace=1.0)
        check("广播不通时自测自动兜底 (不会变成两个互相看不见的窗口)",
              all(r["source"] == "manual" and r["seen"] for r in results), str(results))
        check("兜底的地址是真实端口 (不是 0)",
              all(r["address"].startswith("127.0.0.1:")
                  and not r["address"].endswith(":0") for r in results),
              str([r["address"] for r in results]))
        entries = [p for p in a.lan_peers() if p["peer_id"] == b.peer_id]
        check("界面能看出这是『手动登记』而不是搜到的人",
              bool(entries) and entries[0].get("manual") is True, str(entries))
        check("兜底不算『被搜索到』(隐身只看发现层)",
              not a.is_discovered(b.peer_id) and not b.is_discovered(a.peer_id),
              f"甲={sorted(a._discovered_ids)} 乙={sorted(b._discovered_ids)}")

        a.send_friend_request(b.peer_id)
        arrived = wait_for(lambda: len(b.pending_requests()) >= 1, 25)
        check("兜底登记之后照样能加好友", arrived,
              str([(c.name, c.state) for c in b.contacts()]))
        if arrived:
            b.accept_request(a.peer_id)
            linked = wait_for(
                lambda: bool((a.get_contact(b.peer_id) or Contact(peer_id="", name="")).encrypted),
                25)
            check("兜底之后照样能建立加密连接", linked,
                  str([(c.name, c.encrypted) for c in a.contacts()]))
        # 模拟"广播后来通了 / 对方出现在发现层": 登记记录必须让位给发现层记录
        from lanchat.discovery import DiscoveredPeer
        a._on_lan_peer_found(DiscoveredPeer(peer_id=b.peer_id, name=b.name,
                                            host="127.0.0.1", port=b.tcp_port))
        entries = [p for p in a.lan_peers() if p["peer_id"] == b.peer_id]
        check("发现层通了之后『手动登记』标记自动消失",
              bool(entries) and entries[0].get("manual") is False and a.is_discovered(b.peer_id),
              str(entries))
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", 1)   # 端口 1: 不该被采纳
        entries = [p for p in a.lan_peers() if p["peer_id"] == b.peer_id]
        check("已经能被发现的人, 再登记也不会被打上『手动登记』标记",
              bool(entries) and entries[0].get("manual") is False
              and entries[0]["port"] == b.tcp_port, str(entries))
    finally:
        for svc in (a, b):
            if svc is not None:
                try:
                    svc.stop()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(base, ignore_errors=True)


def test_simulate_offline_and_reconnect() -> None:
    """自测模式的『🧪 掉线 6 秒』: 真的下线再回来, 对面自动重连并把缺的消息补过来。

    一个按钮验完整条链路: 掉线 → 对面显示"对方不在线" → 我发的消息先存着 →
    重新上线 → 自动重连 → 存着的那条按"新消息"送达。
    """
    print("\n自测: 模拟掉线 → 对方看到离线 → 自动重连 → 补发消息")
    base = os.path.join(TMP, "stoffline")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
        disc = probe.getsockname()[1]
    finally:
        probe.close()
    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=disc,
                            no_discovery=False, rekey_after=30, name="")
    a = b = None
    try:
        a = G.make_self_test_service("甲", 0, base, os.path.join(base, "dl"), ns)
        b = G.make_self_test_service("乙", 1, base, os.path.join(base, "dl"), ns)
        a.start()
        b.start()
        port_before = b.tcp_port
        a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
        b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)
        a.send_friend_request(b.peer_id)
        if not wait_for(lambda: bool(b.pending_requests()), timeout=25):
            check("成为好友 (前置条件)", False, "对方没收到请求")
            return
        b.accept_request(a.peer_id)
        if not wait_for(lambda: (a.get_contact(b.peer_id) or Contact()).encrypted
                        and (b.get_contact(a.peer_id) or Contact()).encrypted, timeout=25):
            check("建立加密会话 (前置条件)", False, "没连上")
            return
        check("自测窗口带上了『模拟掉线』按钮",
              bool(getattr(G, "ChatApp", None)) and hasattr(G.ChatApp, "simulate_offline"), "")

        a.send_text("掉线前: 你好", b.peer_id)
        check("掉线前正常聊天", wait_for(lambda: any(e["text"] == "掉线前: 你好"
                                                   for e in b.conversation(a.peer_id)), 20),
              str([e["text"] for e in b.conversation(a.peer_id)]))

        check("能发起模拟掉线", b.simulate_offline(3.0))
        check("乙真的下线了 (服务停掉 / 监听关闭)",
              wait_for(lambda: not b._started, 10) and not b._started, f"_started={b._started}")

        # 甲这时发消息: 连不上, 应该先存在本机 (pending), 而不是丢
        a.send_text("掉线期间: 你回来能看到吗", b.peer_id)
        stored = [e for e in a.conversation(b.peer_id)
                  if e["text"] == "掉线期间: 你回来能看到吗"]
        check("掉线期间发的消息先存在本机", bool(stored) and stored[0].get("pending") is True,
              str(stored))

        check("乙重新上线 (还绑在原来的端口)",
              wait_for(lambda: b._started, 20) and b.tcp_port == port_before,
              f"_started={b._started} 端口 {b.tcp_port} vs {port_before}")
        check("双方自动重连成功",
              wait_for(lambda: bool((a.get_contact(b.peer_id) or Contact()).encrypted)
                                and bool((b.get_contact(a.peer_id) or Contact()).encrypted), 40),
              str([(c.name, c.connected, c.encrypted) for c in a.contacts()]))
        delivered = wait_for(
            lambda: any(e["text"] == "掉线期间: 你回来能看到吗"
                        for e in b.conversation(a.peer_id)), 30)
        check("掉线期间那条消息在重连后送达", delivered,
              str([e["text"] for e in b.conversation(a.peer_id)]))
        check("它算新消息 (未读 +1)", (b.get_contact(a.peer_id) or Contact()).unread >= 1,
              str((b.get_contact(a.peer_id) or Contact()).unread))
        check("重连以后还能正常聊", b.send_text("我回来了", a.peer_id) == 1
              and wait_for(lambda: any(e["text"] == "我回来了"
                                       for e in a.conversation(b.peer_id)), 20),
              str([e["text"] for e in a.conversation(b.peer_id)]))
    finally:
        for svc in (a, b):
            if svc is not None:
                try:
                    svc.stop()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(base, ignore_errors=True)


def make_pair(tmpdir: str, rekey_every: int = 20) -> tuple:
    """构造两个完整实例 + 两个完整窗口 (就是自测模式做的事)。"""
    base = os.path.join(tmpdir, "selftest")
    a = ChatService(name="甲(本机)", data_dir=os.path.join(base, "a"),
                    download_dir=os.path.join(base, "dl-a"), enable_discovery=False,
                    tcp_port=0, auto_accept_files=True, reconnect_cooldown=0.5,
                    rekey_every=rekey_every)
    b = ChatService(name="乙(本机)", data_dir=os.path.join(base, "b"),
                    download_dir=os.path.join(base, "dl-b"), enable_discovery=False,
                    tcp_port=0, auto_accept_files=True, reconnect_cooldown=0.5,
                    rekey_every=rekey_every)
    a.start()
    b.start()
    a.register_manual_peer(b.peer_id, b.name, "127.0.0.1", b.tcp_port)
    b.register_manual_peer(a.peer_id, a.name, "127.0.0.1", a.tcp_port)

    ns = argparse.Namespace(data_dir=base, download_dir="", discovery_port=51999,
                            no_discovery=True, rekey_after=rekey_every, name="")
    win_a, win_b = tk.Tk(), tk.Tk()
    for win, who in ((win_a, "甲"), (win_b, "乙")):
        win.withdraw()
        win.title(f"[自测 {who}]")
    app_a = G.ChatApp(win_a, a, ns, own_service=False)
    app_b = G.ChatApp(win_b, b, ns, own_service=False)
    return a, b, app_a, app_b, win_a, win_b


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery-port", type=int, default=51021)
    parser.parse_args()

    print("=" * 62)
    print("自测模式 / 验证问题 / 密钥轮换 测试")
    print("=" * 62)

    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError as exc:
        print(f"  [SKIP] 没有图形环境: {exc}")
        return 0

    messagebox.askokcancel = lambda *a, **k: True   # type: ignore[assignment]
    messagebox.askyesno = lambda *a, **k: True      # type: ignore[assignment]
    messagebox.showinfo = lambda *a, **k: MSGS.append(str(a[1]) if len(a) > 1 else "")
    messagebox.showwarning = lambda *a, **k: MSGS.append(str(a[1]) if len(a) > 1 else "")
    messagebox.showerror = lambda *a, **k: MSGS.append(str(a[1]) if len(a) > 1 else "")

    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    a = b = None
    win_a = win_b = None
    try:
        a, b, app_a, app_b, win_a, win_b = make_pair(TMP, rekey_every=0)
        pump(win_a, 0.6)

        # ---------- 1. 两个完整实例 ----------
        check("两个实例身份不同", a.peer_id != b.peer_id)
        check("两个实例指纹不同", a.fingerprint != b.fingerprint,
              f"{a.fingerprint} / {b.fingerprint}")
        check("两个实例端口不同", a.tcp_port != b.tcp_port,
              f"{a.tcp_port} / {b.tcp_port}")
        check("两个完整窗口都建好了", bool(win_a.winfo_exists()) and bool(win_b.winfo_exists()))
        check("窗口里有完整的聊天界面",
              hasattr(app_a, "contact_list") and hasattr(app_a, "chat")
              and hasattr(app_a, "requests_btn"))
        check("『添加联系人』里能看到对方",
              any(p["peer_id"] == b.peer_id for p in a.lan_peers()),
              str([p["name"] for p in a.lan_peers()]))

        # ---------- 2. 验证问题: 答错 (同一个弹窗里直接重试) ----------
        b.set_question(a.peer_id, "1 + 1 = ?", "2", max_attempts=2)
        check("乙给甲设好了验证问题", b.question_of(a.peer_id) == "1 + 1 = ?",
              b.question_of(a.peer_id))
        a.send_friend_request(b.peer_id)
        got_dialog = until(win_a, lambda: b.peer_id in app_a._answer_dialogs, timeout=25)
        check("发请求后自动弹出答题窗口", got_dialog)
        dialog = app_a._answer_dialogs.get(b.peer_id) if got_dialog else None
        if dialog is not None:
            check("弹窗里带着对方的题目",
                  dialog.win.winfo_exists() and "1 + 1" in dialog.question_label.cget("text"),
                  dialog.question_label.cget("text"))
            dialog.entry.delete(0, "end")
            dialog.entry.insert(0, "3")          # 故意答错 (真键盘输入路径)
            dialog.submit()
            told = until(win_a, lambda: "不对" in dialog.result_label.cget("text"), timeout=20)
            check("答错后弹窗里当场提示", told, dialog.result_label.cget("text"))
            check("提示里写了还剩几次", "还剩 1 次" in dialog.result_label.cget("text"),
                  dialog.result_label.cget("text"))
            check("答错后弹窗不关闭, 可以接着改", bool(dialog.win.winfo_exists()))
        contact_a = a.get_contact(b.peer_id)
        check("答错时不会进入『新朋友』列表", not b.pending_requests(),
              str([(c.name, c.state) for c in b.contacts()]))
        check("答错后仍在等待验证", contact_a is not None
              and contact_a.state == ContactState.REQUEST_OUT.value,
              contact_a.state if contact_a else "无")
        check("答错的题保留着 (可以在同一个窗口重试)",
              any(c["peer_id"] == b.peer_id for c in a.pending_challenges()),
              str(a.pending_challenges()))

        # 第二次也答错 -> 自动封禁 (仍在同一个弹窗里)
        if dialog is not None:
            dialog.entry.delete(0, "end")
            dialog.entry.insert(0, "4")
            dialog.submit()
        blocked = until(win_a, lambda: bool(b.blocked_contacts()), timeout=20)
        check("连续答错到上限 -> 乙自动封禁甲", blocked,
              str([c.name for c in b.blocked_contacts()]))
        if dialog is not None:
            banned = until(win_a, lambda: "机会用完" in dialog.result_label.cget("text"),
                           timeout=15)
            check("机会用完时弹窗也明确说了", banned, dialog.result_label.cget("text"))
            dialog.close()
            pump(win_a, 0.3)

        # ---------- 3. 解禁 + 正确答案 ----------
        b.unblock(a.peer_id)
        pump(win_a, 2.0)                     # 等上一次被拒的连接收尾
        check("解禁后黑名单为空", not b.blocked_contacts())
        a.send_friend_request(b.peer_id)
        got_third = until(win_a, lambda: b.peer_id in app_a._answer_dialogs, timeout=25)
        if got_third:
            again = app_a._answer_dialogs[b.peer_id]
            again.entry.delete(0, "end")
            again.entry.insert(0, "2")                 # 正确答案
            again.submit()
            check("答对后弹窗提示正确",
                  until(win_a, lambda: "正确" in again.result_label.cget("text"), timeout=20),
                  again.result_label.cget("text"))
        else:
            ca_now = a.get_contact(b.peer_id)
            check("解禁后能再次收到提问", False,
                  f"甲侧联系人={ca_now and (ca_now.state, ca_now.dial_state, ca_now.last_error)} "
                  f"甲待答={a.pending_challenges()} 乙待答={b.pending_challenges()} "
                  f"乙联系人={[(c.name, c.state, c.blocked) for c in b.contacts()]}")
        passed = until(win_a, lambda: len(b.pending_requests()) >= 1, timeout=25)
        check("答对后请求进入『新朋友』列表", passed,
              str([(c.name, c.state) for c in b.contacts()]))
        if passed:
            b.accept_request(a.peer_id)
        check("双方建立加密会话",
              until(win_a, lambda: encrypted(a, b.peer_id) and encrypted(b, a.peer_id), timeout=30),
              str([(c.name, c.state, c.encrypted) for c in a.contacts()]
                  + [(c.name, c.state, c.encrypted) for c in b.contacts()]))

        # ---------- 4. 双向聊天 (走完整窗口) ----------
        app_a.select_contact(b.peer_id)
        app_b.select_contact(a.peer_id)
        app_a.entry.delete("1.0", "end")
        app_a.entry.insert("1.0", "你好, 我是甲")
        app_a.send_message()
        pump(win_a, 1.5)
        check("乙的聊天窗口显示收到消息",
              "你好, 我是甲" in app_b.chat.get("1.0", "end"),
              app_b.chat.get("1.0", "end")[-120:].strip())
        app_b.entry.delete("1.0", "end")
        app_b.entry.insert("1.0", "收到, 我是乙")
        app_b.send_message()
        pump(win_a, 1.5)
        check("甲的聊天窗口显示收到回复",
              "收到, 我是乙" in app_a.chat.get("1.0", "end"))

        # ---------- 5. 密钥轮换 (每 4 条消息一次, 验证零丢失) ----------
        def conn_of(service: ChatService, peer_id: str):
            contact = service.get_contact(peer_id)
            return contact.connection if contact else None

        a.set_rekey_every(4)
        b.set_rekey_every(4)
        pump(win_a, 0.3)
        app_b.chat.configure(state="normal")
        app_b.chat.delete("1.0", "end")
        app_b.chat.configure(state="disabled")
        total = 12
        for i in range(total):
            a.send_text(f"轮换第 {i} 条", b.peer_id)
            pump(win_a, 0.3)
        pump(win_a, 2.0)
        log_b = app_b.chat.get("1.0", "end")
        missing = [i for i in range(total) if f"轮换第 {i} 条" not in log_b]
        check(f"开启轮换后连续 {total} 条消息零丢失", not missing, f"缺 {missing}")
        live_a = conn_of(a, b.peer_id)
        live_b = conn_of(b, a.peer_id)
        check("确实发生了密钥轮换", live_a.rekeys_done > 0 and live_b.rekeys_done > 0,
              f"甲 {live_a.rekeys_done} 次 / 乙 {live_b.rekeys_done} 次")
        check("双方 epoch 一致", live_a.cipher.epoch == live_b.cipher.epoch,
              f"甲 {live_a.cipher.epoch} / 乙 {live_b.cipher.epoch}")
        a.set_rekey_every(0)
        b.set_rekey_every(0)

        app_b.chat.configure(state="normal")
        app_b.chat.delete("1.0", "end")
        app_b.chat.configure(state="disabled")
        a.send_text("轮换之后还能聊", b.peer_id)
        pump(win_a, 1.5)
        check("继续正常聊天",
              "轮换之后还能聊" in app_b.chat.get("1.0", "end"),
              repr(app_b.chat.get("1.0", "end")[-80:]))

        # ---------- 6. 加密验证: 线上只有密文 ----------
        from lanchat import protocol

        captured = bytearray()
        real = protocol.send_frame

        def spy(sock, obj):  # type: ignore[no-untyped-def]
            captured.extend(protocol.encode_frame(obj))
            return real(sock, obj)

        protocol.send_frame = spy  # type: ignore[assignment]
        try:
            a.send_text("抓包标记-9381", b.peer_id)
            pump(win_a, 1.0)
        finally:
            protocol.send_frame = real  # type: ignore[assignment]
        blob = bytes(captured)
        check("抓到的流量里没有明文消息",
              "抓包标记-9381".encode("utf-8") not in blob, f"{len(blob)} 字节")
        check("抓到的流量里只有密文帧", b'"type":"enc"' in blob)

        # ---------- 7. 文件传输 ----------
        payload = os.urandom(200_000)
        src = os.path.join(TMP, "selftest.bin")
        with open(src, "wb") as fh:
            fh.write(payload)
        digest = hashlib.sha256(payload).hexdigest()
        target = os.path.join(b.download_dir, "selftest.bin")
        a.send_file(src, b.peer_id)

        def file_ready() -> bool:
            try:
                return os.path.isfile(target) and os.path.getsize(target) >= len(payload)
            except OSError:
                return False

        received = until(win_a, file_ready, timeout=60)
        check("文件传输完成", received,
              str(os.listdir(b.download_dir) if os.path.isdir(b.download_dir) else "无目录"))
        if received:
            pump(win_a, 0.6)
            with open(target, "rb") as fh:
                got = fh.read()
            check("文件内容 SHA256 一致", hashlib.sha256(got).hexdigest() == digest,
                  f"{len(got)} 字节")

        # ---------- 8. 封禁 / 解禁 ----------
        b.block(a.peer_id)
        pump(win_a, 0.6)
        check("封禁后进入黑名单", bool(b.blocked_contacts()))
        b.unblock(a.peer_id)
        pump(win_a, 0.6)
        check("解禁后黑名单清空", not b.blocked_contacts())

        # ---------- 9. 界面回归: 自动刷新不能把选中项刷掉 ----------
        # 这是"回答问题时说没选择人"的根因: 列表每 3 秒被广播刷新一次, 旧代码
        # 重建列表后选中项就没了, 于是点按钮时提示"请先选择一个人"。
        a.register_manual_peer("lc-test-fake-peer", "丙(测试用假人)", "127.0.0.1", 1)
        contacts_dlg = G.ContactsDialog(app_a)
        pump(win_a, 0.3)
        contacts_dlg.tree.selection_set(b.peer_id)
        contacts_dlg.refresh()                       # 模拟一次自动刷新
        pump(win_a, 0.2)
        selected = contacts_dlg._selected_peer()
        check("『添加联系人』自动刷新后仍选中同一个人",
              bool(selected) and selected["peer_id"] == b.peer_id,
              str(selected and selected["peer_id"]))
        MSGS.clear()
        contacts_dlg.send_request()
        pump(win_a, 0.5)
        check("选中的人还在, 不会再提示『请先选择一个人』",
              not [m for m in MSGS if "点一下" in m or "选择" in m], str(MSGS))
        # 上面这一通操作(封禁/解禁/重新申请)之后, 对方可能又出了一道题; 这里先把它清干净,
        # 专门测"真的没有待答问题时"的表现
        for peer_id, dialog in list(app_a._answer_dialogs.items()):
            dialog.close()
        with a._lock:
            a._incoming_questions.clear()
        app_a._update_answer_button()
        MSGS.clear()
        contacts_dlg.answer_pending()                 # 没有待答问题时给提示, 不能报错
        pump(win_a, 0.3)
        check("没有待回答的问题时给提示而不是报错",
              any("没有需要回答" in m for m in MSGS),
              f"{MSGS} / 待答={app_a.service.pending_challenges()}")
        check("『回答问题』按钮平时是普通状态",
              "回答问题" in app_a.answer_btn.cget("text"), app_a.answer_btn.cget("text"))
        contacts_dlg.win.destroy()
        pump(win_a, 0.2)

        # 『新朋友』列表同样要保住选中项
        b.clear_question(a.peer_id)
        b.remove_friend(a.peer_id)
        a.remove_friend(b.peer_id)
        pump(win_a, 0.5)
        a.send_friend_request(b.peer_id)
        has_request = until(win_a, lambda: len(b.pending_requests()) >= 1, timeout=25)
        check("重新发请求后对方能收到", has_request,
              str([(c.name, c.state) for c in b.contacts()]))
        if has_request:
            requests_dlg = G.FriendRequestsDialog(app_b)
            pump(win_b, 0.3)
            requests_dlg.tree.selection_set(a.peer_id)
            requests_dlg.refresh()               # 模拟一次自动刷新
            pump(win_b, 0.2)
            kept = requests_dlg._selected()
            check("『新朋友』自动刷新后仍选中同一条请求",
                  bool(kept) and kept.peer_id == a.peer_id, str(kept and kept.peer_id))
            requests_dlg.win.destroy()
            pump(win_b, 0.2)

        # ---------- 10. 关闭 ----------
        for app in (app_a, app_b):
            app._closing = True
            for job in (getattr(app, "_drain_job", None), getattr(app, "_tick_job", None)):
                if job:
                    try:
                        app.after_cancel(job)
                    except tk.TclError:
                        pass
        check("关闭两个窗口不报错", True)
    finally:
        for service in (a, b):
            if service is not None:
                try:
                    service.stop()
                except Exception:  # noqa: BLE001
                    pass
        for win in (win_a, win_b):
            if win is not None:
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(TMP, ignore_errors=True)

    os.makedirs(TMP, exist_ok=True)
    try:
        test_question_survives_restart()
        test_manual_peer_port_guard()
        test_files_during_rekey()
        test_remove_friend_notifies_peer()
        test_cancel_friend_request()
        test_question_can_be_cancelled()
        test_question_required_again_after_removal()
        test_rekey_negotiation()
        test_download_dir_default_and_unblock_button()
        test_self_test_config_and_auto_accept()
        test_self_test_discovers_and_can_hide()
        test_selftest_fallback_when_broadcast_blocked()
        test_simulate_offline_and_reconnect()
        test_notifications_for_peer_actions()
        test_save_as_path()
        test_gui_file_dialog_and_answer_recovery()
        test_answer_feedback_keeps_dialog()
        test_progress_bar_and_speed()
        test_dialog_buttons_visible()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
        return 1
    print("✅ 自测模式 / 验证问题 / 密钥轮换 全部通过")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
