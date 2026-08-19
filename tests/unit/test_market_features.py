from __future__ import annotations

from pa_agent.ai.market_features import compute_simple_market_features, render_simple_market_features
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame


def _frame(*bars: KlineBar) -> KlineFrame:
    n = len(bars)
    return KlineFrame(
        symbol="XAUUSD",
        timeframe="5m",
        bars=bars,
        indicators=IndicatorBundle(
            ema20=tuple(100.0 for _ in range(n)),
            atr14=tuple(2.0 for _ in range(n)),
        ),
        snapshot_ts_local_ms=1,
    )


def test_range_position_upper_third() -> None:
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(i), open=105.0, high=110.0, low=100.0, close=108.0, volume=1)
        for i in range(8)
    )
    features = compute_simple_market_features(_frame(*bars), lookback=8)
    assert features.range_high == 110.0
    assert features.range_low == 100.0
    assert features.zone == "upper_third"
    assert features.price_position is not None
    assert features.price_position > 2 / 3


def test_swing_pivots_and_structure_label() -> None:
    bars = (
        KlineBar(seq=1, ts_open=5.0, open=109.0, high=111.0, low=108.0, close=110.0, volume=1),
        KlineBar(seq=2, ts_open=4.0, open=105.0, high=110.0, low=104.5, close=109.0, volume=1),
        KlineBar(seq=3, ts_open=3.0, open=107.0, high=107.5, low=104.0, close=105.0, volume=1),
        KlineBar(seq=4, ts_open=2.0, open=102.0, high=108.0, low=101.5, close=107.0, volume=1),
        KlineBar(seq=5, ts_open=1.0, open=104.0, high=105.0, low=100.0, close=102.0, volume=1),
        KlineBar(seq=6, ts_open=0.0, open=100.0, high=105.0, low=99.0, close=104.0, volume=1),
    )
    features = compute_simple_market_features(_frame(*bars), lookback=6)
    assert len(features.swings) >= 2
    kinds = {p.kind for p in features.swings}
    assert "high" in kinds and "low" in kinds


def test_hl_count_triggers_on_high_breaks() -> None:
    bars = (
        KlineBar(seq=1, ts_open=3.0, open=10.2, high=10.4, low=10.0, close=10.3, volume=1),
        KlineBar(seq=2, ts_open=2.0, open=10.0, high=10.2, low=9.8, close=10.1, volume=1),
        KlineBar(seq=3, ts_open=1.0, open=9.9, high=10.1, low=9.7, close=10.0, volume=1),
        KlineBar(seq=4, ts_open=0.0, open=9.8, high=10.0, low=9.6, close=9.9, volume=1),
    )
    features = compute_simple_market_features(_frame(*bars), lookback=4)
    assert features.hl_count.bull_count >= 2
    assert features.hl_count.bull_candidate in ("h2", "h3")
    assert features.hl_count.last_bull_trigger_seq == 1


def test_breakout_failure_detected() -> None:
    # Range top ~100; K3 closes above; K1 reclaims below.
    bars = (
        KlineBar(seq=1, ts_open=4.0, open=99.0, high=99.5, low=98.5, close=99.0, volume=1),
        KlineBar(seq=2, ts_open=3.0, open=99.2, high=99.8, low=98.8, close=99.3, volume=1),
        KlineBar(seq=3, ts_open=2.0, open=100.0, high=101.5, low=99.8, close=101.2, volume=1),
        KlineBar(seq=4, ts_open=1.0, open=99.0, high=100.0, low=98.5, close=99.2, volume=1),
        KlineBar(seq=5, ts_open=0.0, open=98.0, high=99.0, low=97.5, close=98.5, volume=1),
        KlineBar(seq=6, ts_open=-1.0, open=97.0, high=98.0, low=96.5, close=97.5, volume=1),
    )
    features = compute_simple_market_features(_frame(*bars), lookback=6)
    failed = [e for e in features.breakout_events if e.event == "failed"]
    assert failed, "expected a failed upside breakout reclaim"
    assert failed[-1].level_kind == "range_high"
    assert features.breakout_quality == "failed"
    assert features.mm_as_tp_ok is False


