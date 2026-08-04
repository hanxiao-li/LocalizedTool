"""Punctuation check — LanguageTool (primary) + CJK fallback + general rules.

Architecture per language:
- CJK (zh, ja): our full-width/half-width logic (LT doesn't cover this)
- Other: LanguageTool local server (punctuation rules), with inline fallback rules
- All: quote matching, double punctuation
"""

import re
import bisect
import logging
from .base import BaseChecker, CheckResult

logger = logging.getLogger(__name__)

# ── CJK punctuation (kept — LanguageTool doesn't cover full-width/half-width) ─

CJK_LANGUAGES = {'zh-cn', 'zh-tw', 'zh-hk', 'ja-jp'}

HALF_TO_FULL_MAP = {
    ',': '，', '.': '。', '!': '！', '?': '？', ';': '；', ':': '：',
    '(': '（', ')': '）', '[': '【', ']': '】',
}

FRENCH_PUNCTUATION = {'?', '!', ';', ':'}


# ── Helpers ──────────────────────────────────────────────────────────────

def _is_cjk(char: str) -> bool:
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
        0x2E80 <= cp <= 0x2FDF or 0x3040 <= cp <= 0x309F or
        0x30A0 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF
    )


def _in_url_or_code(text: str, position: int) -> bool:
    before = text[max(0, position - 20):position + 1]
    return bool(re.search(r'https?://|www\.|\.com|\.cn|\.jp|\.kr|</?\w+', before))


# ── LanguageTool integration ─────────────────────────────────────────────

# Module-level LanguageTool client cache: lang -> LT instance or None
_lt_cache = {}
LT_PUNCT_CATEGORIES = {'PUNCTUATION', 'TYPOGRAPHY', 'PUNCTUATION_QUOTES'}


def _get_languagetool(lang_code: str):
    """Get or create LanguageTool client for a language (local server)."""
    lt_lang = lang_code.replace('_', '-').lower()
    # Normalize short codes
    if '-' not in lt_lang:
        map_short = {'en': 'en-US', 'de': 'de-DE', 'fr': 'fr-FR', 'es': 'es-ES',
                     'zh': 'zh-CN', 'ja': 'ja-JP', 'ko': 'ko-KR'}
        lt_lang = map_short.get(lt_lang, lt_lang + '-' + lt_lang.upper())

    if lang_code in _lt_cache:
        return _lt_cache[lang_code]

    try:
        from language_tool_python import LanguageTool
        lt = LanguageTool(lt_lang, remote_server='http://localhost:8081/v2')
        lt.check("test")
        _lt_cache[lang_code] = lt
        return lt
    except Exception:
        _lt_cache[lang_code] = None
        return None


# ── Checker ──────────────────────────────────────────────────────────────

