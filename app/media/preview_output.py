"""Backward-compatible preview output wrapper."""

from __future__ import annotations

from app.media.output_renderer import OutputRenderer


class PreviewOutput(OutputRenderer):
    """Compatibility alias for older imports."""
