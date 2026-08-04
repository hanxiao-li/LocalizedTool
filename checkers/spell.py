"""Spell check — offline multi-tier lookup:
1. Hunspell + english-words (fast, offline) — common words & inflections
2. Wiktionary offline word list (kaikki.org) — rare/game/compound terms
3. Only flag words rejected by ALL sources

Downloads Wiktionary word lists on first use per language (one-time, cached).
"""

import re
import os
import sys
import json
import logging
import difflib
from typing import Optional
import urllib.request
from .base import BaseChecker, CheckResult

try:
    import requests as _requests_lib
except ImportError:
    _requests_lib = None

logger = logging.getLogger(__name__)

# ── Language config ──────────────────────────────────────────────────────

CJK_LIMITED_SPELL = {'zh-cn', 'zh-tw', 'zh-hk', 'ja-jp', 'ko-kr'}

WOORM_LANG_MAP = {
    'en': 'en',
    'de': 'de', 'fr': 'fr', 'es': 'es', 'pt': 'pt',
    'ru': 'ru', 'it': 'it', 'nl': 'nl',
    'pl': 'pl', 'sv': 'sv', 'da': 'da',
    'ca': 'ca', 'uk': 'uk', 'ro': 'ro',
    'tr': 'tr', 'ar': 'ar', 'fa': 'fa',
    'vi': 'vi', 'id': 'id', 'ms': 'ms',
    'th': 'th', 'cs': 'cs', 'sk': 'sk',
    'hu': 'hu', 'fi': 'fi', 'nb': 'nb',
    'bg': 'bg', 'he': 'he', 'hr': 'hr',
    'sr': 'sr', 'sl': 'sl', 'et': 'et',
    'lv': 'lv', 'lt': 'lt', 'el': 'el',
}

# kaikki.org language name mapping (base_lang → kaikki language name)
KAIKKI_LANG_MAP = {
    'en': 'English', 'de': 'German', 'fr': 'French', 'es': 'Spanish',
    'pt': 'Portuguese', 'ru': 'Russian', 'it': 'Italian', 'nl': 'Dutch',
    'pl': 'Polish', 'sv': 'Swedish', 'da': 'Danish', 'ca': 'Catalan',
    'uk': 'Ukrainian', 'ro': 'Romanian', 'tr': 'Turkish', 'ar': 'Arabic',
    'fa': 'Persian', 'vi': 'Vietnamese', 'id': 'Indonesian', 'ms': 'Malay',
    'cs': 'Czech', 'sk': 'Slovak', 'hu': 'Hungarian', 'fi': 'Finnish',
    'nb': 'Norwegian', 'bg': 'Bulgarian', 'he': 'Hebrew', 'hr': 'Croatian',
    'sr': 'Serbian', 'sl': 'Slovenian', 'et': 'Estonian', 'lv': 'Latvian',
    'lt': 'Lithuanian', 'el': 'Greek', 'th': 'Thai',
}

def _get_dict_dir() -> str:
    """Find hunspell_dicts directory (works in dev and PyInstaller)."""
    # PyInstaller onedir: dictionaries are next to the exe
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
        d = os.path.join(base, 'hunspell_dicts')
        if os.path.isdir(d):
            return d
    # Fallback: relative to this file (dev mode)
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), 'hunspell_dicts')


DICT_DIR = _get_dict_dir()
WOORM_BASE_URL = 'https://raw.githubusercontent.com/wooorm/dictionaries/main/dictionaries'


