"""Escape character consistency check.

Checks for escape sequences like \\n, \\t, \\r, \\r\\n, \\", \\\\ etc.
"""

import re
from collections import Counter
from .base import BaseChecker, CheckResult

# Common escape sequences in game/software strings
ESCAPE_PATTERN = re.compile(
    r'''(?:\\[ntr'"\\0abfv])|'''     # Standard escape: \n \t \r \' \" \\ \0 \a \b \f \v
    r'''(?:\\x[0-9A-Fa-f]{2})|'''    # Hex escape: \x41
    r'''(?:\\u[0-9A-Fa-f]{4})|'''    # Unicode escape: A
    r'''(?:\\U[0-9A-Fa-f]{8})|'''    # Long Unicode: \U00000041
    r'''(?:\\[0-7]{1,3})|'''          # Octal escape: \101
    r'''(?:&#\d+;)|'''                # HTML numeric: &#123;
    r'''(?:&#x[0-9A-Fa-f]+;)|'''     # HTML hex: &#x7B;
    r'''(?:&amp;|&lt;|&gt;|&quot;|&apos;|&nbsp;)'''  # HTML entities
)


class EscapeChecker(BaseChecker):
    name = "escape"
    label = "转义符一致性检查"

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        if not target_text or not target_text.strip():
            return results

        src_escapes = Counter(ESCAPE_PATTERN.findall(source_text))
        tgt_escapes = Counter(ESCAPE_PATTERN.findall(target_text))

        all_escapes = set(src_escapes.keys()) | set(tgt_escapes.keys())

        for esc in all_escapes:
            src_count = src_escapes.get(esc, 0)
            tgt_count = tgt_escapes.get(esc, 0)

            if src_count != tgt_count:
                label = self._escape_label(esc)
                if src_count > tgt_count:
                    results.append(self._make_result(
                        row_index + 1, source_text, target_text, source_col, target_col,
                        issue=f"转义符缺失: {label} (源中有{src_count}个，目标中只有{tgt_count}个)",
                        severity="error",
                        details=f"缺失 {src_count - tgt_count} 个转义符: {esc}"
                    ))
                else:
                    results.append(self._make_result(
                        row_index + 1, source_text, target_text, source_col, target_col,
                        issue=f"转义符多出: {label} (源中有{src_count}个，目标中有{tgt_count}个)",
                        severity="error",
                        details=f"多出 {tgt_count - src_count} 个转义符: {esc}"
                    ))

        return results

    @staticmethod
    def _escape_label(esc: str) -> str:
        """Human-readable label for an escape sequence."""
        labels = {
            '\n': '\\n (换行)',
            '\t': '\\t (制表)',
            '\r': '\\r (回车)',
            '\\"': '\\" (双引号)',
            "\\'": "\\' (单引号)",
            '\\\\': '\\\\ (反斜杠)',
            '\\0': '\\0 (空字符)',
        }
        return labels.get(esc, esc)
