"""Shared helper for the *clear* half of the factory + populate + clear pattern."""

from __future__ import annotations

from PySide6.QtWidgets import QLayout


def remove_all_widgets(layout: QLayout) -> None:
    """Remove every dynamically generated child of ``layout`` safely (``deleteLater``).

    Each child is also detached from its parent right away, so the parent holds
    no stale children until the event loop deletes them.
    """
    while layout.count():
        item = layout.takeAt(0)
        child = item.widget() if item is not None else None
        if child is not None:
            child.setParent(None)
            child.deleteLater()
