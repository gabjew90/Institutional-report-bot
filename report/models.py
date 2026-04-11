"""Dataclasses for report output."""

from dataclasses import dataclass, field


@dataclass
class DailyReport:
    report_date: str
    report_type: str  # "morning" or "afternoon"
    pdf_count: int
    markdown_content: str
    raw_json: dict = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
