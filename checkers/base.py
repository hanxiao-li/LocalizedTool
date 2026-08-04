"""Base checker class and result data structure."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CheckResult:
    """Represents a single check finding."""
    row: int
    column: str
    source_text: str
    target_text: str
    check_type: str
    issue: str
    severity: str = "error"  # error, warning, info
    details: Optional[str] = None
    language: str = ""        # Target language display name
    source_lang: str = ""     # Source language display name


class BaseChecker:
    """Base class for all LQA checkers."""

    name: str = "base"
    label: str = "Base Check"

    def __init__(self, language_code: str = "en"):
        self.language_code = language_code.lower()

    def check(self, source_text: str, target_text: str, row_index: int,
              source_col: str = "source", target_col: str = "target") -> list[CheckResult]:
        """Run the check on a single row. Returns list of CheckResult."""
        raise NotImplementedError("Subclasses must implement check()")

    def _make_result(self, row: int, source: str, target: str,
                     source_col: str, target_col: str,
                     issue: str, severity: str = "warning",
                     details: str = None) -> CheckResult:
        return CheckResult(
            row=row,
            column=target_col,
            source_text=source,
            target_text=target,
            check_type=self.name,
            issue=issue,
            severity=severity,
            details=details,
        )
