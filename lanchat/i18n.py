"""多语言支持 (i18n)。

设计取舍 —— 为什么用"中文原文当 key"而不是 `gui.send_button` 这种符号 key:

1. 这个程序的界面文案是**边写边改**的, 符号 key 一改文案就得同步改 key, 一旦忘记同步
   就会出现"界面上显示 gui.send_button"这种事故; 用原文当 key, 漏翻最多是显示中文。
2. 中文是源语言: 词条缺失时 `t()` 原样返回中文, 所以**任何翻译缺失都不会让程序坏掉**,
   最差情况只是"这版还没翻到"。
3. 代码里 `t("发送")` 一眼能看出这行显示什么, 不用来回跳文件查 key。

代价: 改了中文原文就等于改了 key, 旧翻译会失效 (退回中文)。因此**改文案时要同步改
locales/*.py 里的 key**, `python tests/test_i18n.py` 会报出"目录里有 key 已经不存在于代码中"。

占位符: `t()` 本身**不做格式化**, 需要插值的地方写成 `t("共 {0} 条").format(n)`。
这样翻译里出现花括号 (比如帮助文本里的 JSON 示例) 不会意外触发格式化, 也就不会抛异常。
"""

from __future__ import annotations

import importlib
import locale
import os
import sys
from typing import Dict, List, Optional

# 源语言: key 就是简体中文原文, 没有对应目录
SOURCE_LANGUAGE = "zh-Hans"

