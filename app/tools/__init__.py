"""Standalone tools that run alongside the recording app.

Phase 8 introduces the post-session MP4 processor, run manually after
the recording app has shut down. The package is deliberately separate
from `app/core/` and `app/media/` because the processor is a different
program — it never instantiates the live pipeline graph and shouldn't
share state with it.
"""
