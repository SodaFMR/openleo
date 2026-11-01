"""Satellite and topocentric geometry for the 2.0 engine (design spec section 4).

Satellite states are v1's chain, Skyfield ``EarthSatellite.at(t).frame_xyz_and_velocity(itrs)``,
evaluated without its intermediate celestial frame:

- SGP4 runs in TEME on the satellite's ``Satrec`` (``sgp4_array``), at the Julian date Skyfield
  passes it (``t.whole`` and the UTC fraction ``t.tai_fraction - leap seconds``).
- Skyfield rotates TEME to GCRS with ``(rot_z(θ − GAST) N P)ᵀ`` (θ is GMST 1982 at UT1) and
  GCRS to ITRS with ``rot_z(−GAST) N P``. The product is ``rot_z(−θ)``: GAST, nutation and
  precession cancel, up to rounding. So the position is ``rot_z(−θ) r_TEME`` with Skyfield's
  own ``theta_GMST1982``, and the velocity is ``rot_z(−θ) v_TEME`` plus Skyfield's ITRS rate
  term ``ω × r`` (``ANGVEL``, no polar motion): no IAU 2000A nutation series is evaluated. The
  difference from the full chain is rounding, about 1e-9 m and 1e-12 m/s, far inside the v1
  equivalence tolerances (1e-4 m, 1e-6 m/s) that ``tests/engine/test_geometry.py`` checks
  against the frozen v1 fixtures and against Skyfield's chain off the grid.
- The Skyfield times and rotations of the last offsets vector are cached, and so is the last
  satellite state: callers sweep stations and satellites over the same offsets.

ΔUT1 comes from the builtin timescale and polar motion is not applied, as in v1.

Topocentric values come from ITRS differences and the station's geodetic east-north-up basis:
ρ = r_sat − r_station, range = ‖ρ‖, range rate = ρ·v_sat / range (the station is fixed in
ITRS), elevation = atan2(ρ·û, hypot(ρ·ê, ρ·n̂)) and azimuth = atan2(ρ·ê, ρ·n̂) mod 360°.
The elevation is the well-conditioned form of asin(ρ·û / range) and is also Skyfield's form.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from sgp4.api import SGP4_ERRORS
from skyfield.api import wgs84
from skyfield.constants import ANGVEL, DAY_S
from skyfield.sgp4lib import theta_GMST1982

from openleo.engine.timegrid import skyfield_times, utc_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import ArrayLike, NDArray

    from openleo.engine.catalogs import LoadedOrbit
    from openleo.scenario.model import Station

# Below this horizontal-to-range ratio (5.7e-8 deg from the zenith) the azimuth is undefined
# and reported as 0.
ZENITH_AZIMUTH_RATIO = 1e-9


@dataclass(frozen=True, slots=True, eq=False)
class Topocentric:
    elevation_deg: NDArray[np.float64]
    azimuth_deg: NDArray[np.float64]
    range_m: NDArray[np.float64]
    range_rate_mps: NDArray[np.float64]


class Geometry:
    """Satellite ECEF states and station-to-satellite geometry at offsets after ``start_utc``.

    Offsets are seconds; like every engine time they are rounded to the microsecond when they
    are converted to Skyfield times (``openleo.engine.timegrid.skyfield_times``).
    """

    def __init__(
        self,
        orbits: Sequence[LoadedOrbit],
        stations: Sequence[Station],
        timescale,
        start_utc: datetime,
    ) -> None:
        if not isinstance(start_utc, datetime) or start_utc.utcoffset() != timedelta(0):
            raise ValueError("start_utc must be a timezone-aware UTC datetime")
        self.orbits = tuple(orbits)
        self.stations = tuple(stations)
        self.timescale = timescale
        self.start_utc = start_utc.astimezone(UTC)
        self.station_ecef_m = np.array(
            [
                wgs84.latlon(
                    station.latitude_deg,
                    station.longitude_deg,
                    elevation_m=station.height_ellipsoid_m,
                ).itrs_xyz.m
                for station in self.stations
            ],
            dtype=np.float64,
        ).reshape(-1, 3)
        self.station_ecef_m.setflags(write=False)
        self._enu = np.array(
            [_enu_basis(station.latitude_deg, station.longitude_deg) for station in self.stations]
        )
        # The last offsets' SGP4 dates and Earth rotation, and the last satellite state.
        self._last_frame: tuple[bytes, _Frame] | None = None
        self._last_state: tuple[int, bytes, tuple] | None = None

    def satellite_ecef(
        self, satellite_index: int, offsets_s: ArrayLike
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """ITRS position (m) and velocity (m/s), each of shape (len(offsets), 3)."""
        offsets = _offsets(offsets_s)
        if offsets.size == 0:
            return np.empty((0, 3)), np.empty((0, 3))
        key = offsets.tobytes()
        if self._last_state is not None and self._last_state[:2] == (satellite_index, key):
            return self._last_state[2]
        satellite = self.orbits[satellite_index].satellite
        frame = self._frame(offsets, key)
        errors, position_km, velocity_kmps = _teme_states(satellite.model, frame)
        if errors.any():
            first = int(np.argmax(errors != 0))
            raise ValueError(
                f"SGP4 error for NORAD {satellite.model.satnum} at "
                f"{utc_text(self.start_utc, offsets[first])}: {SGP4_ERRORS[int(errors[first])]}"
            )
        cos, sin = frame.cos_theta, frame.sin_theta
        x, y, z = position_km.T * 1_000.0
        vx, vy, vz = velocity_kmps.T * 1_000.0
        # rot_z(-θ) r, and rot_z(-θ) v + ω × r with ω = (0, 0, ANGVEL) rad/s.
        position_m = np.stack([cos * x + sin * y, cos * y - sin * x, z], axis=1)
        velocity_mps = np.stack([cos * vx + sin * vy, cos * vy - sin * vx, vz], axis=1)
        velocity_mps[:, 0] += ANGVEL * position_m[:, 1]
        velocity_mps[:, 1] -= ANGVEL * position_m[:, 0]
        finite = np.isfinite(position_m).all(axis=1) & np.isfinite(velocity_mps).all(axis=1)
        if not finite.all():
            offset = offsets[np.argmin(finite)]
            raise ValueError(
                f"SGP4 produced a non-finite state for NORAD {satellite.model.satnum} at "
                f"{utc_text(self.start_utc, offset)}"
            )
        position_m.setflags(write=False)
        velocity_mps.setflags(write=False)
        self._last_state = (satellite_index, key, (position_m, velocity_mps))
        return position_m, velocity_mps

    def topocentric(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> Topocentric:
        """Geometric elevation, azimuth, range and range rate of a satellite from a station."""
        position_m, velocity_mps = self.satellite_ecef(satellite_index, offsets_s)
        relative_m = position_m - self.station_ecef_m[station_index]
        east_m, north_m, up_m = (relative_m @ self._enu[station_index].T).T
        range_m = np.sqrt(np.einsum("ij,ij->i", relative_m, relative_m))
        horizontal_m = np.hypot(east_m, north_m)
        with np.errstate(invalid="ignore", divide="ignore"):
            range_rate_mps = np.einsum("ij,ij->i", relative_m, velocity_mps) / range_m
        azimuth_deg = np.degrees(np.arctan2(east_m, north_m)) % 360.0
        # A tiny negative angle wraps to exactly 360.0 in floating point; keep [0, 360).
        azimuth_deg[azimuth_deg >= 360.0] = 0.0
        azimuth_deg[horizontal_m < ZENITH_AZIMUTH_RATIO * range_m] = 0.0
        # The states are finite, so only a satellite at the station itself can fail here.
        if not ((range_m > 0.0).all() and np.isfinite(range_rate_mps).all()):
            raise ValueError(
                f"non-finite topocentric geometry for station {self.stations[station_index].name}"
                f" and NORAD {self.orbits[satellite_index].satellite.model.satnum}"
            )
        return Topocentric(
            elevation_deg=np.degrees(np.arctan2(up_m, horizontal_m)),
            azimuth_deg=azimuth_deg,
            range_m=range_m,
            range_rate_mps=range_rate_mps,
        )

    def elevation_deg(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> NDArray[np.float64]:
        """Geometric elevation in degrees, the function event root-finding works on."""
        return self.topocentric(station_index, satellite_index, offsets_s).elevation_deg

    def _frame(self, offsets: NDArray[np.float64], key: bytes) -> _Frame:
        if self._last_frame is None or self._last_frame[0] != key:
            t = skyfield_times(self.timescale, self.start_utc, offsets)
            theta, _ = theta_GMST1982(t.whole, t.ut1_fraction)
            whole = np.asarray(t.whole, dtype=np.float64)
            fraction = np.asarray(t.tai_fraction - t._leap_seconds() / DAY_S, dtype=np.float64)
            self._last_frame = (key, _Frame(whole, fraction, np.cos(theta), np.sin(theta)))
        return self._last_frame[1]


@dataclass(frozen=True, slots=True, eq=False)
class _Frame:
    """SGP4's UTC Julian date (whole and fraction) and the TEME-to-ITRS angle θ at some times."""

    whole: NDArray[np.float64]
    fraction: NDArray[np.float64]
    cos_theta: NDArray[np.float64]
    sin_theta: NDArray[np.float64]


def _teme_states(model, frame: _Frame):
    """SGP4 error codes, TEME positions (km) and velocities (km/s) of one satellite."""
    return model.sgp4_array(frame.whole, frame.fraction)


def _enu_basis(latitude_deg: float, longitude_deg: float) -> NDArray[np.float64]:
    """Rows: east, north and up (the WGS84 ellipsoid normal) unit vectors in ITRS."""
    latitude, longitude = np.radians(latitude_deg), np.radians(longitude_deg)
    sin_lat, cos_lat = np.sin(latitude), np.cos(latitude)
    sin_lon, cos_lon = np.sin(longitude), np.cos(longitude)
    return np.array(
        [
            [-sin_lon, cos_lon, 0.0],
            [-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat],
            [cos_lat * cos_lon, cos_lat * sin_lon, sin_lat],
        ]
    )


def _offsets(offsets_s: ArrayLike) -> NDArray[np.float64]:
    offsets = np.atleast_1d(np.asarray(offsets_s, dtype=np.float64))
    if offsets.ndim != 1:
        raise ValueError("offsets must be a one-dimensional array of seconds")
    if not np.isfinite(offsets).all():
        raise ValueError("offsets must be finite")
    return offsets