def test_surviving_breakout_enables_mm_flag() -> None:
    # Build a range then close above and stay above for several bars (newest=seq1).
    older = [
        KlineBar(seq=i, ts_open=float(20 - i), open=99.0, high=100.0, low=98.0, close=99.0, volume=1)
        for i in range(12, 6, -1)
    ]
    # breakout around seq 6, then hold above
    hold = [
        KlineBar(seq=6, ts_open=6.0, open=100.2, high=101.5, low=100.0, close=101.2, volume=1),
        KlineBar(seq=5, ts_open=5.0, open=101.0, high=101.8, low=100.5, close=101.4, volume=1),
        KlineBar(seq=4, ts_open=4.0, open=101.2, high=102.0, low=100.8, close=101.6, volume=1),
        KlineBar(seq=3, ts_open=3.0, open=101.4, high=102.2, low=101.0, close=101.8, volume=1),
        KlineBar(seq=2, ts_open=2.0, open=101.5, high=102.5, low=101.1, close=102.0, volume=1),
        KlineBar(seq=1, ts_open=1.0, open=101.8, high=102.8, low=101.4, close=102.3, volume=1),
    ]
    bars = tuple(hold + older)
    features = compute_simple_market_features(_frame(*bars), lookback=12)
    assert features.breakout_quality in ("surviving", "close_breakout", "testing")
    text = render_simple_market_features(features)
    assert "突破质量" in text
    assert "尖峰余波" in text


def test_scale_conflict_and_render_keys() -> None:
    # Recent rising, older falling → conflict when enough bars
    bars = []
    # seq 1..20 recent up (newest first in frame)
    for i in range(1, 21):
        px = 120 - i * 0.5  # newer higher
        bars.append(KlineBar(seq=i, ts_open=float(i), open=px - 0.2, high=px + 0.5, low=px - 0.5, close=px, volume=1))
    # seq 41..80 background: make older window net down when indexed as bars[40:]
    for i in range(21, 81):
        # older bars have higher prices so bg net (bg[0]-bg[-1]) is negative when newest-first
        px = 100 + (i - 21) * 0.4
        bars.append(KlineBar(seq=i, ts_open=float(i), open=px, high=px + 0.3, low=px - 0.3, close=px, volume=1))
    features = compute_simple_market_features(_frame(*tuple(bars)), lookback=80)
    assert features.scale_conflict is True
    pf_keys = {
        "breakout_quality",
        "mm_as_tp_ok",
        "spike_aftermath_hint",
        "scale_conflict",
    }
    from pa_agent.ai.market_features import build_program_features_dict

    d = build_program_features_dict(_frame(*tuple(bars)))
    assert pf_keys <= set(d)


def test_measured_move_range_projection() -> None:
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(i), open=105.0, high=110.0, low=100.0, close=105.0, volume=1)
        for i in range(6)
    )
    features = compute_simple_market_features(_frame(*bars), lookback=6)
    range_up = [m for m in features.measured_moves if m.kind == "range_up"]
    assert range_up
    assert range_up[0].height == 10.0
    assert range_up[0].target_price == 120.0


def test_render_includes_key_sections() -> None:
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(i), open=105.0, high=110.0, low=100.0, close=108.0, volume=1)
        for i in range(6)
    )
    text = render_simple_market_features(compute_simple_market_features(_frame(*bars)))
    assert "程序结构辅助特征" in text
    assert "区间位置" in text
    assert "H/L 计数" in text
    assert "Measured Move" in text


def test_breakout_events_deduped_per_level() -> None:
    from pa_agent.ai.market_features import BreakoutEvent, _dedupe_breakout_events

    events = [
        BreakoutEvent(100.0, "range_high", "test", 8, "K10-K8", "a"),
        BreakoutEvent(100.0, "range_high", "test", 5, "K10-K5", "b"),
        BreakoutEvent(100.0, "range_high", "test", 3, "K10-K3", "c"),
        BreakoutEvent(90.0, "range_low", "test", 4, "K10-K4", "d"),
    ]
    deduped = _dedupe_breakout_events(events)
    highs = [e for e in deduped if e.level_kind == "range_high"]
    assert len(highs) == 1
    assert highs[0].trigger_seq == 3
    assert len(deduped) == 2
