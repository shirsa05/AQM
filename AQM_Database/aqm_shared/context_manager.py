"""
Python port of the C++ ContextManager (codes/src/crypto/context_manager.h).

Device-aware coin tier selection based on battery, WiFi, and signal strength.
The C++ version used hardcoded values; this version is fully parameterized.

Decision tree:
    battery < 5%                    → BRONZE
    no WiFi + signal < -100 dBm    → BRONZE
    WiFi + battery < 20%           → BRONZE
    no WiFi + signal >= -100 dBm   → SILVER
    WiFi + 20% <= battery < 50%    → SILVER
    WiFi + battery >= 50%          → GOLD
"""

import random
from dataclasses import dataclass


@dataclass
class DeviceContext:
    """Snapshot of device state at message-send time."""
    battery_pct: float
    wifi_connected: bool
    signal_dbm: float
    label: str = ""


class ContextManager:
    """Selects the optimal coin tier based on device context."""

    def select_coin(self, ctx: DeviceContext) -> str:
        """Return 'GOLD', 'SILVER', or 'BRONZE' for the given device state."""

        # Critical battery — always conserve
        if ctx.battery_pct < 5:
            return "BRONZE"

        if not ctx.wifi_connected:
            # Cellular only
            if ctx.signal_dbm < -100:
                return "BRONZE"
            return "SILVER"

        # WiFi connected
        if ctx.battery_pct < 20:
            return "BRONZE"
        if ctx.battery_pct < 50:
            return "SILVER"
        return "GOLD"

    def is_ideal_state(self, ctx: DeviceContext) -> bool:
        """True when conditions are good enough for background maintenance."""
        return ctx.battery_pct > 20 and ctx.wifi_connected


# ─── Pre-defined demo scenarios ───

SCENARIO_A = DeviceContext(
    battery_pct=80,
    wifi_connected=True,
    signal_dbm=-50,
    label="Home WiFi, fully charged",
)

SCENARIO_B = DeviceContext(
    battery_pct=40,
    wifi_connected=False,
    signal_dbm=-85,
    label="Outdoor, cellular only",
)

SCENARIO_C = DeviceContext(
    battery_pct=3,
    wifi_connected=False,
    signal_dbm=-120,
    label="Underground, critical battery",
)

SCENARIOS = [SCENARIO_A, SCENARIO_B, SCENARIO_C]


def random_context() -> DeviceContext:
    """Generate a random DeviceContext simulating real-world fluctuation.

    Battery: uniform 0-100%
    WiFi: 60% chance True (biased toward connected)
    Signal: uniform -130 to -40 dBm
    """
    battery = round(random.uniform(0, 100), 1)
    wifi = random.random() < 0.6
    signal = round(random.uniform(-130, -40), 1)
    label = (
        f"bat={battery:.0f}%, "
        f"{'WiFi' if wifi else 'cell'}, "
        f"sig={signal:.0f}dBm"
    )
    return DeviceContext(
        battery_pct=battery,
        wifi_connected=wifi,
        signal_dbm=signal,
        label=label,
    )
