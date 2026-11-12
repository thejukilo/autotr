from __future__ import annotations

from typing import Protocol

from autotr.market.features import SymbolState
from autotr.models import Signal


class Strategy(Protocol):
    def on_bar(self, state: SymbolState) -> Signal | None:
        """Called after every closed 1m bar of an active symbol. Return a signal or None."""
        ...