# 语言代码 -> (母语名, 英文名, 界面字体候选)
LANGUAGES: Dict[str, Dict[str, object]] = {
    "zh-Hans": {"native": "简体中文", "english": "Chinese (Simplified)",
                "fonts": ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei")},
    "zh-Hant": {"native": "繁體中文", "english": "Chinese (Traditional)",
                "fonts": ("Microsoft JhengHei UI", "Microsoft JhengHei", "MingLiU")},
    "en": {"native": "English", "english": "English",
           "fonts": ("Segoe UI", "Tahoma", "Arial")},
}

DEFAULT_LANGUAGE = SOURCE_LANGUAGE

# 语言代码 -> 词条模块。**写死成表**, 不用拼字符串动态导入:
# PyInstaller 静态分析看不到拼出来的模块名, 打包后会 ModuleNotFoundError。
CATALOG_MODULES = {
    "zh-Hant": "lanchat.locales.zh_hant",
    "en": "lanchat.locales.en",
}

_ENV_OVERRIDE = "LANCHAT_LANG"

_current = DEFAULT_LANGUAGE
_catalog: Dict[str, str] = {}
_loaded_for: Optional[str] = None


# ---------------------------------------------------------------------------
# 语言代码归一化
# ---------------------------------------------------------------------------
def normalize(code: str) -> str:
    """把各种写法收敛到 LANGUAGES 里的三个代码之一。

    `zh-TW` / `zh-HK` / `zh-Hant-TW` / `zh_HK` 都算繁體; 认不出来的一律回落到源语言,
    宁可显示中文也不要显示一堆 key。
    """
    text = (code or "").strip().replace("_", "-").lower()
    if not text:
        return DEFAULT_LANGUAGE
    if text in ("zh-hans", "zh-cn", "zh-sg", "zh", "chs"):
        return "zh-Hans"
    if text.startswith("zh-hant") or text in ("zh-tw", "zh-hk", "zh-mo", "cht"):
        return "zh-Hant"
    if text.startswith("zh"):
        # zh-XX: 只有明确是港澳台才算繁體, 其余当简体
        return "zh-Hant" if text.split("-")[-1] in ("tw", "hk", "mo") else "zh-Hans"
    if text.startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def available() -> List[str]:
    """可切换的语言代码, 顺序固定 (界面下拉框直接用这个顺序)。"""
    return list(LANGUAGES.keys())


def display_name(code: str) -> str:
    """下拉框里显示的名字: 母语名 + 英文名 (英语则只显示一次)。"""
    item = LANGUAGES.get(normalize(code)) or {}
    native = str(item.get("native", code))
    english = str(item.get("english", ""))
    return native if native == english else f"{native} / {english}"


# ---------------------------------------------------------------------------
# 系统语言探测
# ---------------------------------------------------------------------------
def _windows_ui_language() -> str:
    """Windows: 取"用户界面语言"的 LCID (比 locale 模块更贴近用户实际看到的语言)。

    0404/0C04/1404 = 台湾/香港/澳门繁体; 0804/1004 = 简体; 其他交给 normalize 判断。
    """
    try:
        import ctypes

        langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 非 Windows / 受限环境, 退回去用 locale
        return ""
    return {
        0x0404: "zh-TW", 0x0C04: "zh-HK", 0x1404: "zh-MO", 0x0804: "zh-CN", 0x1004: "zh-CN",
    }.get(langid, f"lcid-{langid:04x}" if langid else "")


def detect_system_language() -> str:
    """按 环境变量 -> Windows UI 语言 -> locale 的顺序猜, 猜不出就简体中文。"""
    override = os.environ.get(_ENV_OVERRIDE, "").strip()
    if override:
        return normalize(override)
    windows = _windows_ui_language()
    if windows and not windows.startswith("lcid-"):
        return normalize(windows)
    for getter in (lambda: locale.getlocale(locale.LC_MESSAGES)[0],
                   lambda: locale.getlocale()[0],
                   lambda: locale.getdefaultlocale()[0]):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            continue
        if value:
            return normalize(str(value))
    return DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# 当前语言与目录
# ---------------------------------------------------------------------------
def _ensure_catalog() -> None:
    global _catalog, _loaded_for
    if _loaded_for == _current:
        return
    module_name = CATALOG_MODULES.get(_current)
    if not module_name:
        _catalog = {}
        _loaded_for = _current
        return
    try:
        module = importlib.import_module(module_name)
        catalog = getattr(module, "CATALOG", {})
    except Exception as exc:  # noqa: BLE001 - 目录坏了也不能让程序起不来
        if os.environ.get("LANCHAT_I18N_DEBUG"):
            print(f"[i18n] 载入 {module_name} 失败: {exc!r}", file=sys.stderr)
        catalog = {}
    _catalog = {str(k): str(v) for k, v in dict(catalog).items()}
    _loaded_for = _current


def set_language(code: str) -> str:
    """切换语言, 返回归一化后的代码。"""
    global _current
    _current = normalize(code)
    _ensure_catalog()
    return _current


def current_language() -> str:
    return _current


def catalog() -> Dict[str, str]:
    """当前语言的词条 (仅供测试/检查用, 不要改它)。"""
    _ensure_catalog()
    return dict(_catalog)


def t(text: str) -> str:
    """翻译一条文案。找不到就原样返回 (源语言是中文, 所以永远有兜底)。"""
    if _current == SOURCE_LANGUAGE:
        return text
    _ensure_catalog()
    return _catalog.get(text, text)


# 让调用方少写几个字符, 同时保留一个更明确的名字
tr = t


def font_families() -> tuple:
    """当前语言的界面字体候选 (Tk 会挑第一个存在的)。"""
    item = LANGUAGES.get(_current) or {}
    return tuple(item.get("fonts", ("Sans",)))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 从 settings.json 里早期读取语言
# ---------------------------------------------------------------------------
def load_saved_language(data_dir: str) -> str:
    """读 settings.json 里的 language; 没有/坏了就返回空串 (交给调用方决定默认值)。"""
    import json

    try:
        with open(os.path.join(data_dir, "settings.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("language", "")
    return normalize(str(value)) if value else ""


def resolve_initial_language(cli_value: str, data_dir: str) -> str:
    """启动时的语言优先级: 命令行 --lang > 设置里存过的 > 环境变量/系统语言。"""
    if (cli_value or "").strip():
        return normalize(cli_value)
    saved = load_saved_language(data_dir)
    if saved:
        return saved
    return detect_system_language()
