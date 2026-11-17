"""What the weather means for a room: how much sun gets in and how hard the wind blows."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# How much of the sun's warmth gets through for each sky, 0..1.
CLEARNESS = {
    "sunny": 1.0,
    "clear-night": 1.0,
    "windy": 0.8,
    "partlycloudy": 0.6,
    "windy-variant": 0.5,
    "exceptional": 0.5,
    "cloudy": 0.25,
    "fog": 0.15,
    "rainy": 0.15,
    "snowy": 0.15,
    "hail": 0.1,
    "lightning": 0.1,
    "lightning-rainy": 0.1,
    "pouring": 0.1,
    "snowy-rainy": 0.1,
}
DEFAULT_CLEARNESS = 0.5
KMH = {"km/h": 1.0, "m/s": 3.6, "mph": 1.609, "kn": 1.852, "ft/s": 1.097}


def sun_height(latitude: float, longitude: float, timestamp: float) -> float:
    """Sine of the sun's height above the horizon, or 0 when it is down."""
    when = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    equation = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    solar_minutes = hour * 60 + equation + 4 * longitude
    hour_angle = math.radians(solar_minutes / 4 - 180)
    lat = math.radians(latitude)
    height = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    return max(0.0, height)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sky(condition: str | None, attrs: dict[str, Any]) -> tuple[float, float]:
    """(how clear the sky is 0..1, wind in units of 10 km/h) from a weather state or forecast item."""
    clear = CLEARNESS.get(str(condition), DEFAULT_CLEARNESS)
    cover = _float(attrs.get("cloud_coverage"))
    if cover is not None:
        clear = max(0.1, 1 - 0.85 * min(100.0, max(0.0, cover)) / 100)
    speed = _float(attrs.get("wind_speed"))
    wind = 0.0
    if speed is not None:
        wind = max(0.0, speed * KMH.get(str(attrs.get("wind_speed_unit", "km/h")), 1.0) / 10)
    return clear, wind
