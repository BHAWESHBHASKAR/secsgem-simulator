"""Physics-lite model of an etch chamber's temperature and pressure.

Each value follows a first-order lag towards a target:

    value += (target - value) * (1 - exp(-dt / tau)) + noise

i.e. it moves a fixed fraction of the remaining gap every tick, the way a real
heater or pressure controller settles. That is enough to make values drift,
overshoot thresholds and trigger alarms in a believable way.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

AMBIENT_TEMP_C = 25.0
BASE_PRESSURE_MTORR = 5.0   # chamber pumped down, no process gas


@dataclass
class ChamberModel:
    temperature: float = AMBIENT_TEMP_C   # degC
    pressure: float = BASE_PRESSURE_MTORR  # mTorr
    temp_tau: float = 1.2                 # seconds to cover ~63% of the gap
    pressure_tau: float = 0.5
    temp_noise: float = 0.15              # std dev of random noise per tick
    pressure_noise: float = 0.3
    seed: Optional[int] = None
    rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def step(self, dt: float, temp_target: float, pressure_target: float) -> None:
        self.temperature = self._approach(
            self.temperature, temp_target, dt, self.temp_tau, self.temp_noise)
        self.pressure = max(0.0, self._approach(
            self.pressure, pressure_target, dt, self.pressure_tau, self.pressure_noise))

    def _approach(self, value: float, target: float, dt: float, tau: float, noise: float) -> float:
        fraction = 1.0 - math.exp(-dt / tau)
        return value + (target - value) * fraction + self.rng.gauss(0.0, noise)
