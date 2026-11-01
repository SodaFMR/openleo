"""Satellite and topocentric geometry for the 2.0 engine (design spec section 4).

Satellite states are v1's chain: Skyfield ``EarthSatellite.at(t).frame_xyz_and_velocity(itrs)``
per satellite, vectorised over time, so SGP4 runs in TEME and Skyfield rotates to ITRS with
its own rate term for the velocity. ΔUT1 comes from the builtin timescale and polar motion is
not applied, as in v1.

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
from skyfield.api import wgs84
from skyfield.framelib import itrs

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
        # The last Skyfield times: callers sweep satellites and stations over the same offsets,
        # and a shared Time also shares Skyfield's cached Earth-rotation matrices.
        self._last_times: tuple[bytes, object] | None = None

    def satellite_ecef(
        self, satellite_index: int, offsets_s: ArrayLike
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """ITRS position (m) and velocity (m/s), each of shape (len(offsets), 3)."""
        offsets = _offsets(offsets_s)
        if offsets.size == 0:
            return np.empty((0, 3)), np.empty((0, 3))
        satellite = self.orbits[satellite_index].satellite
        state = satellite.at(self._times(offsets))
        for offset, message in zip(offsets.tolist(), state.message, strict=True):
            if message:
                raise ValueError(
                    f"SGP4 error for NORAD {satellite.model.satnum} at "
                    f"{utc_text(self.start_utc, offset)}: {message}"
                )
        position, velocity = state.frame_xyz_and_velocity(itrs)
        position_m, velocity_mps = position.m.T, velocity.m_per_s.T
        finite = np.isfinite(position_m).all(axis=1) & np.isfinite(velocity_mps).all(axis=1)
        if not finite.all():
            offset = offsets[np.argmin(finite)]
            raise ValueError(
                f"SGP4 produced a non-finite state for NORAD {satellite.model.satnum} at "
                f"{utc_text(self.start_utc, offset)}"
            )
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

    def _times(self, offsets: NDArray[np.float64]):
        key = offsets.tobytes()
        if self._last_times is None or self._last_times[0] != key:
            self._last_times = (key, skyfield_times(self.timescale, self.start_utc, offsets))
        return self._last_times[1]


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
