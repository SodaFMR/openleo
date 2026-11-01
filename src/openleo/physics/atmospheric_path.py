"""P.676-13 Annex 1 paths through the P.835-7 global reference atmosphere.

Heights are geometric kilometres above mean sea level, on the recommendation's
mean sphere of radius 6 371 km. This is a reference atmosphere, not local weather.
Columns evaluate the profile and line spectra once and can be reused within a run.

Endpoint solves. The scalar ``for_geometry`` (the v1 kernel, kept as the test oracle) finds
the apparent elevation by 40 bisection steps on [geometric elevation, 90], which leaves it
within about 4e-11 degrees. The array ``for_geometry_array`` solves the same equation by
safeguarded Newton iteration (``_newton_rows``) with the endpoint angle's analytic derivative,
d asin(b / s) / db = 1 / sqrt(s^2 - b^2) summed over the layers: a bracket is kept from the
residual's sign, a step leaving it is replaced by bisection, and a row stops once a step is at
most ``NEWTON_TOLERANCE_DEG`` (1e-11 degrees). Convergence is quadratic, three or four trace
evaluations instead of 41, and the result is within the residual's rounding floor (about
1e-12 degrees) of the root, so it is closer than the bisection; the kernel equivalence tests
(``tests/physics/test_path_arrays.py``: 1e-10 dB, 1e-9 degrees and 1e-13 s against the scalar
kernel) hold.

Tabulated loss. ``GasTable`` (``build_gas_table``) serves only the engine's Shannon-bound
quadrature and MODCOD root-finding: it tabulates the two layer sums, which depend on the
apparent elevation alone, as piecewise Chebyshev series, and keeps the endpoint geometry exact.
Its loss error bound is verified at construction and recorded in results; see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import asin, atan2, ceil, cos, expm1, floor, hypot, log1p, radians, sin, sqrt

import numpy as np
from numpy.polynomial.chebyshev import chebder
from numpy.typing import NDArray

from openleo.physics.gases import (
    MAX_FREQUENCY_HZ,
    MIN_FREQUENCY_HZ,
    _validate_finite,
    specific_gaseous_attenuation,
)
from openleo.physics.reference_atmosphere import reference_atmosphere

EARTH_RADIUS_KM = 6_371.0
SPEED_OF_LIGHT_M_S = 299_792_458.0
# The array endpoint solve: Newton steps until a step is at most this (degrees).
NEWTON_TOLERANCE_DEG = 1e-11
NEWTON_MAX_ITERATIONS = 60
# GasTable: piecewise Chebyshev series on [GAS_TABLE_LOW_DEG, 90] degrees of apparent
# elevation, and the largest loss error bound (dB) it accepts at its check points.
GAS_TABLE_LOW_DEG = 5.0
GAS_TABLE_SEGMENTS = 16
GAS_TABLE_DEGREE = 16
GAS_TABLE_MAX_ERROR_DB = 1e-10
# Array ray traces run in row chunks so each (rows, layers) float64 temporary stays
# near 8 MiB whatever the number of elements.
_CHUNK_ELEMENTS = 1 << 20


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
class GroundPathArrays:
    """``GroundPathResult`` fields evaluated element-wise, as read-only float64 arrays."""

    dry_air_db: NDArray[np.float64]
    water_vapour_db: NDArray[np.float64]
    total_db: NDArray[np.float64]
    apparent_elevation_deg: NDArray[np.float64]
    excess_delay_s: NDArray[np.float64]


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

    def for_geometry_array(
        self, geometric_elevation_deg: NDArray[np.float64], range_m: NDArray[np.float64]
    ) -> GroundPathArrays:
        """Evaluate ``for_geometry`` element-wise; the inputs broadcast together.

        Every element gets the scalar method's validation, its 40-step bisection on
        apparent elevation and its ray trace, in the same operation order: the
        bisection runs on (rows,) arrays and the layer sums on (rows, layers) arrays.
        Any invalid element raises the scalar method's ``ValueError``.
        """
        shape, elevation, distance, target_radius, target_angle = self._endpoints_rows(
            geometric_elevation_deg, range_m
        )
        results = np.empty((5, elevation.size))
        rows = max(1, _CHUNK_ELEMENTS // self.radii_km.size)
        for start in range(0, elevation.size, rows):
            chunk = slice(start, start + rows)
            results[:, chunk] = self._ground_path_rows(
                elevation[chunk], distance[chunk], target_radius[chunk], target_angle[chunk]
            )
        return GroundPathArrays(*(_readonly(values).reshape(shape) for values in results))

    def _endpoints_rows(self, geometric_elevation_deg, range_m):
        """``for_geometry``'s validation and endpoint, element-wise, on flattened inputs.

        Returns the broadcast shape, the flat elevations and ranges, and the endpoint's radius
        and central angle from the station.
        """
        elevation = _finite_array(geometric_elevation_deg, "geometric_elevation_deg")
        distance = _finite_array(range_m, "range_m")
        elevation, distance = np.broadcast_arrays(elevation, distance)
        shape = elevation.shape
        elevation, distance = elevation.ravel(), distance.ravel()
        if not np.all((5.0 <= elevation) & (elevation <= 90.0)):
            raise ValueError("geometric_elevation_deg must be between 5 and 90 inclusive")
        if np.any(distance <= 0.0):
            raise ValueError("range_m must be positive")
        if self.upper_height_km != 100.0:
            raise ValueError("ground-to-space geometry requires a column ending at 100 km")
        radius = EARTH_RADIUS_KM + self.lower_height_km
        elevation_rad = np.radians(elevation)
        radial = radius + distance / 1_000 * np.sin(elevation_rad)
        transverse = distance / 1_000 * np.cos(elevation_rad)
        target_radius = np.hypot(radial, transverse)
        top_radius = EARTH_RADIUS_KM + self.upper_height_km
        if np.any(target_radius <= top_radius):
            raise ValueError("geometric endpoint must be above the 100 km atmospheric column")
        target_angle = np.arctan2(transverse, radial)
        return shape, elevation, distance, target_radius, target_angle

    def _column_rows(
        self, apparent_elevation_deg: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Total gaseous loss (dB) and the column's central angle (rad) of rays launched at
        the given apparent elevations: ``_ground_path_rows``'s and ``_endpoint_angle``'s sums."""
        impact = self._impact_km_rows(apparent_elevation_deg)
        entry_sines, exit_sines = self._incidence_sines_rows(impact)
        top_radii = self.radii_km + self.thicknesses_km
        entry_distances = self.radii_km * np.sqrt((1 - entry_sines) * (1 + entry_sines))
        exit_distances = top_radii * np.sqrt((1 - exit_sines) * (1 + exit_sines))
        lengths = (
            self.thicknesses_km
            * (2 * self.radii_km + self.thicknesses_km)
            / (entry_distances + exit_distances)
        )
        total = lengths @ self.dry_air_db_per_km + lengths @ self.water_vapour_db_per_km
        central = np.sum(np.arcsin(entry_sines) - np.arcsin(exit_sines), axis=1)
        return total, central

    def _ground_path_rows(
        self,
        geometric_elevation_deg: NDArray[np.float64],
        range_m: NDArray[np.float64],
        target_radius: NDArray[np.float64],
        target_angle: NDArray[np.float64],
    ) -> tuple[NDArray[np.float64], ...]:
        apparent = self._solve_elevation_rows(geometric_elevation_deg, target_radius, target_angle)
        # The trace and the excess path repeat at_apparent_elevation and for_geometry.
        impact = self._impact_km_rows(apparent)
        entry_sines, exit_sines = self._incidence_sines_rows(impact)
        top_radii = self.radii_km + self.thicknesses_km
        entry_distances = self.radii_km * np.sqrt((1 - entry_sines) * (1 + entry_sines))
        exit_distances = top_radii * np.sqrt((1 - exit_sines) * (1 + exit_sines))
        lengths = (
            self.thicknesses_km
            * (2 * self.radii_km + self.thicknesses_km)
            / (entry_distances + exit_distances)
        )
        dry = lengths @ self.dry_air_db_per_km
        water = lengths @ self.water_vapour_db_per_km
        refractive_excess_path_m = (lengths @ (self.refractive_indices - 1)) * 1_000
        geometric_path_m = np.sum(lengths, axis=1) * 1_000
        top_radius = EARTH_RADIUS_KM + self.upper_height_km
        vacuum_km = (target_radius - top_radius) * (
            (target_radius + top_radius)
            / (
                target_radius * np.sqrt(1 - (impact / target_radius) ** 2)
                + top_radius * np.sqrt(1 - (impact / top_radius) ** 2)
            )
        )
        excess_m = geometric_path_m + vacuum_km * 1_000 - range_m + refractive_excess_path_m
        roundoff_m = 64 * np.finfo(float).eps * range_m
        if np.any(excess_m < -roundoff_m):
            raise ValueError("endpoint solve produced a negative excess optical path")
        excess_delay_s = np.maximum(0.0, excess_m) / SPEED_OF_LIGHT_M_S
        return dry, water, dry + water, apparent, excess_delay_s

    def _solve_elevation_rows(
        self,
        lower: NDArray[np.float64],
        target_radius: NDArray[np.float64],
        target_angle: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        """Apparent elevations by safeguarded Newton iteration (module docstring)."""
        apparent = lower.copy()
        pending = np.flatnonzero(lower != 90.0)
        radius, angle = target_radius[pending], target_angle[pending]
        residual, slope = self._endpoint_residual_rows(lower[pending], radius, angle)
        roundoff_rad = 64 * np.finfo(float).eps
        if np.any(residual < -roundoff_rad):
            raise ValueError("geometric endpoint is not bracketed by the ascending refracted ray")
        solve = np.abs(residual) > roundoff_rad
        radius, angle = radius[solve], angle[solve]
        apparent[pending[solve]] = _newton_rows(
            lambda elevation, rows: self._endpoint_residual_rows(
                elevation, radius[rows], angle[rows]
            ),
            lower[pending[solve]],
            (residual[solve], slope[solve]),
        )
        return apparent

    def _endpoint_residual_rows(
        self,
        elevation_deg: NDArray[np.float64],
        target_radius: NDArray[np.float64],
        target_angle: NDArray[np.float64],
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """The endpoint angle less the target angle, and its derivative per degree."""
        impact = self._impact_km_rows(elevation_deg)
        top_radius = EARTH_RADIUS_KM + self.upper_height_km
        if np.any(impact > top_radius):
            raise ValueError("refracted ray cannot continue into vacuum above 100 km")
        entry_sines, exit_sines = self._incidence_sines_rows(impact)
        # d asin(b / s) / db = 1 / sqrt(s^2 - b^2) = (1 / s) / sqrt(1 - (b / s)^2).
        slope = np.sum(
            1 / (self.refractive_indices * self.radii_km * np.sqrt(1 - entry_sines**2))
            - 1
            / (
                self.refractive_indices
                * (self.radii_km + self.thicknesses_km)
                * np.sqrt(1 - exit_sines**2)
            ),
            axis=1,
        )
        np.arcsin(entry_sines, out=entry_sines)
        np.arcsin(exit_sines, out=exit_sines)
        entry_sines -= exit_sines
        central_angle = np.sum(entry_sines, axis=1)
        top, target = impact / top_radius, impact / target_radius
        residual = central_angle + np.arcsin(top) - np.arcsin(target) - target_angle
        slope += 1 / (top_radius * np.sqrt(1 - top**2)) - 1 / (
            target_radius * np.sqrt(1 - target**2)
        )
        # db/de for the impact parameter b = n0 r0 cos(e), e in degrees.
        slope *= (
            -float(self.refractive_indices[0] * self.radii_km[0])
            * np.sin(np.radians(elevation_deg))
            * (np.pi / 180)
        )
        return residual, slope

    def _impact_km_rows(self, elevation_deg: NDArray[np.float64]) -> NDArray[np.float64]:
        impact = float(self.refractive_indices[0] * self.radii_km[0]) * np.cos(
            np.radians(elevation_deg)
        )
        return np.where(elevation_deg == 90.0, 0.0, impact)

    def _incidence_sines_rows(
        self, impact_km: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        top_radii = self.radii_km + self.thicknesses_km
        entry_sines = impact_km[:, None] / (self.refractive_indices * self.radii_km)
        exit_sines = impact_km[:, None] / (self.refractive_indices * top_radii)
        if np.any(entry_sines > 1.0) or np.any(exit_sines > 1.0):
            raise ValueError("refracted path is trapped or outside the ascending-ray domain")
        return entry_sines, exit_sines


@dataclass(frozen=True, slots=True, eq=False)
class GasTable:
    """A column's total gaseous loss and ray central angle as functions of apparent elevation.

    For the Shannon integrand and Es/N0 root-finding only (design spec section 6.4 and the
    Task 18 ruling); exported per-sample values use ``ReferenceColumn.for_geometry_array``.

    Both column sums of the ray trace depend on the apparent elevation e alone: the total loss
    T(e) = Σ ℓ_i (γ_dry,i + γ_water,i) and the central angle C(e) = Σ (β_i − α_i) the ray
    sweeps inside the column. Each is tabulated as a piecewise Chebyshev series of degree
    ``GAS_TABLE_DEGREE`` on ``GAS_TABLE_SEGMENTS`` segments of [5, 90] degrees, equal in log(e),
    from the exact sums at the Chebyshev points (``_column_rows``). The endpoint solve is
    ``for_geometry``'s: the endpoint angle C(e) + asin(b / r_top) − asin(b / r_target), with
    b = n0 r0 cos(e), equals the geometric endpoint's angle, solved by the same safeguarded
    Newton iteration (``_newton_rows``) with C and its derivative from the table and the vacuum
    terms exact. Only T then comes from the table.

    ``build_gas_table`` compares the series with the exact sums at every segment end and midway
    between consecutive Chebyshev points: ``max_loss_error_db`` and
    ``max_central_angle_error_rad`` are the largest differences found. An error δC in C moves
    the solved elevation by at most δC / |C'(e)| (the vacuum terms only steepen the residual),
    so the loss error at e is bounded by |T̃(e) − T(e)| + |T'(e)| δC / |C'(e)|;
    ``error_bound_db`` is the largest such bound over the check points, and ``verified`` is
    whether it is within ``GAS_TABLE_MAX_ERROR_DB``. Callers use the exact trace otherwise.
    The solve itself converges to about 1e-12 degrees in both paths (``_newton_rows``).
    """

    column: ReferenceColumn
    edges_deg: NDArray[np.float64]
    total_db: NDArray[np.float64]
    # C and its derivative per degree, stacked: (segments, 2, degree + 1).
    central_angle_rad: NDArray[np.float64]
    max_loss_error_db: float
    max_central_angle_error_rad: float
    error_bound_db: float
    verified: bool

    def total_db_for_geometry(
        self, geometric_elevation_deg: NDArray[np.float64], range_m: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        """``for_geometry_array(...).total_db`` from the table, with the same validation."""
        column = self.column
        shape, lower, _, target_radius, target_angle = column._endpoints_rows(
            geometric_elevation_deg, range_m
        )
        apparent = lower.copy()
        pending = np.flatnonzero(lower != 90.0)
        radius, angle = target_radius[pending], target_angle[pending]
        residual, slope = self._residual(lower[pending], radius, angle)
        roundoff_rad = 64 * np.finfo(float).eps
        if np.any(residual < -roundoff_rad):
            raise ValueError("geometric endpoint is not bracketed by the ascending refracted ray")
        solve = np.abs(residual) > roundoff_rad
        radius, angle = radius[solve], angle[solve]
        apparent[pending[solve]] = _newton_rows(
            lambda elevation, rows: self._residual(elevation, radius[rows], angle[rows]),
            lower[pending[solve]],
            (residual[solve], slope[solve]),
        )
        return _readonly(self._series(self.total_db, apparent)).reshape(shape)

    def _residual(self, elevation_deg, target_radius, target_angle):
        column = self.column
        impact = column._impact_km_rows(elevation_deg)
        top_radius = EARTH_RADIUS_KM + column.upper_height_km
        top, target = impact / top_radius, impact / target_radius
        central, central_slope = self._series(self.central_angle_rad, elevation_deg)
        residual = central + np.arcsin(top) - np.arcsin(target) - target_angle
        vacuum_slope = 1 / (top_radius * np.sqrt(1 - top**2)) - 1 / (
            target_radius * np.sqrt(1 - target**2)
        )
        impact_slope = (
            -float(column.refractive_indices[0] * column.radii_km[0])
            * np.sin(np.radians(elevation_deg))
            * (np.pi / 180)
        )
        slope = central_slope + vacuum_slope * impact_slope
        return residual, slope

    def _series(self, coefficients, elevation_deg):
        """Clenshaw evaluation of the segment's Chebyshev series at each elevation.

        ``coefficients`` is (segments, degree + 1), or (segments, k, degree + 1) for k series
        evaluated together (then the result has k rows).
        """
        edges = self.edges_deg
        segment = np.searchsorted(edges, elevation_deg, side="right") - 1
        segment = np.minimum(np.maximum(segment, 0), edges.size - 2)
        a, b = edges[segment], edges[segment + 1]
        u = (2 * elevation_deg - (a + b)) / (b - a)
        c = np.moveaxis(coefficients[segment], 0, -1)  # (..., degree + 1, n)
        twice_u = 2 * u
        b1 = np.zeros(c.shape[:-2] + u.shape)
        b2 = np.zeros_like(b1)
        for k in range(c.shape[-2] - 1, 0, -1):
            b1, b2 = c[..., k, :] + twice_u * b1 - b2, b1
        return c[..., 0, :] + u * b1 - b2


def build_gas_table(column: ReferenceColumn) -> GasTable:
    """Tabulate a ground-to-space column's loss and central angle (``GasTable``)."""
    if column.upper_height_km != 100.0:
        raise ValueError("ground-to-space geometry requires a column ending at 100 km")
    count, degree = GAS_TABLE_SEGMENTS, GAS_TABLE_DEGREE
    edges = np.exp(np.linspace(np.log(GAS_TABLE_LOW_DEG), np.log(90.0), count + 1))
    edges[0], edges[-1] = GAS_TABLE_LOW_DEG, 90.0
    a, b = edges[:-1, None], edges[1:, None]
    angles = np.pi * (np.arange(degree + 1) + 0.5) / (degree + 1)
    nodes = np.cos(angles)  # descending in (-1, 1)
    points = 0.5 * (a + b) + 0.5 * (b - a) * nodes
    total, central = column._column_rows(points.ravel())
    # Interpolating Chebyshev coefficients at the first-kind points (discrete cosine transform).
    basis = np.cos(np.outer(np.arange(degree + 1), angles)) * (2.0 / (degree + 1))
    basis[0] *= 0.5
    total_coefficients = total.reshape(points.shape) @ basis.T
    central_coefficients = central.reshape(points.shape) @ basis.T
    total_slope = np.zeros_like(total_coefficients)
    central_slope = np.zeros_like(central_coefficients)
    for index in range(count):
        scale = 2.0 / (b[index, 0] - a[index, 0])  # d/de = du/de d/du
        total_slope[index, :degree] = chebder(total_coefficients[index]) * scale
        central_slope[index, :degree] = chebder(central_coefficients[index]) * scale
    table = GasTable(
        column=column,
        edges_deg=_readonly(edges),
        total_db=_readonly_matrix(total_coefficients),
        central_angle_rad=_readonly_matrix(np.stack([central_coefficients, central_slope], 1)),
        max_loss_error_db=np.inf,
        max_central_angle_error_rad=np.inf,
        error_bound_db=np.inf,
        verified=False,
    )
    # Check points: every segment end and every midpoint between consecutive Chebyshev points.
    ordered = np.sort(points, axis=1)
    checks = np.unique(np.concatenate([edges, (0.5 * (ordered[:, :-1] + ordered[:, 1:])).ravel()]))
    exact_total, exact_central = column._column_rows(checks)
    loss_error = np.abs(table._series(table.total_db, checks) - exact_total)
    angle, angle_slope = table._series(table.central_angle_rad, checks)
    angle_error = float(np.max(np.abs(angle - exact_central)))
    shift_deg = angle_error / np.abs(angle_slope)
    bound = float(np.max(loss_error + np.abs(table._series(total_slope, checks)) * shift_deg))
    return replace(
        table,
        max_loss_error_db=float(np.max(loss_error)),
        max_central_angle_error_rad=angle_error,
        error_bound_db=bound,
        verified=bound <= GAS_TABLE_MAX_ERROR_DB,
    )


def _newton_rows(residual, lower: NDArray[np.float64], first) -> NDArray[np.float64]:
    """Roots in [lower, 90] of a decreasing ``residual`` with ``residual(lower) > 0``.

    ``residual(elevation, rows)`` returns the residual and its derivative per degree at the
    elevations of the given rows; ``first`` is that pair at ``lower``. Every iterate updates a
    bracket from the residual's sign, and a Newton step that leaves the bracket is replaced by
    bisection. A row is done when its Newton step is at most ``NEWTON_TOLERANCE_DEG`` or its
    bracket is no wider. The last step is applied when it stays in the bracket, so the error is
    about the step squared or the residual's rounding floor (about 1e-12 degrees) divided by
    its slope; a step that small leaving the bracket is that rounding floor, and x is kept.
    """
    low = lower.copy()
    high = np.full_like(low, 90.0)
    elevation = low.copy()
    active = np.arange(low.size)
    value, slope = first
    for _ in range(NEWTON_MAX_ITERATIONS):
        x = elevation[active]
        low[active] = np.where(value > 0.0, x, low[active])
        high[active] = np.where(value < 0.0, x, high[active])
        with np.errstate(divide="ignore", invalid="ignore"):
            trial = x - value / slope
        a, b = low[active], high[active]
        inside = (trial > a) & (trial < b)
        # A step this small is at the residual's rounding floor: the root is that close to x.
        done = (value == 0.0) | (np.abs(trial - x) <= NEWTON_TOLERANCE_DEG)
        elevation[active] = np.where(inside, trial, np.where(done, x, 0.5 * (a + b)))
        done |= b - a <= NEWTON_TOLERANCE_DEG
        active = active[~done]
        if active.size == 0:
            return elevation
        value, slope = residual(elevation[active], active)
    raise ValueError(
        f"apparent elevation solve did not converge within {NEWTON_MAX_ITERATIONS} iterations"
    )


def _readonly_matrix(values) -> NDArray[np.float64]:
    return _readonly(values).reshape(np.shape(values))


def _finite_array(values: NDArray[np.float64], name: str) -> NDArray[np.float64]:
    """Element-wise ``_validate_finite``: real numbers only, never bools."""
    array = np.asarray(values)
    if array.dtype.kind not in "iuf" or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite and not a bool")
    return array.astype(np.float64, copy=False)


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
