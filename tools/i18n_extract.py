"""抽出源码里所有 `t("...")` 词条, 用于新增语言 / 补翻译。

用法:
    python tools/i18n_extract.py                     # 打印词条总数和统计
    python tools/i18n_extract.py --json keys.json    # 导出 {原文: {count, files}}
    python tools/i18n_extract.py --list              # 逐行打印原文 (喂给翻译用)
    python tools/i18n_extract.py --missing en        # 列出 en 目录里还缺哪几条

约定见 docs/开发文档.md 第 13 节: 中文原文就是词条 key, 所以**改了中文文案等于改了 key**,
旧译文会失效 (退回中文); 用 `--missing` 或 `tools/i18n_check.py` 就能发现。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lanchat.console import configure_console  # noqa: E402

configure_console()   # 官方 Python 控制台默认 GBK, 打印中文/符号前先切 UTF-8

# 界面文案可能出现在这些文件里 (新增文件记得加进来)
SOURCE_FILES = [
    "lanchat/gui.py", "lanchat/service.py", "lanchat/connection.py", "lanchat/crypto.py",
    "lanchat/identity.py", "lanchat/discovery.py", "lanchat/protocol.py",
    "lanchat/startup_log.py", "chat_gui.py",
]


def extract_keys() -> Dict[str, Dict]:
    """扫出所有 `t("字面量")` -> {原文: {"count": n, "files": [...]}}"""
    found: Dict[str, Dict] = {}
    for rel in SOURCE_FILES:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "t" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                item = found.setdefault(arg.value, {"count": 0, "files": []})
                item["count"] += 1
                if rel not in item["files"]:
                    item["files"].append(rel)
            elif isinstance(arg, ast.JoinedStr):
                # f-string 没被抽成 "模板".format(...) —— 这种词条没法翻译 (key 里带变量),
                # 属于写法问题, 直接报出来。
                found.setdefault("<未转成模板的 f-string>", {"count": 0, "files": []})
                found["<未转成模板的 f-string>"]["count"] += 1
    return found


def load_catalog(code: str) -> Dict[str, str]:
    """读 lanchat/locales/<code>.py 里的 CATALOG。"""
    name = code.replace("-", "_").lower()
    path = os.path.join(ROOT, "lanchat", "locales", f"{name}.py")
    if not os.path.exists(path):
        return {}
    namespace: Dict = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), namespace)  # noqa: S102
    return {str(k): str(v) for k, v in dict(namespace.get("CATALOG", {})).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="抽出 / 检查界面词条")
    parser.add_argument("--json", metavar="路径", help="把词条表写成 JSON")
    parser.add_argument("--list", action="store_true", help="逐行打印原文 (给翻译用)")
    parser.add_argument("--missing", metavar="语言代码", help="列出该语言目录里缺的词条")
    args = parser.parse_args()

    keys = extract_keys()
    bad = keys.pop("<未转成模板的 f-string>", None)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(keys, handle, ensure_ascii=False, indent=1)
        print(f"已写入 {args.json}")
    if args.list:
        for key in sorted(keys):
            print(key.replace("\n", "\\n"))
    if args.missing:
        catalog = load_catalog(args.missing)
        missing = sorted(k for k in keys if k not in catalog)
        print(f"{args.missing}: 共 {len(keys)} 条, 缺 {len(missing)} 条")
        for key in missing:
            print("  " + key.replace("\n", "\\n"))
    if not (args.json or args.list or args.missing):
        print(f"词条总数: {len(keys)}")
        for rel in SOURCE_FILES:
            count = sum(1 for item in keys.values() if rel in item["files"])
            if count:
                print(f"  {count:4} 条  {rel}")
        for code in ("en", "zh_hant"):
            catalog = load_catalog(code)
            missing = [k for k in keys if k not in catalog]
            print(f"  {code}: 目录 {len(catalog)} 条, "
                  f"{'全部覆盖' if not missing else f'缺 {len(missing)} 条'}")

    if bad:
        print(f"\n[!] 有 {bad['count']} 处 f-string 没有写成 "
              f"t(\"模板\").format(...) 形式, 无法翻译: {bad['files']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
