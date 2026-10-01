"""语言词条目录。

每个模块导出 `CATALOG: dict[str, str]`:
    key   = 简体中文原文 (源码里 `t("...")` 的原样文本)
    value = 该语言的译文

词条由 `python tools/i18n_extract.py` 从源码里抽出, 缺失的 key 会退回中文显示。
"""
