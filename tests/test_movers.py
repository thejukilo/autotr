import pandas as pd

from autotr.data.movers import MoversStore


def test_select_top_movers_by_abs_change_with_volume_floor():
    daily = pd.DataFrame([
        # symbol, day, open, close, quote_volume
        ("AAAUSDT", "2026-01-01", 1.0, 1.70, 300e6),   # +70%
        ("BBBUSDT", "2026-01-01", 1.0, 0.60, 100e6),   # -40%
        ("CCCUSDT", "2026-01-01", 1.0, 2.00, 1e6),     # +100% but too little volume
        ("DDDUSDT", "2026-01-01", 1.0, 1.01, 900e6),   # +1%
        ("AAAUSDT", "2026-01-02", 1.0, 1.05, 300e6),
        ("DDDUSDT", "2026-01-02", 1.0, 1.20, 900e6),
    ], columns=["symbol", "day", "open", "close", "quote_volume"])
    idx = MoversStore.select(daily, top_n=2, min_quote_volume=20e6)
    assert idx == {"2026-01-01": ["AAAUSDT", "BBBUSDT"], "2026-01-02": ["DDDUSDT", "AAAUSDT"]}
