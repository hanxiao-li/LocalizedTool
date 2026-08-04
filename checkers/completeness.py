"""Translation completeness check — verifies target text is actually translated
in the target language.

Checks:
1. Source has text but target is empty — untranslated
2. Source is empty but target has text — extra content
3. Target text characters belong to the target language's allowed scripts
"""

from .base import BaseChecker, CheckResult


# ── Unicode script classification ──────────────────────────────────────
# Classifies each character into a broad script group based on Unicode ranges.
# Zero external dependencies — pure Python standard library.


def _char_script(cp: int) -> str:
    """Classify a Unicode codepoint into a broad script group."""
    # CJK Unified Ideographs + Extensions A/B + Compatibility + Radicals
    if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or \
       (0x20000 <= cp <= 0x2A6DF) or (0xF900 <= cp <= 0xFAFF) or \
       (0x2F800 <= cp <= 0x2FA1F):
        return 'CJK'
    # Hiragana
    if 0x3040 <= cp <= 0x309F:
        return 'Hiragana'
    # Katakana (incl. half-width)
    if 0x30A0 <= cp <= 0x30FF or 0xFF65 <= cp <= 0xFF9F:
        return 'Katakana'
    # Hangul (syllables + jamo)
    if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF) or \
       (0x3130 <= cp <= 0x318F) or (0xA960 <= cp <= 0xA97C) or \
       (0xD7B0 <= cp <= 0xD7FF):
        return 'Hangul'
    # Cyrillic (basic + supplement)
    if (0x0400 <= cp <= 0x04FF) or (0x0500 <= cp <= 0x052F) or \
       (0x2DE0 <= cp <= 0x2DFF) or (0xA640 <= cp <= 0xA69F):
        return 'Cyrillic'
    # Arabic (basic + supplement + presentation forms)
    if (0x0600 <= cp <= 0x06FF) or (0x0750 <= cp <= 0x077F) or \
       (0xFB50 <= cp <= 0xFDFF) or (0xFE70 <= cp <= 0xFEFF) or \
       (0x08A0 <= cp <= 0x08FF):
        return 'Arabic'
    # Thai
    if 0x0E00 <= cp <= 0x0E7F:
        return 'Thai'
    # Latin (ASCII letters + Latin-1 Supplement + Extended A/B/C/D + IPA)
    if (0x0041 <= cp <= 0x005A) or (0x0061 <= cp <= 0x007A) or \
       (0x00C0 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF) or \
       (0x2C60 <= cp <= 0x2C7F) or (0xA720 <= cp <= 0xA7FF) or \
       (0x0250 <= cp <= 0x02AF):
        return 'Latin'
    # Common: digits, punctuation, spaces, symbols, emoji
    if cp < 0x0041 or (0x005B <= cp <= 0x0060) or (0x007B <= cp <= 0x00BF) or \
       cp in (0x00D7, 0x00F7) or \
       (0x2000 <= cp <= 0x206F) or (0x20A0 <= cp <= 0x20CF) or \
       (0x2100 <= cp <= 0x214F) or (0x2190 <= cp <= 0x21FF) or \
       (0x2200 <= cp <= 0x22FF) or (0x2300 <= cp <= 0x23FF) or \
       (0x2500 <= cp <= 0x257F) or (0x2600 <= cp <= 0x26FF) or \
       (0x2700 <= cp <= 0x27BF) or (0x2E00 <= cp <= 0x2E7F) or \
       (0x3000 <= cp <= 0x303F) or (0xFE00 <= cp <= 0xFE0F) or \
       (0xFE30 <= cp <= 0xFE4F) or (0xFF00 <= cp <= 0xFF0F) or \
       (0xFF10 <= cp <= 0xFF19) or (0xFF1A <= cp <= 0xFF20) or \
       (0xFF3B <= cp <= 0xFF40) or (0xFF5B <= cp <= 0xFF64) or \
       (0x1F000 <= cp <= 0x1FFFF):
        return 'Common'
    return 'Other'


# ── Allowed scripts per target language (base lang code) ───────────────

