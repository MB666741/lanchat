"""检查所有语言词条目录是否完整、是否自相矛盾。

用法:
    python tools/i18n_check.py          # 全部检查, 有问题退出码 1

检查项:
  1. 源码里每个 t("...") 都能在每个语言目录里找到 (缺了就会退回中文, 界面上是"这里没翻");
  2. 目录里没有"源码里已经不存在"的僵尸词条 (改了中文文案却忘了删旧 key);
  3. `{0}` `{1}` 这类占位符在原文与译文里数量、编号一致 (错了会 IndexError 或显示不全);
  4. 英文目录里不该出现汉字;
  5. 繁體目录里不该出现简体字 (用一份"只在简体里出现"的字表, 不含 于/后/里/几 这类两边都用得到的字);
  6. 没有空译文。
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lanchat.console import configure_console  # noqa: E402

configure_console()   # Windows 控制台默认 GBK, 不切 UTF-8 打印特殊字符会直接崩

from i18n_extract import extract_keys, load_catalog  # noqa: E402

HAN = re.compile(r"[\u4e00-\u9fff]")
PLACEHOLDER = re.compile(r"\{\d+(?::[^}]*)?\}")
# 只在简体里出现的字 (刻意不含 于/后/里/几/只 这些繁体也用得到的)
SIMPLIFIED_ONLY = re.compile(
    r"[网设键联验读确让发开关单机际体数传输备录选应对话语态实资严么门东见长问间题点线连断错误还这个们时"
    r"为无与专业书条头务现额视观规则络软盘标页浏览]")


def main() -> int:
    keys = extract_keys()
    keys.pop("<未转成模板的 f-string>", None)
    problems = []

    for code in ("en", "zh_hant"):
        catalog = load_catalog(code)
        missing = sorted(k for k in keys if k not in catalog)
        zombie = sorted(k for k in catalog if k not in keys)
        if missing:
            problems.append(f"{code}: 缺 {len(missing)} 条词条, 例如 {missing[:3]}")
        if zombie:
            problems.append(f"{code}: 有 {len(zombie)} 条词条已不在源码里, 例如 {zombie[:3]}")

        for key, value in catalog.items():
            if not value.strip():
                problems.append(f"{code}: 空译文 {key[:40]!r}")
            if sorted(PLACEHOLDER.findall(key)) != sorted(PLACEHOLDER.findall(value)):
                problems.append(f"{code}: 占位符不一致 {key[:40]!r} -> {value[:40]!r}")
            if code == "en" and HAN.search(value):
                problems.append(f"{code}: 译文里出现汉字 {value[:40]!r}")
            if code == "zh_hant" and SIMPLIFIED_ONLY.search(value):
                hit = "".join(sorted(set(SIMPLIFIED_ONLY.findall(value))))
                problems.append(f"{code}: 译文里出现简体字 [{hit}] {value[:40]!r}")
        print(f"{code}: 词条 {len(catalog)} 条, 源码词条 {len(keys)} 条")

    if problems:
        print(f"\n[FAIL] 发现 {len(problems)} 个问题:")
        for line in problems[:40]:
            print("   -", line)
        return 1
    print("\n[OK] 词条目录检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
