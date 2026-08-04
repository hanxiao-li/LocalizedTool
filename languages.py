"""规范化语言列表 —— 项目下拉、Excel 表头、自动判定共用的单一来源。

所有语种的「规范显示名」统一为这里的 name：项目源语言列名、目标语种名、
术语库表头、翻译输出列、校验自动判定都用同一套名字，保证全项目一致。
lang_code() 同时兼容常见别名（如『中文』→ 中文(简体)），旧文件/旧项目可继续用。
"""

LANGUAGES = [
    {'code': 'zh-cn', 'name': '中文(简体)'},
    {'code': 'zh-tw', 'name': '中文(繁体)'},
    {'code': 'en', 'name': 'English'},
    {'code': 'ja', 'name': '日本語'},
    {'code': 'ko', 'name': '한국어'},
    {'code': 'de', 'name': 'Deutsch'},
    {'code': 'fr', 'name': 'Français'},
    {'code': 'es', 'name': 'Español'},
    {'code': 'pt', 'name': 'Português'},
    {'code': 'ru', 'name': 'Русский'},
    {'code': 'tr', 'name': 'Türkçe'},
    {'code': 'it', 'name': 'Italiano'},
    {'code': 'nl', 'name': 'Nederlands'},
    {'code': 'pl', 'name': 'Polski'},
    {'code': 'sv', 'name': 'Svenska'},
    {'code': 'da', 'name': 'Dansk'},
    {'code': 'ar', 'name': 'العربية'},
    {'code': 'th', 'name': 'ไทย'},
    {'code': 'vi', 'name': 'Tiếng Việt'},
    {'code': 'id', 'name': 'Bahasa Indonesia'},
    {'code': 'ms', 'name': 'Bahasa Melayu'},
]

LANG_NAMES = [l['name'] for l in LANGUAGES]

_CODE_TO_NAME = {l['code']: l['name'] for l in LANGUAGES}
_NAME_TO_CODE = {l['name']: l['code'] for l in LANGUAGES}

# 常见别名（小写键），保证旧表头/旧项目兼容
_ALIASES = {
    '中文': 'zh-cn', '简体中文': 'zh-cn', '中文简体': 'zh-cn', 'chinese': 'zh-cn',
    'chinese simplified': 'zh-cn', 'chinese(simplified)': 'zh-cn',
    'chinese (simplified)': 'zh-cn', 'simplified chinese': 'zh-cn',
    '中文(繁体)': 'zh-tw', '繁體中文': 'zh-tw', 'chinese traditional': 'zh-tw',
    'chinese (traditional)': 'zh-tw',
    'english': 'en', 'english (us)': 'en', 'en-us': 'en', '英文': 'en',
    'english (uk)': 'en-gb', 'en-gb': 'en-gb',
    'japanese': 'ja', '日文': 'ja', 'korean': 'ko', '韩文': 'ko',
    'deutsch': 'de', 'german': 'de', '德文': 'de',
    'français': 'fr', 'french': 'fr', '法文': 'fr',
    'español': 'es', 'spanish': 'es', '西班牙文': 'es',
    'português': 'pt', 'portuguese': 'pt', '葡文': 'pt',
    'russian': 'ru', 'русский': 'ru', '俄文': 'ru',
    'türkçe': 'tr', 'turkish': 'tr', '土文': 'tr',
    'italiano': 'it', 'italian': 'it', '意文': 'it',
    'nederlands': 'nl', 'dutch': 'nl', '荷兰文': 'nl',
    'polski': 'pl', 'polish': 'pl', '波兰文': 'pl',
    'svenska': 'sv', 'swedish': 'sv', '瑞典文': 'sv',
    'dansk': 'da', 'danish': 'da', '丹麦文': 'da',
    'arabic': 'ar', 'العربية': 'ar', '阿文': 'ar',
    'thai': 'th', 'ไทย': 'th', '泰文': 'th',
    'tiếng việt': 'vi', 'vietnamese': 'vi', '越南文': 'vi',
    'bahasa indonesia': 'id', 'indonesian': 'id', '印尼文': 'id',
    'bahasa melayu': 'ms', 'malay': 'ms', '马来文': 'ms',
}


