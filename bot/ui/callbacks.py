from __future__ import annotations

"""UI callback_data helpers.

This module exists to avoid circular imports between UI rendering (screens)
and services that need to build callback_data (notifications, etc.).

Keep it **dependency-free** (no imports from services/handlers).
"""

from typing import List

UI_PREFIX = "ui:"


def ui_cb(*parts: str) -> str:
    """Build a safe callback_data for UI interactions."""
    safe: List[str] = []
    for p in parts:
        p = (p or "").replace(":", "_")
        safe.append(p)
    return UI_PREFIX + ":".join(safe)
