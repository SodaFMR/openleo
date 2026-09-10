"""P.676-13 Annex 1 paths through the P.835-7 global reference atmosphere.

Heights are geometric kilometres above mean sea level, on the recommendation's
mean sphere of radius 6 371 km. This is a reference atmosphere, not local weather.
Columns evaluate the profile and line spectra once and can be reused within a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, atan2, ceil, cos, expm1, floor, hypot, log1p, radians, sin, sqrt

import numpy as np
from numpy.typing import NDArray

from openleo.gases import (
    MAX_FREQUENCY_HZ,
    MIN_FREQUENCY_HZ,
    _validate_finite,
    specific_gaseous_attenuation,
)
from openleo.reference_atmosphere import reference_atmosphere

EARTH_RADIUS_KM = 6_371.0
SPEED_OF_LIGHT_M_S = 299_792_458.0


@dataclass(frozen=True, slots=True)
class SlantPath:
    dry_air_db: float
    water_vapour_db: float
    total_db: float
    bending_rad: float
    refractive_excess_path_m: float
    geometric_path_m: float
    central_angle_rad: float


@dataclass(frozen=True, slots=True)
class GroundPathResult:
    dry_air_db: float
    water_vapour_db: float
    total_db: float
    apparent_elevation_deg: float
    excess_delay_s: float


@dataclass(frozen=True, slots=True)
class ReferenceColumn:
    frequency_hz: float
    lower_height_km: float
    upper_height_km: float
    refinement: int
    radii_km: NDArray[np.float64]
    thicknesses_km: NDArray[np.float64]
    refractive_indices: NDArray[np.float64]
    dry_air_db_per_km: NDArray[np.float64]
    water_vapour_db_per_km: NDArray[np.float64]

    def _impact_km(self, elevation_deg: float) -> float:
        return (
            0.0
            if elevation_deg == 90.0
            else float(self.refractive_indices[0] * self.radii_km[0]) * cos(radians(elevation_deg))
        )

    def _incidence_sines(self, impact_km: float) -> tuple[NDArray, NDArray]:
        top_radii = self.radii_km + self.thicknesses_km
        entry_sines = impact_km / (self.refractive_indices * self.radii_km)
        exit_sines = impact_km / (self.refractive_indices * top_radii)
        if np.any(entry_sines > 1.0) or np.any(exit_sines > 1.0):
            raise ValueError("refracted path is trapped or outside the ascending-ray domain")
        return entry_sines, exit_sines

    def _trace(self, impact_km: float) -> tuple[NDArray, NDArray, NDArray]:
        entry_sines, exit_sines = self._incidence_sines(impact_km)
        top_radii = self.radii_km + self.thicknesses_km
        # Rationalized Eq. 17 avoids subtracting nearly equal Earth-scale lengths.
        entry_distances = self.radii_km * np.sqrt((1 - entry_sines) * (1 + entry_sines))
        exit_distances = top_radii * np.sqrt((1 - exit_sines) * (1 + exit_sines))
        lengths = (
            self.thicknesses_km
            * (2 * self.radii_km + self.thicknesses_km)
            / (entry_distances + exit_distances)
        )
        return lengths, np.arcsin(entry_sines), np.arcsin(exit_sines)

    def at_apparent_elevation(self, elevation_deg: float) -> SlantPath:
        """Integrate Eqs. 13, 17–19, 22–23 for apparent elevation in [0, 90].

        Bending follows Eq. 22 and excludes any terminal jump to vacuum.
        Geometric path is the bent ray length within this column; Eq. 23's
        refractive excess is measured relative to that ray, not an endpoint chord.
        """
        _validate_finite(elevation_deg, "elevation_deg")
        if not 0.0 <= elevation_deg <= 90.0:
            raise ValueError("apparent elevation_deg must be between 0 and 90 inclusive")
        lengths, beta, alpha = self._trace(self._impact_km(elevation_deg))
        dry = float(np.dot(lengths, self.dry_air_db_per_km))
        water = float(np.dot(lengths, self.water_vapour_db_per_km))
        return SlantPath(
            dry_air_db=dry,
            water_vapour_db=water,
            total_db=dry + water,
            bending_rad=float(np.sum(beta[1:] - alpha[:-1])),
            refractive_excess_path_m=float(np.dot(lengths, self.refractive_indices - 1)) * 1_000,
            geometric_path_m=float(np.sum(lengths)) * 1_000,
            central_angle_rad=float(np.sum(beta - alpha)),
        )

    def _endpoint_angle(self, elevation_deg: float, target_radius: float) -> float:
        impact = self._impact_km(elevation_deg)
        top_radius = EARTH_RADIUS_KM + self.upper_height_km
        if impact > top_radius:
            raise ValueError("refracted ray cannot continue into vacuum above 100 km")
        entry_sines, exit_sines = self._incidence_sines(impact)
        central_angle = float(np.sum(np.arcsin(entry_sines) - np.arcsin(exit_sines)))
        return central_angle + asin(impact / top_radius) - asin(impact / target_radius)

    def for_geometry(self, geometric_elevation_deg: float, range_m: float) -> GroundPathResult:
        """Solve the refracted ray to a geometric endpoint above the 100 km column.

        The mean-sphere endpoint is specified by unrefracted elevation and chord
        range. Excess delay includes both the bent ray's extra geometric length
        and refractive excess, using non-dispersive P.453 refractivity.
        """
        _validate_finite(geometric_elevation_deg, "geometric_elevation_deg")
        _validate_finite(range_m, "range_m")
        if not 5.0 <= geometric_elevation_deg <= 90.0:
            raise ValueError("geometric_elevation_deg must be between 5 and 90 inclusive")
        if range_m <= 0.0:
            raise ValueError("range_m must be positive")
        if self.upper_height_km != 100.0:
            raise ValueError("ground-to-space geometry requires a column ending at 100 km")
        radius = EARTH_RADIUS_KM + self.lower_height_km
        elevation_rad = radians(geometric_elevation_deg)
        radial = radius + range_m / 1_000 * sin(elevation_rad)
        transverse = range_m / 1_000 * cos(elevation_rad)
        target_radius = hypot(radial, transverse)
        top_radius = EARTH_RADIUS_KM + self.upper_height_km
        if target_radius <= top_radius:
            raise ValueError("geometric endpoint must be above the 100 km atmospheric column")
        target_angle = atan2(transverse, radial)
        apparent = self._solve_elevation(geometric_elevation_deg, target_radius, target_angle)
        path = self.at_apparent_elevation(apparent)
        impact = self._impact_km(apparent)
        # Rationalize the vacuum chord segment just as for atmospheric layers.
        vacuum_km = (target_radius - top_radius) * (
            (target_radius + top_radius)
            / (
                target_radius * sqrt(1 - (impact / target_radius) ** 2)
                + top_radius * sqrt(1 - (impact / top_radius) ** 2)
            )
        )
        excess_m = (
            path.geometric_path_m + vacuum_km * 1_000 - range_m + path.refractive_excess_path_m
        )
        roundoff_m = 64 * np.finfo(float).eps * range_m
        if excess_m < -roundoff_m:
            raise ValueError("endpoint solve produced a negative excess optical path")
        return GroundPathResult(
            dry_air_db=path.dry_air_db,
            water_vapour_db=path.water_vapour_db,
            total_db=path.total_db,
            apparent_elevation_deg=apparent,
            excess_delay_s=float(max(0.0, excess_m)) / SPEED_OF_LIGHT_M_S,
        )

    def _solve_elevation(self, lower: float, target_radius: float, target_angle: float) -> float:
        if lower == 90.0:
            return lower
        residual = self._endpoint_angle(lower, target_radius) - target_angle
        roundoff_rad = 64 * np.finfo(float).eps
        if residual < -roundoff_rad:
            raise ValueError("geometric endpoint is not bracketed by the ascending refracted ray")
        if abs(residual) <= roundoff_rad:
            return lower
        upper = 90.0
        for _ in range(40):
            midpoint = (lower + upper) / 2
            if self._endpoint_angle(midpoint, target_radius) > target_angle:
                lower = midpoint
            else:
                upper = midpoint
        return (lower + upper) / 2


def _readonly(values) -> NDArray[np.float64]:
    """Use immutable bytes as storage, so callers cannot re-enable array writes."""
    return np.frombuffer(np.asarray(values, dtype=np.float64).tobytes(), dtype=np.float64)


def _layer_boundaries(lower: float, upper: float, refinement: int) -> NDArray[np.float64]:
    # Eq. 16a–d, normalized to both requested endpoints. expm1 avoids cancellation.
    scale = 1e4 * expm1(0.01)
    first = floor(100 * log1p(scale * lower) + 1)
    last = ceil(100 * log1p(scale * upper) + 1)
    count = last - first
    if count < 1:
        raise ValueError("height interval is too small to resolve atmospheric layers")
    base = lower + (upper - lower) * np.expm1(np.arange(count + 1) / 100) / expm1(count / 100)
    boundaries = base[:-1, None] + np.diff(base)[:, None] * np.arange(refinement) / refinement
    return np.append(boundaries.ravel(), upper)


def build_reference_column(
    frequency_hz: float,
    lower_height_km: float,
    upper_height_km: float = 100.0,
    refinement: int = 1,
) -> ReferenceColumn:
    """Prepare immutable midpoint states and attenuation on normalized Eq. 16 layers.

    Refinement 2 or 4 splits every base layer evenly and reevaluates its midpoint.
    P.676 cautions that intervals covering fewer than 50 base layers may be less
    accurate. No process-global cache is used; callers own each column's lifetime.
    """
    for value, name in (
        (frequency_hz, "frequency_hz"),
        (lower_height_km, "lower_height_km"),
        (upper_height_km, "upper_height_km"),
    ):
        _validate_finite(value, name)
    if not MIN_FREQUENCY_HZ <= frequency_hz <= MAX_FREQUENCY_HZ:
        raise ValueError("frequency_hz must be between 1e9 and 1e12 inclusive")
    if not 0.0 <= lower_height_km < upper_height_km <= 100.0:
        raise ValueError("heights must satisfy 0 <= lower_height_km < upper_height_km <= 100")
    if (
        isinstance(refinement, bool)
        or not isinstance(refinement, int)
        or refinement not in (1, 2, 4)
    ):
        raise ValueError("refinement must be the integer 1, 2, or 4")
    boundaries = _layer_boundaries(lower_height_km, upper_height_km, refinement)
    thicknesses = np.diff(boundaries)
    radii = EARTH_RADIUS_KM + boundaries
    # Subtract shared radii so r_i + delta_i is exactly r_(i+1). Mixing a
    # height-derived delta with rounded Earth radii creates false interface bends.
    radial_thicknesses = np.diff(radii)
    if np.any(thicknesses <= 0) or np.any(radial_thicknesses <= 0):
        raise ValueError("height interval is too small to resolve atmospheric layers")
    states = tuple(
        reference_atmosphere(float(height)) for height in boundaries[:-1] + thicknesses / 2
    )
    attenuation = tuple(
        specific_gaseous_attenuation(
            frequency_hz,
            state.dry_pressure_hpa,
            state.temperature_k,
            state.water_vapour_density_g_m3,
        )
        for state in states
    )
    return ReferenceColumn(
        frequency_hz=float(frequency_hz),
        lower_height_km=float(lower_height_km),
        upper_height_km=float(upper_height_km),
        refinement=refinement,
        radii_km=_readonly(radii[:-1]),
        thicknesses_km=_readonly(radial_thicknesses),
        refractive_indices=_readonly([state.refractive_index for state in states]),
        dry_air_db_per_km=_readonly([value.dry_air_db_per_km for value in attenuation]),
        water_vapour_db_per_km=_readonly([value.water_vapour_db_per_km for value in attenuation]),
    )
