"""Leading/trailing whitespace consistency check.

Verifies that leading and trailing whitespace in the target matches the source.
"""

from .base import BaseChecker, CheckResult


class WhitespaceChecker(BaseChecker):
    name = "whitespace"
    label = "首尾空格一致性检查"

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        if not target_text:
            return results

        src_leading = len(source_text) - len(source_text.lstrip())
        tgt_leading = len(target_text) - len(target_text.lstrip())

        src_trailing = len(source_text) - len(source_text.rstrip())
        tgt_trailing = len(target_text) - len(target_text.rstrip())

        if src_leading > 0 and tgt_leading == 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="目标文本缺少前导空格",
                severity="error",
                details=f"源文本有{src_leading}个前导空格，目标文本没有"
            ))
        elif src_leading == 0 and tgt_leading > 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="目标文本多出了前导空格",
                severity="error",
                details=f"源文本没有前导空格，目标文本有{tgt_leading}个"
            ))

        if src_trailing > 0 and tgt_trailing == 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="目标文本缺少尾部空格",
                severity="error",
                details=f"源文本有{src_trailing}个尾部空格，目标文本没有"
            ))
        elif src_trailing == 0 and tgt_trailing > 0:
            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue="目标文本多出了尾部空格",
                severity="error",
                details=f"源文本没有尾部空格，目标文本有{tgt_trailing}个"
            ))

        return results