_ALLOWED_SCRIPTS: dict[str, set[str]] = {
    # Latin-only (most European languages)
    'en': {'Latin', 'Common'},
    'de': {'Latin', 'Common'},
    'fr': {'Latin', 'Common'},
    'es': {'Latin', 'Common'},
    'pt': {'Latin', 'Common'},
    'it': {'Latin', 'Common'},
    'nl': {'Latin', 'Common'},
    'pl': {'Latin', 'Common'},
    'sv': {'Latin', 'Common'},
    'da': {'Latin', 'Common'},
    'tr': {'Latin', 'Common'},
    'vi': {'Latin', 'Common'},
    'id': {'Latin', 'Common'},
    'ms': {'Latin', 'Common'},
    # CJK
    'zh': {'CJK', 'Latin', 'Common'},
    'ja': {'CJK', 'Hiragana', 'Katakana', 'Latin', 'Common'},
    'ko': {'Hangul', 'CJK', 'Latin', 'Common'},
    # Cyrillic
    'ru': {'Cyrillic', 'Latin', 'Common'},
    # Arabic
    'ar': {'Arabic', 'Latin', 'Common'},
    # Thai
    'th': {'Thai', 'Latin', 'Common'},
}

_LANG_NAMES: dict[str, str] = {
    'zh': '中文', 'ja': '日本語', 'ko': '한국어',
    'en': 'English', 'de': 'Deutsch', 'fr': 'Français',
    'es': 'Español', 'pt': 'Português', 'ru': 'Русский',
    'it': 'Italiano', 'nl': 'Nederlands', 'pl': 'Polski',
    'sv': 'Svenska', 'da': 'Dansk', 'tr': 'Türkçe',
    'ar': 'العربية', 'th': 'ไทย', 'vi': 'Tiếng Việt',
    'id': 'Bahasa Indonesia', 'ms': 'Bahasa Melayu',
}


def _find_foreign_chars(text: str, allowed: set[str]) -> list[str]:
    """Return up to 5 foreign (disallowed) characters found in text."""
    foreign = []
    for ch in text:
        if ch.isspace():
            continue
        if _char_script(ord(ch)) not in allowed:
            foreign.append(ch)
            if len(foreign) >= 5:
                break
    return foreign


class CompletenessChecker(BaseChecker):
    name = "completeness"
    label = "翻译语言空或翻成其他语言判定"

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        src_empty = not source_text or not source_text.strip()
        tgt_empty = not target_text or not target_text.strip()

        # ── Empty checks ──────────────────────────────────────────────

        if not src_empty and tgt_empty:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="目标文本为空，可能未翻译",
                severity="error",
                details="源语言列有内容但目标语言列为空"
            ))

        if src_empty and not tgt_empty:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="源文本为空但目标文本有内容",
                severity="error",
                details="源语言为空但目标语言有翻译内容，请确认"
            ))

        # ── Script check ──────────────────────────────────────────────

        if tgt_empty:
            return results

        target_base_lang = (self.language_code or 'en').split('-')[0].lower()
        allowed = _ALLOWED_SCRIPTS.get(target_base_lang)

        if not allowed:
            return results  # unknown language, skip script check

        foreign = _find_foreign_chars(target_text, allowed)
        if not foreign:
            return results

        # Determine which foreign scripts were found
        foreign_scripts: set[str] = set()
        for ch in foreign:
            s = _char_script(ord(ch))
            if s not in ('Other', 'Common'):
                foreign_scripts.add(s)
        script_desc = '/'.join(sorted(foreign_scripts)) if foreign_scripts else '未知脚本'
        foreign_str = ' '.join(foreign)
        expected_name = _LANG_NAMES.get(target_base_lang, target_base_lang)

        results.append(self._make_result(
            row_index + 1, source_text, target_text, source_col, target_col,
            issue=f"目标文本含非目标语种字符: {foreign_str}（{script_desc}），期望为「{expected_name}」",
            severity="error",
            details=f"脚本检测发现{len(foreign)}个非{expected_name}允许的字符"
        ))

        return results
