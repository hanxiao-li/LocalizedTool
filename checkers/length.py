"""Length limit check — verifies target text doesn't exceed per-row character limit.

The limit value is read from a column in the Excel file for each row.
If the limit cell is not a valid number, the row is skipped.
"""

from .base import BaseChecker, CheckResult


class LengthChecker(BaseChecker):
    name = "length"
    label = "长度限制检查"

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target",
              limit_value: int | None = None) -> list[CheckResult]:
        results = []

        if not target_text or limit_value is None or limit_value <= 0:
            return results

        char_count = len(target_text)

        if char_count > limit_value:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue=f"字符数超限: {char_count}/{limit_value} (超出{char_count - limit_value}个字符)",
                severity="error",
                details=f"当前字符数: {char_count}, 本行限制: {limit_value}"
            ))

        return results
