"""Placeholder and tag consistency check.

Uses mature regex patterns to detect common game/software placeholder formats:
- {0}, {1}, {name} — format strings
- %s, %d, %f, %@ — printf-style
- ${var}, %(name)s — Python-style
- <tag>, [tag] — XML/bracket tags
- {{var}} — double-brace templates
- Rich text tags: <color=...>, <size=...>, etc.
"""

import re
from collections import Counter
from .base import BaseChecker, CheckResult

# Common placeholder patterns found in game localization
PLACEHOLDER_PATTERNS = [
    # Curly brace format: {0}, {name}, {0:format}
    (r'\{[^}]*\}', '花括号占位符'),
    # Percent format: %s, %d, %f, %@, %1$s
    (r'%(\d+\$)?[+\-]?[0-9.]*[sdifFgcboxXhHlLqQjJtTzZ@%]', 'printf风格占位符'),
    # Dollar-brace: ${var}
    (r'\$\{[^}]+\}', '${}变量占位符'),
    # Percent-paren: %(name)s
    (r'%\([^)]+\)[sdifFgcboxX]', '%(name)风格占位符'),
    # Double brace: {{var}}
    (r'\{\{[^}]+\}\}', '{{}}模板占位符'),
    # Square bracket: [var] or [[var]]
    (r'\[\[?[A-Za-z_][A-Za-z0-9_]*\]\]?', '[]括号占位符'),
    # XML/HTML tags: <tag>, <tag attr="val">
    (r'</?[A-Za-z_][A-Za-z0-9_]*(?:\s[^>]*)?/?>', 'XML/HTML标签'),
    # Rich text color/size tags
    (r'</?(?:color|size|b|i|u|material|font|link|a|s|sub|sup|smallcaps|allcaps|nobr|br|margin|mark|space|sprite|style)(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]*))?\s*(?:\s[^>]*)?>',
     '富文本标签'),
    # At-format: %@
    (r'%@', '@格式占位符'),
]


def extract_placeholders(text: str) -> dict[str, list[str]]:
    """Extract all placeholders from text, grouped by pattern type."""
    found = {}
    for pattern, label in PLACEHOLDER_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            found[label] = matches
    return found


class PlaceholderChecker(BaseChecker):
    name = "placeholder"
    label = "占位符和标签检查"

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        if not target_text or not target_text.strip():
            return results

        source_placeholders = extract_placeholders(source_text)
        target_placeholders = extract_placeholders(target_text)

        # Check each pattern type present in the source
        for pattern_name in source_placeholders:
            src_list = source_placeholders.get(pattern_name, [])
            tgt_list = target_placeholders.get(pattern_name, [])

            # Count occurrences
            src_counts = Counter(src_list)
            tgt_counts = Counter(tgt_list)

            # Check for missing placeholders
            for ph, count in src_counts.items():
                tgt_count = tgt_counts.get(ph, 0)
                if tgt_count < count:
                    missing_count = count - tgt_count
                    results.append(self._make_result(
                        row_index + 1, source_text, target_text, source_col, target_col,
                        issue=f"{pattern_name}缺失: \"{ph}\" (源中有{count}个，目标中只有{tgt_count}个)",
                        severity="error",
                        details=f"缺失的占位符: {ph}"
                    ))
                elif tgt_count > count:
                    extra_count = tgt_count - count
                    results.append(self._make_result(
                        row_index + 1, source_text, target_text, source_col, target_col,
                        issue=f"{pattern_name}多出: \"{ph}\" (源中有{count}个，目标中有{tgt_count}个)",
                        severity="error",
                        details=f"多余的占位符: {ph}"
                    ))

            # Check for extra placeholders in target that aren't in source
            for ph, count in tgt_counts.items():
                if ph not in src_counts:
                    results.append(self._make_result(
                        row_index + 1, source_text, target_text, source_col, target_col,
                        issue=f"目标中多出{pattern_name}: \"{ph}\"，源中不存在",
                        severity="error",
                        details=f"多余的占位符: {ph}"
                    ))

        return results
