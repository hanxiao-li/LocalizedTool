"""列解析辅助函数（languages.py）—— 项目语言配置驱动，全流程共用。

覆盖需求1/2/3 的核心：由所选语种确定源语言列/目标语言列、术语库源键列。
"""

from languages import find_lang_column, resolve_source_column, glossary_source_key


PROJECT_ZH = {
    'source_lang': 'zh',
    'source_col_name': '中文(简体)',
    'languages': [{'name': 'English', 'source_lang': 'zh'},
                  {'name': 'Deutsch', 'source_lang': 'en'}],
}
PROJECT_ROW = {  # 模拟 sqlite Row：无 'languages' 键
    'source_lang': 'zh',
    'source_col_name': '中文(简体)',
}


# ── find_lang_column：目标语种列 ────────────────────────────────────────

def test_find_lang_column_exact():
    cols = ['中文(简体)', 'English', '日本語', '备注']
    assert find_lang_column(cols, 'English') == 'English'
    assert find_lang_column(cols, '中文(简体)') == '中文(简体)'


def test_find_lang_column_alias():
    cols = ['中文', 'English', '备注']
    assert find_lang_column(cols, '中文(简体)') == '中文'
    assert find_lang_column(cols, '简体中文') == '中文'
    assert find_lang_column(['英文', '中文(简体)'], 'English') == '英文'


def test_find_lang_column_missing():
    assert find_lang_column(['中文(简体)', 'English'], 'Deutsch') is None
    assert find_lang_column([], 'English') is None


# ── resolve_source_column：按源语言找源列 ────────────────────────────────

def test_resolve_source_column_zh():
    # zh 源语种：项目源列存在 → 用项目源列
    assert resolve_source_column(['中文(简体)', 'English'], PROJECT_ZH, 'zh') == '中文(简体)'
    # 项目源列缺失但别名在 → 用别名列
    assert resolve_source_column(['中文', 'English'], PROJECT_ZH, 'zh') == '中文'
    # 无中文列 → None
    assert resolve_source_column(['English'], PROJECT_ZH, 'zh') is None


def test_resolve_source_column_en():
    assert resolve_source_column(
        ['中文(简体)', 'English', 'Deutsch'], PROJECT_ZH, 'en') == 'English'
    assert resolve_source_column(
        ['中文(简体)', '英文', 'Deutsch'], PROJECT_ZH, 'en') == '英文'
    assert resolve_source_column(['中文(简体)'], PROJECT_ZH, 'en') is None


# ── glossary_source_key：术语库源键列 ────────────────────────────────────

def test_glossary_source_key():
    # zh 源语种 → 项目源列（中文术语列）
    assert glossary_source_key(PROJECT_ZH, 'zh') == '中文(简体)'
    # en 源语种 → 项目语种中的 English 名
    assert glossary_source_key(PROJECT_ZH, 'en') == 'English'
    # Row 对象无 languages 键 → 兜底规范名
    assert glossary_source_key(PROJECT_ROW, 'zh') == '中文(简体)'
    assert glossary_source_key(PROJECT_ROW, 'en') == 'English'
