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

        for src_term, expected_tgt in self.glossary.items():
            if not src_term or not expected_tgt:
                continue

            src_term_lower = src_term.lower().strip()

            # Check if source term appears in source text
            if src_term_lower not in source_lower:
                continue

            expected_tgt_lower = expected_tgt.lower().strip()

            # Check if expected translation appears in target text
            if expected_tgt_lower not in target_lower:
                # Find the actual text around where the term should be
                results.append(self._make_result(
                    row_index + 1, source_text, target_text, source_col, target_col,
                    issue=f"术语翻译不一致: \"{src_term}\" 应翻译为 \"{expected_tgt}\"",
                    severity="error",
                    details=f"源术语 \"{src_term}\" 在源文本中出现，但目标文本中未找到对应的术语 \"{expected_tgt}\""
                ))

        return results