class PunctuationChecker(BaseChecker):
    name = "punctuation"
    label = "标点规范检查"

    def _check_cjk_punctuation(self, target_text: str, row_index: int,
                                source_col: str, target_col: str,
                                source_text: str) -> list[CheckResult]:
        results = []
        cjk_chars = sum(1 for c in target_text if _is_cjk(c))
        if cjk_chars < 3:
            return results
        for i, char in enumerate(target_text):
            if char in HALF_TO_FULL_MAP:
                if _in_url_or_code(target_text, i):
                    continue
                context = target_text[max(0, i - 5):min(len(target_text), i + 6)]
                full = HALF_TO_FULL_MAP[char]
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f"CJK文本中使用了半角标点 \"{char}\"，应为全角 \"{full}\"",
                    severity="error",
                    details=f"上下文: ...{context}..."
                ))
        return results

    def _check_foreign_cjk_punctuation(self, target_text: str, row_index: int,
                                        source_col: str, target_col: str,
                                        source_text: str) -> list[CheckResult]:
        results = []
        for i, char in enumerate(target_text):
            cp = ord(char)
            if (0xFF01 <= cp <= 0xFF5E) or cp == 0x3002:
                context = target_text[max(0, i - 8):min(len(target_text), i + 9)]
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f"非CJK文本中出现全角标点 \"{char}\" (U+{cp:04X})，应为半角标点",
                    severity="error",
                    details=f"上下文: ...{context}..."
                ))
        return results

    def _check_languagetool(self, target_text: str, row_index: int,
                             source_col: str, target_col: str,
                             source_text: str) -> list[CheckResult]:
        """Run LanguageTool and filter to punctuation-related matches only."""
        lt = _get_languagetool(self.language_code)
        if lt is None:
            return []

        if len(target_text.strip()) < 3:
            return []

        results = []
        try:
            matches = lt.check(target_text)
        except Exception:
            # If check fails mid-run, disable LT for this session
            _lt_cache[self.language_code] = None
            return results

        for m in matches:
            try:
                rid = (getattr(m, 'ruleId', None) or getattr(m, 'rule_id', '')).upper()
                offset = getattr(m, 'offset', 0)
                err_len = getattr(m, 'errorLength', getattr(m, 'error_length', 0)) or 0
                msg = getattr(m, 'message', 'punctuation issue')

                if not any(kw in rid for kw in ('PUNCT', 'TYPOGRAPHY', 'QUOTE',
                                                 'COMMA', 'SPACE', 'WHITESPACE')):
                    continue

                context = target_text[max(0, offset - 10):min(len(target_text), offset + err_len + 10)]
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f"标点问题: {msg}",
                    severity="error",
                    details=f"规则: {rid}, 上下文: ...{context}..."
                ))
            except Exception:
                continue

        return results

    def _check_general_quotes(self, target_text: str, row_index: int,
                               source_col: str, target_col: str,
                               source_text: str) -> list[CheckResult]:
        results = []

        double_quotes = target_text.count('"')
        if double_quotes % 2 != 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="双引号数量不匹配（奇数个）",
                severity="error",
                details=f"发现{double_quotes}个双引号"
            ))

        quote_marks = 0
        for m in re.finditer(r"'", target_text):
            pos = m.start()
            before = target_text[pos - 1] if pos > 0 else ' '
            after = target_text[pos + 1] if pos + 1 < len(target_text) else ' '
            if before.isalpha() or after.isalpha():
                continue
            quote_marks += 1

        if quote_marks > 0 and quote_marks % 2 != 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="单引号数量不匹配（奇数个）",
                severity="error",
                details=f"发现{quote_marks}个引号用法的单引号（已排除单词内撇号）"
            ))

        if self.language_code in CJK_LANGUAGES:
            if '"' in target_text or "'" in target_text:
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue="CJK文本中使用了半角引号，建议使用全角引号「」『』或＂＇",
                    severity="error",
                    details="半角引号在CJK文本中显示效果不佳"
                ))

        return results

    def _check_double_punctuation(self, target_text: str, row_index: int,
                                   source_col: str, target_col: str,
                                   source_text: str) -> list[CheckResult]:
        results = []
        matches = list(re.finditer(r'[!?。！？]{2,}', target_text))
        for m in matches[:3]:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue=f"连续重复标点: \"{m.group()}\"",
                severity="error",
                details=f"位置: {m.start()}"
            ))
        return results

    BATCH_SEPARATOR = '\n###ROW###\n'

    def batch_check(self, rows: list, source_col: str = "source",
                    target_col: str = "target") -> list[CheckResult]:
        """Batch check all rows. Fast per-row checks + one LT call for all texts."""
        results = []

        # ── Per-row fast checks (no network) ──────────────────────
        for row_idx, src, tgt in rows:
            if not tgt or not tgt.strip():
                continue

            if self.language_code in CJK_LANGUAGES:
                results.extend(self._check_cjk_punctuation(
                    tgt, row_idx, source_col, target_col, src))
            else:
                results.extend(self._check_foreign_cjk_punctuation(
                    tgt, row_idx, source_col, target_col, src))

            results.extend(self._check_general_quotes(
                tgt, row_idx, source_col, target_col, src))
            results.extend(self._check_double_punctuation(
                tgt, row_idx, source_col, target_col, src))

        # ── Batch LanguageTool (one HTTP call for all rows) ───────
        lt = _get_languagetool(self.language_code)
        if lt is None:
            return results

        # Build combined text with row tracking
        valid_entries = []  # (row_idx, src, tgt, start_offset, end_offset)
        parts = []
        offset = 0

        for row_idx, src, tgt in rows:
            if not tgt or len(tgt.strip()) < 3:
                continue
            clean = tgt.strip()
            parts.append(clean)
            valid_entries.append((row_idx, src, tgt, offset, offset + len(clean)))
            offset += len(clean) + len(self.BATCH_SEPARATOR)

        if not parts:
            return results

        combined = self.BATCH_SEPARATOR.join(parts)

        # Single LT call
        try:
            matches = lt.check(combined)
        except Exception:
            _lt_cache[self.language_code] = None
            return results

        # Map LT offsets back to rows via binary search
        starts = [e[3] for e in valid_entries]
        import bisect

        for m in matches:
            try:
                mo = getattr(m, 'offset', getattr(m, 'errorOffset', 0))
                rid = (getattr(m, 'ruleId', None) or getattr(m, 'rule_id', '')).upper()
                msg = getattr(m, 'message', 'punctuation issue')
                err_len = getattr(m, 'errorLength', getattr(m, 'error_length', 0)) or 0

                # Only punctuation rules
                if not any(kw in rid for kw in ('PUNCT', 'TYPOGRAPHY', 'QUOTE',
                                                 'COMMA', 'SPACE', 'WHITESPACE')):
                    continue

                idx = bisect.bisect_right(starts, mo) - 1
                if idx < 0 or idx >= len(valid_entries):
                    continue

                row_idx, src, tgt, start, end = valid_entries[idx]
                if not (start <= mo < end):
                    continue

                local_offset = mo - start
                context = tgt[max(0, local_offset - 10):min(len(tgt), local_offset + err_len + 10)]
                results.append(self._make_result(
                    row_idx + 1, src, tgt, source_col, target_col,
                    issue=f"标点问题: {msg}",
                    severity="error",
                    details=f"规则: {rid}, 上下文: ...{context}..."
                ))
            except Exception:
                continue

        return results

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        """Per-row check. Delegates to batch_check for efficiency."""
        return self.batch_check(
            [(row_index, source_text, target_text)], source_col, target_col)