def known_lang_code(name: str) -> str | None:
    """显示名/别名 → 标准语言代码；无法识别返回 None。"""
    name = (name or '').strip()
    if not name:
        return None
    return _ALIASES.get(name.lower()) or _NAME_TO_CODE.get(name)


def lang_code(name: str) -> str:
    """显示名/别名 → 标准语言代码。未知返回 'en'。"""
    return known_lang_code(name) or 'en'


def lang_name(code: str) -> str:
    """标准代码 → 规范显示名。未知返回 code 本身。"""
    return _CODE_TO_NAME.get((code or '').lower(), code or '')


def base_code(code: str) -> str:
    return (code or '').lower().split('-')[0]


def source_lang_of(name: str) -> str:
    """由所选源语言列名推导 source_lang：中文系→zh，英文系→en，其他→语言基本码。"""
    base = base_code(lang_code(name))
    if base in ('zh', 'en'):
        return base
    return base


# ── 文件列解析（项目语言配置驱动，全流程共用）───────────────────────────

def find_lang_column(columns: list[str], name: str) -> str | None:
    """在上传表列名中找指定语种的列：精确名优先，其次按语言代码别名匹配。

    返回匹配到的列名，找不到返回 None。columns 为字符串列表（表头）。
    """
    if not columns:
        return None
    target_code = known_lang_code(name)
    # 1. 精确名（含 '中文' 等别名表内键）
    for col in columns:
        if str(col) == name:
            return col
    # 2. 规范显示名匹配（列名本身可能就是规范名，如 '中文(简体)'）
    canonical = lang_name(target_code) if target_code else None
    for col in columns:
        if canonical and str(col) == canonical:
            return col
    # 3. 语言代码别名匹配（大小写不敏感）
    if target_code:
        for col in columns:
            col_s = str(col)
            if col_s and known_lang_code(col_s) == target_code:
                return col_s
    return None


def resolve_source_column(columns: list[str], project, source_lang: str) -> str | None:
    """按语种的源语言（zh/en）在上传表中找源语言列。

    * source_lang == 项目源语言（固定 'zh'）→ 优先项目 source_col_name，
      否则按 zh-cn 别名匹配（'中文(简体)' / '中文' / '简体中文' ...）；
    * 否则（如 'en'）→ 找代码等于该 source_lang 的列（'English'/'英文'...）。

    返回列名或 None（找不到源列）。
    """
    if not columns:
        return None
    if (source_lang or '').lower() == (project['source_lang'] or '').lower():
        src_name = project['source_col_name']
        if src_name and src_name in [str(c) for c in columns]:
            return src_name
        # 项目源列未精确命中 → 按该语言代码找别名
        src_code = known_lang_code(src_name) if src_name else None
        if src_code:
            for col in columns:
                if known_lang_code(str(col)) == src_code:
                    return str(col)
        return None
    # 非项目源语言（en）：按语言代码匹配列
    return find_lang_column(columns, lang_name(source_lang) if source_lang else '')


def glossary_source_key(project, lang_source_lang: str) -> str:
    """术语库中作为「源术语列」的列名（术语一致性/翻译时用）。

    * 语种源语言 == 项目源语言（zh）→ 项目 source_col_name（中文术语列）；
    * 否则（en）→ 项目语种中代码等于该源语言的语种名（如 'English'），
      兜底用规范显示名（如 'English'）。

    project 可为 dict 或 sqlite3.Row（两者都支持下标访问）。
    """
    if (lang_source_lang or '').lower() == (project['source_lang'] or '').lower():
        return project['source_col_name']
    code = (lang_source_lang or '').lower()
    # 在项目语种名中找代码匹配的那个（如 en → 'English'）
    try:
        langs = project['languages']
    except (KeyError, IndexError, TypeError):
        langs = None
    if langs:
        for lname in langs:
            name = lname['name'] if isinstance(lname, dict) else str(lname)
            if known_lang_code(name or '') == code:
                return name
    return lang_name(code)
