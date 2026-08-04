"""Number consistency check — uses established libraries:
- Babel: locale-aware digit number parsing (10,000 vs 10.000 vs 10 000)
- words2num2: word-form number extraction in 100+ languages (一万, ten thousand)
- text2num: European language word-form extraction (French, German, Spanish, etc.)

Rule: every numeric value found in source must appear in target,
AND every numeric value found in target must appear in source.
"""

import re
import logging
from .base import BaseChecker, CheckResult

logger = logging.getLogger(__name__)

# Locale alias map (LQA codes -> library codes)
BABEL_LOCALE = {
    'de': 'de_DE', 'en': 'en_US', 'fr': 'fr_FR', 'es': 'es_ES',
    'pt': 'pt_BR', 'ru': 'ru_RU', 'it': 'it_IT', 'nl': 'nl_NL',
    'pl': 'pl_PL', 'sv': 'sv_SE', 'da': 'da_DK', 'tr': 'tr_TR',
}

TEXT2NUM_LANGS = {'en', 'fr', 'es', 'pt', 'de', 'ca', 'ru'}

# Strip placeholders/tags before number extraction
STRIP_RE = re.compile(
    r'\{[^}]*\}|<%[^>]*%>|</?\w+[^>]*>|\$\{[^}]+\}|'
    r'%\([^)]+\)[sdifFgcboxX]|%(\d+\$)?[+\-]?[0-9.]*[sdifFgcboxX]'
)

# Map full-width CJK digits to ASCII
_FW_DIGITS = str.maketrans('０１２３４５６７８９', '0123456789')

# Match digit sequences possibly containing locale separators (10,000 / 10.000 / 10 000)
_DIGIT_RE = re.compile(r'[\d０-９]+(?:[,.\s\xa0]+[\d０-９]+)*')


def _get_base_lang(lang_code: str) -> str:
    return (lang_code or 'en').lower().replace('_', '-').split('-')[0]


def _extract_digit_numbers(text: str, base_lang: str) -> set[int]:
    """Extract numbers from digit sequences using Babel."""
    clean = STRIP_RE.sub(' ', text)
    values = set()

    for m in _DIGIT_RE.finditer(clean):
        num_str = m.group().translate(_FW_DIGITS)
        try:
            from babel.numbers import parse_number
            locale = BABEL_LOCALE.get(base_lang, base_lang)
            val = parse_number(num_str, locale=locale)
            values.add(int(val))
        except Exception:
            digits = ''.join(c for c in num_str if c.isdigit())
            if digits:
                try:
                    values.add(int(digits))
                except ValueError:
                    pass
    return values


def _extract_word_numbers(text: str, base_lang: str) -> set[int]:
    """Extract numbers from word forms using words2num2 or text2num."""
    clean = STRIP_RE.sub(' ', text)
    values = set()

    # Try words2num2 first (100+ languages)
    try:
        from words2num2 import words2num_sentence
        w2n_lang = {'zh': 'zh_CN', 'ja': 'ja', 'ko': 'ko',
                    'de': 'de', 'en': 'en', 'fr': 'fr'}.get(base_lang, base_lang)
        converted = words2num_sentence(clean, lang=w2n_lang)
        for m in _DIGIT_RE.finditer(converted):
            try:
                digits = ''.join(c for c in m.group() if c.isdigit())
                if digits:
                    values.add(int(digits))
            except ValueError:
                pass
    except Exception:
        pass

    # Fallback: text2num for European languages
    if base_lang in TEXT2NUM_LANGS:
        try:
            from text_to_num import alpha2digit
            converted = alpha2digit(clean, base_lang)
            for m in _DIGIT_RE.finditer(converted):
                try:
                    digits = ''.join(c for c in m.group() if c.isdigit())
                    if digits:
                        values.add(int(digits))
                except ValueError:
                    pass
        except Exception:
            pass

    return values


def _extract_all_numbers(text: str, lang_code: str) -> set[int]:
    """Extract all numeric values from text: digits + word forms."""
    base_lang = _get_base_lang(lang_code)
    digits = _extract_digit_numbers(text, base_lang)
    words = _extract_word_numbers(text, base_lang)
    return digits | words


class NumbersChecker(BaseChecker):
    name = "numbers"
    label = "数字一致性检查"

    def __init__(self, target_lang: str = "en", source_lang: str = None):
        super().__init__(target_lang)
        # Source language for extracting numbers from source text
        # (target language for extracting from target text)
        self.source_lang = (source_lang or target_lang).lower()

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        if not source_text or not target_text:
            return results

        src_values = _extract_all_numbers(source_text, self.source_lang)
        tgt_values = _extract_all_numbers(target_text, self.language_code)

        # Forward: source → target
        for val in sorted(src_values):
            if val not in tgt_values:
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f'数字 "{val}" 在目标文本中缺失',
                    severity="error",
                    details=f'源文本中的数字 {val} 在目标文本中未找到'
                ))

        # Reverse: target → source (warning) — only compare Arabic digits
        # Word-form numbers are excluded because they're unreliable across
        # languages (e.g. "one" may be a pronoun, not the number 1).
        src_digits = _extract_digit_numbers(source_text, _get_base_lang(self.source_lang))
        tgt_digits = _extract_digit_numbers(target_text, _get_base_lang(self.language_code))
        for val in sorted(tgt_digits):
            if val not in src_digits:
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f'目标文本包含数字 "{val}"，但源文本中不存在',
                    severity="warning",
                    details=f'目标文本中的阿拉伯数字 {val} 在源文本中未找到，请确认是否需要翻译'
                ))

        return results
