"""Report HTML rendering from frozen DTOs (assembler and routes are separate cards)."""

from .render import PIN_ANCHOR_VERSION, REPORT_CSP, REPORT_HEADERS, build_environment, render_report

__all__ = ["PIN_ANCHOR_VERSION", "REPORT_CSP", "REPORT_HEADERS", "build_environment", "render_report"]