def _download(url: str, local: str, timeout: int = 30) -> None:
    """Timeout-bounded download to a local file (never hangs forever)."""
    req = urllib.request.Request(url, headers={'User-Agent': 'LocalizedTool/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with open(local, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

# LibreOffice dictionaries — more comprehensive morphological rules than wooorm.
# Maps base_lang -> (dir_path, file_prefix)
# Paths verified from Dictionary_*.mk files in LibreOffice/dictionaries repo.
LIBREOFFICE_DICT_MAP = {
    'de': ('de', 'de_DE_frami'),
    'en': ('en', 'en_US'),
    'fr': ('fr_FR/dictionaries', 'fr'),
    'es': ('es', 'es_ES'),
    'pt': ('pt_BR', 'pt_BR'),
    'ru': ('ru_RU', 'ru_RU'),
    'it': ('it_IT', 'it_IT'),
    'nl': ('nl_NL', 'nl_NL'),
    'pl': ('pl_PL', 'pl_PL'),
    'sv': ('sv_SE/dictionaries', 'sv_SE'),
    'da': ('da_DK', 'da_DK'),
    'cs': ('cs_CZ', 'cs_CZ'),
    'sk': ('sk_SK', 'sk_SK'),
    'hu': ('hu_HU', 'hu_HU'),
    'ro': ('ro', 'ro_RO'),
    'bg': ('bg_BG', 'bg_BG'),
    'uk': ('uk_UA', 'uk_UA'),
    'tr': ('tr_TR', 'tr_TR'),
    'el': ('el_GR', 'el_GR'),
    'sl': ('sl_SI', 'sl_SI'),
    'hr': ('hr_HR', 'hr_HR'),
    'sr': ('sr', 'sr'),
    'lt': ('lt_LT', 'lt_LT'),
    'lv': ('lv_LV', 'lv_LV'),
    'et': ('et_EE', 'et_EE'),
    'he': ('he_IL', 'he_IL'),
    'ar': ('ar', 'ar'),
    'vi': ('vi', 'vi_VN'),
    'ca': ('ca', 'ca'),
    'nb': ('no', 'nb_NO'),
    'fa': ('fa_IR', 'fa_IR'),
    'th': ('th_TH', 'th_TH'),
    'id': ('id', 'id_ID'),
}
LIBREOFFICE_BASE = 'https://raw.githubusercontent.com/LibreOffice/dictionaries/master'
KAIKKI_URL = 'https://kaikki.org/dictionary/{lang}/kaikki.org-dictionary-{lang}.jsonl'

STRIP_PATTERN = re.compile(
    r'<[^>]+>|'
    r'\{[^}]*\}|'
    r'%[+\-]?[0-9.]*[sdifFgcboxXhHlLqQjJtTzZ@%]|'
    r'\$\{[^}]+\}|'
    r'%\([^)]+\)[sdifFgcboxX]|'
    r'\[\[?[A-Za-z_][A-Za-z0-9_]*\]\]?|'
    r'&#\d+;|&#x[0-9A-Fa-f]+;|&[a-z]+;'
)

WORD_RE = re.compile(r'[^\W\d_]{2,}', re.UNICODE)


def _extract_words(text: str):
    for m in WORD_RE.finditer(text):
        yield m.group(), m.start()


# ── Dictionary backends ──────────────────────────────────────────────────

_en_words = None
_dict_cache = {}
_wiki_wordlists = {}   # lang -> set of lowercase words


def _get_english_words():
    global _en_words
    if _en_words is not None:
        return _en_words
    try:
        from english_words import get_english_words_set
        _en_words = set()
        for w in get_english_words_set(['web2', 'gcide'], lower=False):
            _en_words.add(w.lower())
        logger.info(f"english-words: {len(_en_words):,} words")
    except Exception as e:
        logger.warning(f"english-words failed ({e}), using Hunspell+Wiktionary only")
        _en_words = set()
    return _en_words


def _get_hunspell_dict(lang_code: str):
    """Load Hunspell dictionary. Tries LibreOffice first (more comprehensive),
    then falls back to wooorm/dictionaries."""
    if lang_code in _dict_cache:
        return _dict_cache[lang_code]

    os.makedirs(DICT_DIR, exist_ok=True)

    # Determine source: LibreOffice if available, otherwise wooorm
    lo_info = LIBREOFFICE_DICT_MAP.get(lang_code)
    wooorm_code = WOORM_LANG_MAP.get(lang_code)

    if not lo_info and not wooorm_code:
        _dict_cache[lang_code] = None
        return None

    # Try LibreOffice first
    if lo_info:
        lo_dir, lo_prefix = lo_info
        lo_base = os.path.join(DICT_DIR, f'{lang_code}_lo')
        aff_path = os.path.join(DICT_DIR, f'{lang_code}_lo.aff')
        dic_path = os.path.join(DICT_DIR, f'{lang_code}_lo.dic')

        # 未打包时默认跳过下载，避免首次校验卡顿；LOCALIZEDTOOL_WIKI_WORDS=1 可启用
        need_download = not os.path.exists(dic_path) or not os.path.exists(aff_path)
        if need_download and not os.environ.get('LOCALIZEDTOOL_WIKI_WORDS'):
            logger.info(f"LibreOffice dict for '{lang_code}' not bundled; skipping download")
        elif need_download:
            ok = True
            for ext in ('dic', 'aff'):
                url = f'{LIBREOFFICE_BASE}/{lo_dir}/{lo_prefix}.{ext}'
                local = os.path.join(DICT_DIR, f'{lang_code}_lo.{ext}')
                try:
                    logger.info(f"Downloading LibreOffice dict: {url}")
                    _download(url, local)
                except Exception as e:
                    logger.warning(f"LibreOffice download failed: {e}")
                    ok = False
                    break
            if not ok:
                logger.info("LibreOffice download incomplete, falling back to wooorm")

        # Load if files exist
        if os.path.exists(dic_path) and os.path.exists(aff_path):
            try:
                from spylls.hunspell import Dictionary
                d = Dictionary.from_files(lo_base)
                _dict_cache[lang_code] = d
                logger.info(f"LibreOffice dict loaded: {lang_code}")
                return d
            except Exception as e:
                logger.warning(f"LibreOffice load failed: {e}")

    # Fall back to wooorm
    if wooorm_code:
        aff_path = os.path.join(DICT_DIR, f'{wooorm_code}.aff')
        dic_path = os.path.join(DICT_DIR, f'{wooorm_code}.dic')
        base_path = os.path.join(DICT_DIR, wooorm_code)

        # 未打包时默认跳过下载；LOCALIZEDTOOL_WIKI_WORDS=1 可启用联网下载
        need_download = not os.path.exists(dic_path) or not os.path.exists(aff_path)
        if need_download and not os.environ.get('LOCALIZEDTOOL_WIKI_WORDS'):
            logger.info(f"wooorm dict for '{lang_code}' not bundled; skipping download")
            _dict_cache[lang_code] = None
            return None
        elif need_download:
            ok = True
            for ext in ('dic', 'aff'):
                url = f'{WOORM_BASE_URL}/{wooorm_code}/index.{ext}'
                local = os.path.join(DICT_DIR, f'{wooorm_code}.{ext}')
                try:
                    logger.info(f"Downloading wooorm dict: {url}")
                    _download(url, local)
                except Exception as e:
                    logger.warning(f"wooorm download failed: {e}")
                    ok = False
                    break
            if not ok:
                _dict_cache[lang_code] = None
                return None

        if os.path.exists(dic_path) and os.path.exists(aff_path):
            try:
                from spylls.hunspell import Dictionary
                d = Dictionary.from_files(base_path)
                _dict_cache[lang_code] = d
                logger.info(f"wooorm dict loaded: {lang_code}")
                return d
            except Exception as e:
                logger.warning(f"wooorm load failed: {e}")

    _dict_cache[lang_code] = None
    return None


# ── Wiktionary offline word list ─────────────────────────────────────────

def _get_wiki_wordlist(lang: str) -> set:
    """Get or build Wiktionary word list for a language (one-time download).
    Downloads kaikki.org JSONL, extracts words + inflected forms, caches locally.
    """
    if lang in _wiki_wordlists:
        return _wiki_wordlists[lang]

    kaikki_name = KAIKKI_LANG_MAP.get(lang)
    if not kaikki_name:
        logger.info(f"No kaikki.org data for '{lang}'")
        _wiki_wordlists[lang] = set()
        return _wiki_wordlists[lang]

    word_file = os.path.join(DICT_DIR, f'wiki_words_{lang}.txt')

    # Already downloaded — just load
    if os.path.exists(word_file):
        words = set()
        with open(word_file, 'r', encoding='utf-8') as f:
            for line in f:
                words.add(line.rstrip('\n'))
        _wiki_wordlists[lang] = words
        logger.info(f"Wiktionary word list loaded: {lang} ({len(words):,} words)")
        return words

    # 未打包的语种：默认跳过联网下载（kaikki JSONL 可能达 1GB+），
    # 保证首次拼写检查不卡顿；如需自动下载请设置 LOCALIZEDTOOL_WIKI_WORDS=1。
    if not os.environ.get('LOCALIZEDTOOL_WIKI_WORDS'):
        logger.info(f"Wiktionary word list for '{lang}' not bundled locally; skipping download"
                    " (set LOCALIZEDTOOL_WIKI_WORDS=1 to auto-download)")
        _wiki_wordlists[lang] = set()
        return _wiki_wordlists[lang]

    # Download and extract
    url = KAIKKI_URL.format(lang=kaikki_name)
    logger.info(f"Downloading Wiktionary word list for {lang}: {url}")
    logger.info(f"This is a one-time download, may take 1-3 minutes...")

    words = set()
    count = 0
    try:
        if _requests_lib:
            # requests: handles gzip streaming natively
            resp = _requests_lib.get(url, headers={'User-Agent': 'LQA/1.0'}, stream=True, timeout=600)
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                count += 1
                try:
                    entry = json.loads(line)
                    w = entry.get('word', '')
                    if w:
                        words.add(w.lower())
                    for form in entry.get('forms', []):
                        f = form.get('form', '')
                        if f:
                            words.add(f.lower())
                except json.JSONDecodeError:
                    continue
                if count % 100000 == 0:
                    logger.info(f"  {lang}: {count:,} lines, {len(words):,} unique words")
        else:
            # urllib fallback
            req = urllib.request.Request(url, headers={'User-Agent': 'LQA/1.0'})
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw_line in resp:
                    line = raw_line.decode('utf-8', errors='replace').strip()
                    if not line:
                        continue
                    count += 1
                    try:
                        entry = json.loads(line)
                        w = entry.get('word', '')
                        if w:
                            words.add(w.lower())
                        for form in entry.get('forms', []):
                            f = form.get('form', '')
                            if f:
                                words.add(f.lower())
                    except json.JSONDecodeError:
                        continue
                    if count % 100000 == 0:
                        logger.info(f"  {lang}: {count:,} lines, {len(words):,} unique words")

    except Exception as e:
        logger.warning(f"Failed to download Wiktionary word list for {lang}: {e}")
        _wiki_wordlists[lang] = set()
        return _wiki_wordlists[lang]

    # Save to disk
    with open(word_file, 'w', encoding='utf-8') as f:
        for w in sorted(words):
            f.write(w + '\n')

    file_size = os.path.getsize(word_file) / (1024 * 1024)
    logger.info(f"Wiktionary word list saved: {lang} ({len(words):,} words, {file_size:.1f} MB)")

    _wiki_wordlists[lang] = words
    return words


# ── Spell Checker ────────────────────────────────────────────────────────

class SpellChecker(BaseChecker):
    name = "spell"
    label = "拼写检查"

    def __init__(self, language_code: str = "en", glossary_terms: set = None,
                 check_grammar: bool = False):
        super().__init__(language_code)
        self._is_cjk = self.language_code in CJK_LIMITED_SPELL
        self._glossary_terms = glossary_terms or set()
        self._glossary_lower = {t.lower() for t in self._glossary_terms if t}
        self._check_grammar = check_grammar

        self._base_lang = (self.language_code or 'en').split('-')[0].lower()
        self._is_english = self._base_lang == 'en'

        self._en_dict = None
        self._hunspell = None
        self._wiki_words = None

        if not self._is_cjk:
            if self._is_english:
                self._en_dict = _get_english_words()
                self._hunspell = _get_hunspell_dict(self._base_lang)
            else:
                self._hunspell = _get_hunspell_dict(self._base_lang)
            # Wiktionary word list (lazy, one-time download)
            self._wiki_words = _get_wiki_wordlist(self._base_lang)

    def _strip_placeholders(self, text: str) -> str:
        cleaned = STRIP_PATTERN.sub(' ', text)
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        return cleaned.strip()

    def _word_known(self, word: str) -> bool:
        """Check dictionaries: glossary → Wiktionary → english-words → Hunspell.
        Wiktionary is checked before Hunspell because set lookup (0.0001ms)
        is ~300x faster than Hunspell morphological analysis (0.03ms)."""
        if len(word) < 2:
            return True
        wl = word.lower()
        if wl in self._glossary_lower:
            return True

        # Tier 1: Wiktionary offline word list (fastest, ~1.4M+ words)
        if self._wiki_words and wl in self._wiki_words:
            return True

        # Tier 2: english-words (fast set, English only)
        if self._is_english and self._en_dict and wl in self._en_dict:
            return True

        # Tier 3: Hunspell morphological lookup (slower, last resort)
        if self._hunspell is not None:
            return self._hunspell.lookup(word)

        return False

    def _get_suggestions(self, word: str, cached_suggs: list = None) -> str:
        """Get comma-separated suggestions. Pass cached_suggs to avoid re-calling suggest()."""
        if cached_suggs is not None:
            return ', '.join(cached_suggs[:3]) if cached_suggs else ''
        if self._hunspell is not None:
            try:
                suggs = list(self._hunspell.suggest(word))[:3]
                return ', '.join(suggs) if suggs else ''
            except Exception:
                return ''
        return ''

    def _find_compound_parts(self, word: str) -> Optional[str]:
        """Check if word looks like a compound and return how it splits.

        Returns a string like "part1+part2" if the word can be split into known
        Wiktionary parts, or "camelCase" for camelCase patterns, or None if the
        word doesn't look like a compound.

        - camelCase (DragonBorn, SoulBlade)
        - Wiktionary split: word = known_part1 + known_part2
          (useful for languages like German where compounds are common)
        """
        # camelCase: lowercase followed by uppercase anywhere
        for i in range(1, len(word)):
            if word[i-1].islower() and word[i].isupper():
                return "camelCase"

        # Dictionary split: try splitting into known parts (min 3 chars each
        # to avoid trivial splits like "un+do" or "a+bend")
        if self._wiki_words:
            wl = word.lower()
            for i in range(3, len(wl) - 2):
                if wl[:i] in self._wiki_words and wl[i:] in self._wiki_words:
                    return f"{word[:i]}+{word[i:]}"
        return None

    def _is_compound_decomposition(self, word: str, suggestions: list) -> bool:
        """Check if suggestions just split the word with spaces/hyphens.
        Uses pre-fetched suggestions (avoid re-calling Hunspell suggest)."""
        if not suggestions:
            return False
        clean_word = re.sub(r'[\s\-]', '', word).lower()
        for sug in suggestions:
            if ' ' not in sug and '-' not in sug:
                continue
            if re.sub(r'[\s\-]', '', sug).lower() != clean_word:
                continue
            parts = re.split(r'[\s\-]+', sug)
            if all(len(p) >= 2 for p in parts):
                return True
        return False

    # ── Batch check ──────────────────────────────────────────────────

    # suggest() 调用预算：spylls 纯 Python 的 suggest 很慢（英文~0.08s，
    # 法文等其他语种可达 1.3s 甚至对个别病态词指数级卡死），大批量未知词
    # 会把校验拖到分钟级。默认 0 = 完全禁用建议（只报"疑似拼写错误"），
    # 保证校验速度；需要建议时再置正数（如 3）。
    SUGGEST_BUDGET = 0

    def batch_check(self, rows: list, source_col: str = "source",
                    target_col: str = "target") -> list[CheckResult]:
        if self._is_cjk or not rows:
            return []

        MAX_PER_ROW = 5
        results = []
        _suggest_cache = {}  # word -> list of suggestions (per-batch)
        suggest_left = self.SUGGEST_BUDGET

        for row_idx, src, tgt in rows:
            clean = self._strip_placeholders(tgt)
            if len(clean.strip()) < 3:
                continue

            unknown_count = 0
            for word, pos in _extract_words(clean):
                if self._word_known(word):
                    continue
                unknown_count += 1
                if unknown_count > MAX_PER_ROW:
                    break

                # Check if word looks like a compound (camelCase or dictionary split)
                compound_parts = self._find_compound_parts(word)
                if compound_parts:
                    context = clean[max(0, pos - 15):min(len(clean), pos + len(word) + 15)]
                    if compound_parts == "camelCase":
                        issue = f'疑似拼写错误: "{word}"（驼峰命名，请人工确认）'
                        sev = "warning"
                    else:
                        issue = f'疑似复合词: "{word}" → 拆分: {compound_parts}，请人工确认'
                        sev = "info"
                    results.append(self._make_result(
                        row_idx + 1, src, tgt, source_col, target_col,
                        issue=issue,
                        severity=sev,
                        details=f'上下文: "...{context}..."'
                    ))
                    continue

                # Fetch suggestions ONCE per word (expensive: ~128ms), cache per-batch；
                # 受预算约束，超限后不再计算建议，避免大批量未知词拖慢校验。
                if word in _suggest_cache:
                    all_suggs = _suggest_cache[word]
                elif suggest_left > 0 and self._hunspell is not None:
                    try:
                        all_suggs = list(self._hunspell.suggest(word))
                        suggest_left -= 1
                    except Exception:
                        all_suggs = []
                    _suggest_cache[word] = all_suggs
                else:
                    all_suggs = []

                # Compound decomposition check using pre-fetched suggestions
                if self._is_compound_decomposition(word, all_suggs):
                    context = clean[max(0, pos - 15):min(len(clean), pos + len(word) + 15)]
                    issue = f'疑似拼写错误: "{word}" → Hunspell建议拆分: {", ".join(all_suggs[:3])}，请人工确认'
                    results.append(self._make_result(
                        row_idx + 1, src, tgt, source_col, target_col,
                        issue=issue,
                        severity="warning",
                        details=f'上下文: "...{context}..."'
                    ))
                    continue

                context = clean[max(0, pos - 15):min(len(clean), pos + len(word) + 15)]
                suggs_str = ', '.join(all_suggs[:3]) if all_suggs else ''
                if suggs_str:
                    issue = f'疑似拼写错误: "{word}" → 建议: {suggs_str}'
                else:
                    issue = f'疑似拼写错误: "{word}"（无建议）'

                results.append(self._make_result(
                    row_idx + 1, src, tgt, source_col, target_col,
                    issue=issue,
                    severity="error",
                    details=f'上下文: "...{context}..."'
                ))

        return results

    # ── Per-row check ─────────────────────────────────────────────────

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        return self.batch_check(
            [(row_index, source_text, target_text)], source_col, target_col)
