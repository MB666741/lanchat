# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置: 局域网聊天工具 (lanchat)。

**文件夹版**打包 (onedir): 产出 dist/LanChat/ 整个目录, 双击里面的 LanChat.exe 即可,
启动是秒开的。发给别人时要把**整个文件夹**一起拷过去 (不能只拷 exe), 对方不需要装 Python。
构建 (用 packaging_env 里的 PyInstaller):
    packaging_env\\Scripts\\pyinstaller.exe lanchat.spec --noconfirm

说明:
  * console=False -> 不弹黑框; 想看 --diagnose 的输出可以用 `LanChat.exe --diagnose > out.txt`
    (在 cmd 里跑), 或者把下面的 console 改成 True 重新打包。
  * 想要"单文件版"(只有一个 exe, 好发但首次启动慢 1~3 秒, 见下面注释):
    把 EXE(...) 改成带上 a.binaries/a.zipfiles/a.datas, 并删掉 COLLECT。
"""

block_cipher = None

a = Analysis(
    ["chat_gui.py"],
    pathex=["."],
    binaries=[],
    datas=[("使用说明.txt", ".")],   # 打包时一起放进 dist/LanChat (给收到文件夹的人看)
    hiddenimports=["lanchat", "lanchat.gui", "lanchat.service", "lanchat.crypto",
                   "lanchat.connection", "lanchat.discovery", "lanchat.identity",
                   "lanchat.protocol", "lanchat.console", "lanchat.winfocus",
                   "lanchat.startup_log", "lanchat.constants", "lanchat.i18n",
                   # 词条目录是 importlib 按表动态导入的, PyInstaller 静态分析看不到 —— 必须列出来,
                   # 否则打包后一切到英文/繁体就 ModuleNotFoundError (界面直接退回中文)。
                   "lanchat.locales", "lanchat.locales.en", "lanchat.locales.zh_hant"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "numpy", "pytest", "unittest", "pydoc", "doctest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir: 依赖放到 COLLECT 出来的文件夹里
    name="LanChat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI 程序: 不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,
               upx_exclude=[], name="LanChat")

# PyInstaller 6 会把 datas 放进 _internal/, 收到文件夹的人第一眼看不到 —— 再往 exe
# 旁边放一份"使用说明.txt"(上面 datas 里也保留, 免得以后改回 5.x 就丢了)。
try:
    import shutil as _shutil
    _shutil.copyfile(os.path.join(SPECPATH, "使用说明.txt"),
                     os.path.join(DISTPATH, "LanChat", "使用说明.txt"))
except OSError:
    pass

# ---------------------------------------------------------------------------
# 想要"单文件版"(整个程序就一个 exe, 拷贝/发送最省事; 代价是每次启动都要把内容解压到
# %TEMP%, 首次会慢 1~3 秒, 而且在个别受限环境/杀软下可能被拦在解压那一步) 就把上面的
# EXE(...) 换成下面这段, 并删掉 COLLECT:
#
# exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name="LanChat",
#           debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
#           upx_exclude=[], runtime_tmpdir=None, console=False,
#           disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
#           codesign_identity=None, entitlements_file=None)
# ---------------------------------------------------------------------------
