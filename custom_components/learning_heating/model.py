"""Room thermal model learnt online from sparse temperature reports.

    dR/dt = (u - R) / tau                 radiator warmth, lagging the valve
    dT/dt = a * R - (k + kw * wind) * (T - T_out) + g + s * sun   room temperature

u is the effective valve opening (0..1, zero while the zone pump is off), a the
warm-up rate with a fully hot radiator (degC/h), k the heat-loss coefficient
(1/h) and g steady gains from sun, people and appliances (degC/h). With tau
fixed, integrating between two temperature reports gives one linear sample in
(a, k, g, s, kw) however far apart the reports are, fitted by recursive least squares.
`sun` (0..1) is the sun's height times how clear the sky is, so s is the warmth the
room gets from full sun; `wind` is the wind speed in units of 10 km/h, so kw is how
much a breeze adds to the heat loss.
A few radiator lags are learnt side by side and the best-predicting one is used.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

LAGS_H = (0.0, 0.5, 1.5)
DEFAULT_LAG_H = 0.5
BOUNDS = ((0.1, 15.0), (0.01, 1.0), (-0.5, 2.0), (0.0, 3.0), (0.0, 0.3))
INITIAL = (1.5, 0.08, 0.0, 0.0, 0.0)
INITIAL_VARIANCE = (4.0, 0.01, 0.1, 0.5, 0.0004)
N = len(INITIAL)
FORGETTING = 0.997
MIN_INTERVAL_H = 0.15
MAX_INTERVAL_H = 6.0
MAX_ERROR = 1.5
TRAINED_SAMPLES = 20
STEP_H = 1 / 12

OutdoorFn = Callable[[float], float]


def _lag(r: float, u: float, hours: float, tau: float) -> tuple[float, float]:
    """Radiator state after `hours` at constant valve u, and its integral over the period."""
    if tau <= 0:
        return u, u * hours
    decay = math.exp(-hours / tau)
    return u + (r - u) * decay, u * hours + (r - u) * tau * (1 - decay)


@dataclass
class RoomModel:
    lag: float = DEFAULT_LAG_H
    theta: list[float] = field(default_factory=lambda: list(INITIAL))
    p: list[list[float]] = field(
        default_factory=lambda: [
            [INITIAL_VARIANCE[i] if i == j else 0.0 for j in range(N)] for i in range(N)
        ]
    )
    samples: int = 0
    mse: float | None = None

    @property
    def a(self) -> float:
        return self.theta[0]

    @property
    def k(self) -> float:
        return self.theta[1]

    @property
    def gains(self) -> float:
        return self.theta[2]

    @property
    def sun_gain(self) -> float:
        return self.theta[3]

    @property
    def wind_loss(self) -> float:
        return self.theta[4]

    @property
    def trained(self) -> bool:
        return self.samples >= TRAINED_SAMPLES

    @property
    def rms_error(self) -> float | None:
        return None if self.mse is None else math.sqrt(self.mse)

    def update(
        self, x_heat: float, x_loss: float, hours: float, rise: float,
        x_sun: float = 0.0, x_wind: float = 0.0,
    ) -> float:
        """RLS step for rise = a*x_heat - k*x_loss + g*hours + s*x_sun - kw*x_wind.

        Returns the a-priori error."""
        phi = (x_heat, -x_loss, hours, x_sun, -x_wind)
        p = self.p
        p_phi = [sum(p[i][j] * phi[j] for j in range(N)) for i in range(N)]
        denom = FORGETTING + sum(phi[i] * p_phi[i] for i in range(N))
        gain = [v / denom for v in p_phi]
        error = rise - sum(self.theta[i] * phi[i] for i in range(N))
        clipped = max(-MAX_ERROR, min(MAX_ERROR, error))
        for i in range(N):
            lo, hi = BOUNDS[i]
            self.theta[i] = max(lo, min(hi, self.theta[i] + gain[i] * clipped))
        self.p = [
            [(p[i][j] - gain[i] * p_phi[j]) / FORGETTING for j in range(N)]
            for i in range(N)
        ]
        if self.samples >= 5:
            sq = min(error * error, 9.0)
            self.mse = sq if self.mse is None else 0.97 * self.mse + 0.03 * sq
        self.samples += 1
        return error

    def step(
        self, temp: float, radiator: float, hours: float, u: float, outdoor: float,
        sun: float = 0.0, wind: float = 0.0,
    ) -> tuple[float, float]:
        radiator_end, heat = _lag(radiator, u, hours, self.lag)
        mean_radiator = heat / hours if hours > 0 else radiator
        k = self.k + self.wind_loss * wind
        equilibrium = outdoor + (self.a * mean_radiator + self.gains + self.sun_gain * sun) / k
        temp = equilibrium + (temp - equilibrium) * math.exp(-k * hours)
        return temp, radiator_end

    def simulate(
        self, temp: float, radiator: float, hours: float, u: float, outdoor_at: OutdoorFn,
        sun_at: OutdoorFn | None = None, wind_at: OutdoorFn | None = None,
    ) -> float:
        elapsed = 0.0
        while elapsed < hours:
            dt = min(STEP_H, hours - elapsed)
            mid = elapsed + dt / 2
            temp, radiator = self.step(
                temp, radiator, dt, u, outdoor_at(mid),
                sun_at(mid) if sun_at else 0.0, wind_at(mid) if wind_at else 0.0,
            )
            elapsed += dt
        return temp

    def hours_to_reach(
        self,
        temp: float,
        radiator: float,
        target: float,
        outdoor_at: OutdoorFn,
        max_hours: float,
        sun_at: OutdoorFn | None = None,
        wind_at: OutdoorFn | None = None,
    ) -> float | None:
        """Hours of full heating needed to reach target, or None if not within max_hours."""
        elapsed = 0.0
        while temp < target:
            if elapsed >= max_hours:
                return None
            mid = elapsed + STEP_H / 2
            temp, radiator = self.step(
                temp, radiator, STEP_H, 1.0, outdoor_at(mid),
                sun_at(mid) if sun_at else 0.0, wind_at(mid) if wind_at else 0.0,
            )
            elapsed += STEP_H
        return elapsed

    def as_dict(self) -> dict:
        return {
            "lag": self.lag,
            "theta": self.theta,
            "p": self.p,
            "samples": self.samples,
            "mse": self.mse,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoomModel:
        theta = list(data["theta"])
        p = [list(row) for row in data["p"]]
        # Models saved before the sun and wind terms existed have three parameters.
        known = len(theta)
        if known < N:
            theta += list(INITIAL[known:])
            p = [
                (p[i][:known] if i < known else [0.0] * known)
                + [0.0] * (N - known)
                for i in range(N)
            ]
            for i in range(known, N):
                p[i][i] = INITIAL_VARIANCE[i]
        return cls(
            lag=data["lag"],
            theta=theta,
            p=p,
            samples=data["samples"],
            mse=data.get("mse"),
        )


class Tracker:
    """Integrates valve and outdoor temperature between temperature reports for one model."""

    def __init__(self, model: RoomModel) -> None:
        self.model = model
        self.u = 0.0
        self.radiator = 0.0
        self.outdoor: float | None = None
        self.time: float | None = None
        self.last_temp: float | None = None
        self.last_temp_time: float | None = None
        self.estimate: float | None = None
        self.sun = 0.0
        self.wind = 0.0
        self._heat = 0.0
        self._outdoor_integral = 0.0
        self._sun_integral = 0.0
        self._wind_integral = 0.0
        self._covered = 0.0

    def advance(self, now: float) -> None:
        if self.time is not None and now > self.time:
            hours = (now - self.time) / 3600
            if self.last_temp is not None and self.outdoor is not None:
                self.estimate, _ = self.model.step(
                    self.estimate, self.radiator, hours, self.u, self.outdoor,
                    self.sun, self.wind,
                )
                self._outdoor_integral += self.outdoor * hours
                self._sun_integral += self.sun * hours
                self._wind_integral += self.wind * hours
                self._covered += hours
            self.radiator, heat = _lag(self.radiator, self.u, hours, self.model.lag)
            self._heat += heat
        if self.time is None or now > self.time:
            self.time = now

    def set_valve(self, now: float, u: float) -> None:
        self.advance(now)
        self.u = u

    def set_outdoor(self, now: float, outdoor: float) -> None:
        self.advance(now)
        self.outdoor = outdoor

    def set_sky(self, now: float, sun: float, wind: float) -> None:
        self.advance(now)
        self.sun = sun
        self.wind = wind

    def observe(self, now: float, temp: float) -> float | None:
        """Record a temperature report; returns the a-priori error if it produced a sample."""
        self.advance(now)
        error = None
        if self.last_temp is not None and self.last_temp_time is not None:
            hours = (now - self.last_temp_time) / 3600
            if MIN_INTERVAL_H <= hours <= MAX_INTERVAL_H and self._covered >= 0.95 * hours:
                outdoor_mean = self._outdoor_integral / self._covered
                x_loss = ((self.last_temp + temp) / 2 - outdoor_mean) * hours
                wind_mean = self._wind_integral / self._covered
                error = self.model.update(
                    self._heat, x_loss, hours, temp - self.last_temp,
                    self._sun_integral, x_loss * wind_mean,
                )
        self.last_temp = temp
        self.last_temp_time = now
        self.estimate = temp
        self._heat = 0.0
        self._outdoor_integral = 0.0
        self._sun_integral = 0.0
        self._wind_integral = 0.0
        self._covered = 0.0
        return error


class RoomLearner:
    """Learns one model per candidate radiator lag and predicts with the best of them."""

    def __init__(
        self, models: list[RoomModel] | None = None, naive_mse: float | None = None
    ) -> None:
        self.models = models or [RoomModel(lag=lag) for lag in LAGS_H]
        self.trackers = [Tracker(m) for m in self.models]
        self.naive_mse = naive_mse

    @property
    def reliable(self) -> bool:
        """True once the best model predicts reports better than assuming no change."""
        model = self.model
        return (
            model.trained
            and model.mse is not None
            and self.naive_mse is not None
            and model.mse < self.naive_mse
        )

    @property
    def best(self) -> Tracker:
        scored = [t for t in self.trackers if t.model.trained and t.model.mse is not None]
        if scored:
            return min(scored, key=lambda t: t.model.mse)
        return next(t for t in self.trackers if t.model.lag == DEFAULT_LAG_H)

    @property
    def model(self) -> RoomModel:
        return self.best.model

    @property
    def estimate(self) -> float | None:
        return self.best.estimate

    @property
    def last_temp(self) -> float | None:
        return self.trackers[0].last_temp

    @property
    def last_temp_time(self) -> float | None:
        return self.trackers[0].last_temp_time

    def advance(self, now: float) -> None:
        for t in self.trackers:
            t.advance(now)

    def set_valve(self, now: float, u: float) -> None:
        for t in self.trackers:
            t.set_valve(now, u)

    def set_outdoor(self, now: float, outdoor: float) -> None:
        for t in self.trackers:
            t.set_outdoor(now, outdoor)

    def set_sky(self, now: float, sun: float, wind: float) -> None:
        for t in self.trackers:
            t.set_sky(now, sun, wind)

    def observe(self, now: float, temp: float) -> None:
        first = self.trackers[0]
        if first.last_temp is not None and first.last_temp_time is not None:
            hours = (now - first.last_temp_time) / 3600
            if MIN_INTERVAL_H <= hours <= MAX_INTERVAL_H:
                sq = min((temp - first.last_temp) ** 2, 9.0)
                self.naive_mse = sq if self.naive_mse is None else 0.97 * self.naive_mse + 0.03 * sq
        for t in self.trackers:
            t.observe(now, temp)

    def hours_to_reach(
        self, target: float, outdoor_at: OutdoorFn, max_hours: float,
        sun_at: OutdoorFn | None = None, wind_at: OutdoorFn | None = None,
    ) -> float | None:
        best = self.best
        if best.estimate is None:
            return None
        return best.model.hours_to_reach(
            best.estimate, best.radiator, target, outdoor_at, max_hours, sun_at, wind_at
        )

    def predict(
        self, hours: float, u: float, outdoor_at: OutdoorFn,
        sun_at: OutdoorFn | None = None, wind_at: OutdoorFn | None = None,
    ) -> float | None:
        best = self.best
        if best.estimate is None:
            return None
        return best.model.simulate(best.estimate, best.radiator, hours, u, outdoor_at, sun_at, wind_at)

    def as_dict(self) -> dict:
        return {"models": [m.as_dict() for m in self.models], "naive_mse": self.naive_mse}

    @classmethod
    def from_dict(cls, data: dict) -> RoomLearner:
        return cls([RoomModel.from_dict(m) for m in data["models"]], data.get("naive_mse"))
