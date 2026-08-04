"""Terminology consistency check — ensures terms are translated consistently.

Checks that when source text contains a known term from the glossary,
the target text uses the approved translation.
"""

import re
from .base import BaseChecker, CheckResult


class TerminologyChecker(BaseChecker):
    name = "terminology"
    label = "术语一致性检查"

    def __init__(self, language_code: str = "en",
                 glossary: dict[str, str] = None):
        """
        Args:
            language_code: Target language code
            glossary: Dict mapping source term → expected target term
        """
        super().__init__(language_code)
        self.glossary = glossary or {}

    @property
    def has_glossary(self) -> bool:
        return len(self.glossary) > 0

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        results = []

        if not source_text or not target_text or not self.glossary:
            return results

        source_lower = source_text.lower()
        target_lower = target_text.lower()

        # 长术语优先判断，便于「被更长的复合术语覆盖」的判定（旧物 ⊂ 废土旧物市场）
        for src_term, expected_tgt in sorted(self.glossary.items(),
                                             key=lambda x: -(len(x[0] or '') or 0)):
            if not src_term or not expected_tgt:
                continue

            src_term_lower = src_term.lower().strip()
            if not src_term_lower or src_term_lower not in source_lower:
                continue

            expected_tgt_lower = expected_tgt.lower().strip()
            if expected_tgt_lower in target_lower:
                continue

            # 被更长的、且译文正确的复合术语覆盖 → 跳过（避免 旧物→Relics 误报，
            # 因为 废土旧物市场→Wasteland Relic Market 已正确使用）
            if self._covered_by_longer(src_term_lower, source_lower, target_lower):
                continue

            results.append(self._make_result(
                row_index + 1, source_text, target_text, source_col, target_col,
                issue=f"术语翻译不一致: \"{src_term}\" 应翻译为 \"{expected_tgt}\"",
                severity="error",
                details=f"源术语 \"{src_term}\" 在源文本中出现，但目标文本中未找到对应的术语 \"{expected_tgt}\""
            ))

        return results

    def _covered_by_longer(self, src_term_lower: str, source_lower: str,
                           target_lower: str) -> bool:
        """src_term 是否是某个更长、且在源中出现且译文已正确使用的复合术语的子串。"""
        for t2, g2 in self.glossary.items():
            if not t2 or not g2:
                continue
            t2l = t2.lower().strip()
            if not t2l or len(t2l) <= len(src_term_lower):
                continue
            if (src_term_lower in t2l and t2l in source_lower
                    and g2.lower().strip() in target_lower):
                return True
        return False
