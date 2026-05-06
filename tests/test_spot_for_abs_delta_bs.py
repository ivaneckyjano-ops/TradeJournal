"""Test spot inversion z core.greeks.spot_for_abs_delta_bs."""

from core.greeks import bs_delta_raw, spot_for_abs_delta_bs


def test_spot_matches_delta_call_midrange():
    strike = 170.0
    dte = 45
    iv = 0.35
    tgt = 0.375  # 1.5 * 0.25
    s = spot_for_abs_delta_bs(strike, dte, iv, "C", tgt)
    assert s is not None and s > 0
    T = dte / 365.0
    got = bs_delta_raw(s, strike, T, iv, "C")
    assert got is not None
    assert abs(abs(got) - tgt) < 5e-4


def test_spot_matches_delta_put():
    strike = 100.0
    dte = 30
    iv = 0.40
    tgt = 0.30
    s = spot_for_abs_delta_bs(strike, dte, iv, "Put", tgt)
    assert s is not None
    T = dte / 365.0
    got = bs_delta_raw(s, strike, T, iv, "P")
    assert got is not None
    assert abs(abs(got) - tgt) < 5e-4


def test_none_when_target_oob():
    assert spot_for_abs_delta_bs(100, 30, 0.3, "C", 1.2) is None
    assert spot_for_abs_delta_bs(100, 30, 0.3, "C", 0.0) is None


def test_zero_dte_uses_minimum_horizon():
    """Kalendárny DTE 0 → vnútri max(1,0)=1 deň pre BS."""
    s = spot_for_abs_delta_bs(170.0, 0, 0.35, "C", 0.525)
    assert s is not None and s > 0
    T = 1.0 / 365.0
    got = bs_delta_raw(s, 170.0, T, 0.35, "C")
    assert got is not None
    assert abs(abs(got) - 0.525) < 2e-3


def test_negative_dte_returns_none():
    assert spot_for_abs_delta_bs(100, -1, 0.3, "C", 0.5) is None
