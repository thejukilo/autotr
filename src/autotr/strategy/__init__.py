from __future__ import annotations

from autotr.config import Config


def make_strategy(cfg: Config):
    """Strategy selected by `strategy.name`."""
    name = cfg.strategy.name
    if name == "scalper":
        from autotr.strategy.scalper import ScalperStrategy
        return ScalperStrategy(cfg.strategy, cfg.exchange)
    if name == "confluence":
        from autotr.strategy.confluence import ConfluenceStrategy
        return ConfluenceStrategy(cfg.strategy, cfg.exchange)
    raise ValueError(f"unknown strategy {name!r} (expected 'confluence' or 'scalper')")
