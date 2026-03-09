"""Tests for DeviceContext, ContextManager, and pre-defined scenarios."""

import pytest

from AQM_Database.aqm_shared.context_manager import (
    ContextManager,
    DeviceContext,
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
    SCENARIOS,
)


@pytest.fixture
def cm():
    return ContextManager()


# ─── Decision paths (6 branches) ───

def test_critical_battery_returns_bronze(cm):
    """battery < 5% → BRONZE regardless of other conditions."""
    ctx = DeviceContext(battery_pct=3, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "BRONZE"


def test_no_wifi_weak_signal_returns_bronze(cm):
    """no WiFi + signal < -100 dBm → BRONZE."""
    ctx = DeviceContext(battery_pct=80, wifi_connected=False, signal_dbm=-110)
    assert cm.select_coin(ctx) == "BRONZE"


def test_wifi_low_battery_returns_bronze(cm):
    """WiFi + battery < 20% → BRONZE."""
    ctx = DeviceContext(battery_pct=15, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "BRONZE"


def test_no_wifi_decent_signal_returns_silver(cm):
    """no WiFi + signal >= -100 dBm → SILVER."""
    ctx = DeviceContext(battery_pct=60, wifi_connected=False, signal_dbm=-85)
    assert cm.select_coin(ctx) == "SILVER"


def test_wifi_mid_battery_returns_silver(cm):
    """WiFi + 20% <= battery < 50% → SILVER."""
    ctx = DeviceContext(battery_pct=35, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "SILVER"


def test_wifi_high_battery_returns_gold(cm):
    """WiFi + battery >= 50% → GOLD."""
    ctx = DeviceContext(battery_pct=80, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "GOLD"


# ─── Boundary conditions ───

def test_battery_exactly_5_not_critical(cm):
    """battery == 5% should NOT trigger the < 5 critical branch.
    Use no-WiFi + good signal to land in SILVER (not BRONZE)."""
    ctx = DeviceContext(battery_pct=5, wifi_connected=False, signal_dbm=-80)
    assert cm.select_coin(ctx) == "SILVER"


def test_signal_exactly_minus_100_returns_silver(cm):
    """signal == -100 dBm should NOT trigger the < -100 branch."""
    ctx = DeviceContext(battery_pct=60, wifi_connected=False, signal_dbm=-100)
    assert cm.select_coin(ctx) == "SILVER"


def test_battery_exactly_20_returns_silver(cm):
    """WiFi + battery == 20% should NOT trigger the < 20 branch."""
    ctx = DeviceContext(battery_pct=20, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "SILVER"


def test_battery_exactly_50_returns_gold(cm):
    """WiFi + battery == 50% should NOT trigger the < 50 branch."""
    ctx = DeviceContext(battery_pct=50, wifi_connected=True, signal_dbm=-50)
    assert cm.select_coin(ctx) == "GOLD"


# ─── Pre-defined scenarios ───

def test_scenario_a_is_gold(cm):
    assert cm.select_coin(SCENARIO_A) == "GOLD"


def test_scenario_b_is_silver(cm):
    assert cm.select_coin(SCENARIO_B) == "SILVER"


def test_scenario_c_is_bronze(cm):
    assert cm.select_coin(SCENARIO_C) == "BRONZE"


def test_scenarios_list_has_three():
    assert len(SCENARIOS) == 3


# ─── is_ideal_state ───

def test_ideal_state_true(cm):
    ctx = DeviceContext(battery_pct=50, wifi_connected=True, signal_dbm=-50)
    assert cm.is_ideal_state(ctx) is True


def test_ideal_state_false_low_battery(cm):
    ctx = DeviceContext(battery_pct=10, wifi_connected=True, signal_dbm=-50)
    assert cm.is_ideal_state(ctx) is False


def test_ideal_state_false_no_wifi(cm):
    ctx = DeviceContext(battery_pct=80, wifi_connected=False, signal_dbm=-50)
    assert cm.is_ideal_state(ctx) is False
