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
    # Research strategies: src/autotr/strategy/research/<name>.py defining `Strategy(cfg, fees)`.
    import importlib
    try:
        mod = importlib.import_module(f"autotr.strategy.research.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"autotr.strategy.research.{name}":
            raise
        raise ValueError(f"unknown strategy {name!r}: not 'confluence', 'scalper' or a module in "
                         "src/autotr/strategy/research/") from None
    return mod.Strategy(cfg.strategy, cfg.exchange)
