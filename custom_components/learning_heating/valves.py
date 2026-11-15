"""Learns how far above its setpoint each valve stops calling for heat.

Thermostatic valves differ: most close about a degree above their setpoint, but some
stay partly open several degrees above it and keep a zone (and the boiler) running
for a room that is already warm enough.
"""

from __future__ import annotations

from collections import defaultdict

DEMAND_PCT = 10.0  # a zone is typically called above this opening
BIN = 0.5
MIN_BIN_H = 1.0
MIN_TOTAL_H = 24.0
SAMPLE_CAP_S = 7200.0  # readings are sparse; don't trust one for longer than this
MAX_GAP = 6.0

Sample = tuple[float, float | None, float | None, float | None]


def _median(items: list[tuple[float, float]]) -> float:
    items = sorted(items)
    half = sum(weight for _, weight in items) / 2
    seen = 0.0
    for value, weight in items:
        seen += weight
        if seen >= half:
            return value
    return items[-1][0]


def shut_gap(samples: list[Sample]) -> float | None:
    """The smallest (reading - setpoint) from which the valve stays at or below DEMAND_PCT.

    `samples` are time-ordered (timestamp, reading, setpoint, valve %). Returns None
    until there is enough history to tell.
    """
    bins: dict[float, list[tuple[float, float]]] = defaultdict(list)
    total = 0.0
    for (t0, temp, setpoint, valve), (t1, *_rest) in zip(samples, samples[1:]):
        if temp is None or setpoint is None or valve is None:
            continue
        weight = min(t1 - t0, SAMPLE_CAP_S)
        if weight <= 0:
            continue
        gap = max(-1.0, min(MAX_GAP, round((temp - setpoint) / BIN) * BIN))
        bins[gap].append((valve, weight))
        total += weight
    if total < MIN_TOTAL_H * 3600:
        return None

    lowest_shut = None
    for gap in sorted(bins, reverse=True):
        items = bins[gap]
        if sum(weight for _, weight in items) < MIN_BIN_H * 3600:
            continue
        if _median(items) > DEMAND_PCT:
            # Just above the highest gap seen calling for heat: the least it can be,
            # so thin data in between never makes the correction too big.
            return None if lowest_shut is None else gap + BIN
        lowest_shut = gap
    return lowest_shut
