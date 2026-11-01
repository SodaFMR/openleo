"""The link budget as a continuous function of time (design spec section 5).

``LinkEvaluator.evaluate(station, satellite, offsets)`` is the one function that grid sampling,
event root-finding, quadrature and refined-grid convergence runs all call, so integrals and event
times agree with the per-sample values.

The arithmetic is OpenLEO v1.0.0's ``constellation._radio_metrics`` and the Es/N0 and Doppler of
its caller ``_links``, in the same operation order, with the array forms of the same kernels:
free-space C/N0 from range, EIRP, the station terminal's gain, loss and noise temperature; then
the total atmospheric loss (gases plus declared hydrometeors) subtracted from it; delay is the
geometric delay plus the atmospheric excess delay. The atmosphere is traced from the geometric
elevation and range, and the hydrometeor path uses the geometric elevation and the station's
height above mean sea level, both as in v1. The evaluator selects no MODCOD; that is the
adaptation state machine's job, which reads ``available_db``.

Tabulated integrand. ``tabulated_available_db`` and ``tabulated_shannon_bps`` are the same link
budget, operation for operation, except that the gaseous loss under ``itu_reference`` comes
from the station column's ``GasTable`` (``openleo.physics.atmospheric_path``) instead of the
ray trace. The engine uses them only for the Shannon-bound quadrature and the MODCOD
root-finding, which evaluate the link at thousands of times per pass; every exported
per-sample value comes from ``evaluate`` except each sample's MODCOD and rate, which come
from the rate segments. Tables are built only with events on, and one is used only when
``build_gas_table`` verified it (a heuristic Chebyshev-level loss error bound, see
docs/METHODS.md, within ``GAS_TABLE_ACCEPT_DB``, 2.5e-11 dB, a quarter of the 1e-10 dB limit,
and an end-to-end check against the exact ray trace within the bound);
otherwise those methods trace the ray exactly. ``gas_tables`` reports, per station, the bound
or None.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

import numpy as np

from openleo.engine.timegrid import utc_text
from openleo.physics.atmospheric_path import build_gas_table, build_reference_column
from openleo.physics.hydrometeor_path import (
    HydrometeorPathResult,
    evaluate_hydrometeor_path_array,
)
from openleo.physics.link import (
    carrier_to_noise_density_db_hz,
    doppler_shift_hz,
    free_space_path_loss_db,
    noise_density_dbw_per_hz,
    propagation_delay_s,
    received_carrier_power_dbw,
    shannon_capacity_upper_bound_bps,
    signal_to_noise_ratio_db,
)

if TYPE_CHECKING:
    from numpy.typing import ArrayLike, NDArray

    from openleo.engine.geometry import Geometry
    from openleo.physics.atmospheric_path import GasTable, ReferenceColumn
    from openleo.physics.hydrometeor_path import HydrometeorStation
    from openleo.scenario.model import Scenario

BASE_COLUMNS = (
    "elevation_deg",
    "azimuth_deg",
    "range_m",
    "range_rate_mps",
    "doppler_hz",
    "delay_s",
    "cn0_db_hz",
    "esn0_db",
    "snr_db",
    "shannon_upper_bound_bps",
)
REFERENCE_COLUMNS = (
    "gaseous_dry_attenuation_db",
    "gaseous_water_attenuation_db",
    "gaseous_attenuation_db",
    "free_space_cn0_db_hz",
    "geometric_delay_s",
    "atmospheric_excess_delay_s",
    "apparent_elevation_deg",
)
_HYDROMETEOR_PATH_FIELDS = tuple(field.name for field in fields(HydrometeorPathResult))
HYDROMETEOR_COLUMNS = (*_HYDROMETEOR_PATH_FIELDS, "total_atmospheric_attenuation_db")


@dataclass(frozen=True, slots=True)
class _StationLink:
    """What one station contributes to its links, resolved once at construction."""

    name: str
    mask_deg: float
    receive_gain_dbi: float
    miscellaneous_loss_db: float
    noise_density_dbw_per_hz: float
    column: ReferenceColumn | None
    table: GasTable | None
    height_amsl_m: float | None
    hydrometeors: HydrometeorStation | None


class LinkEvaluator:
    """v1 link budgets of every station-satellite pair, at any offsets above the station mask.

    ``columns`` names the arrays ``evaluate`` returns, in order: ``BASE_COLUMNS``, then
    ``REFERENCE_COLUMNS`` under ``itu_reference`` and ``HYDROMETEOR_COLUMNS`` with declared
    hydrometeors. One P.676 reference column per station is built here and reused.
    """

    def __init__(self, scenario: Scenario, geometry: Geometry) -> None:
        stations = scenario.ground_segment.stations
        if tuple(geometry.stations) != tuple(stations):
            raise ValueError("geometry must be built for the scenario's stations, in order")
        propagation = scenario.propagation
        reference = propagation.model == "itu_reference"
        if propagation.hydrometeors is not None and not reference:
            raise ValueError("/propagation/hydrometeors requires propagation.model itu_reference")
        hydrometeors = dict(propagation.hydrometeors.stations) if propagation.hydrometeors else None
        self.scenario = scenario
        self.geometry = geometry
        self.columns = (
            BASE_COLUMNS
            + (REFERENCE_COLUMNS if reference else ())
            + (HYDROMETEOR_COLUMNS if hydrometeors is not None else ())
        )
        self._stations = tuple(
            self._station_link(index, reference, hydrometeors) for index in range(len(stations))
        )

    def _station_link(
        self,
        index: int,
        reference: bool,
        hydrometeors: dict[str, HydrometeorStation] | None,
    ) -> _StationLink:
        scenario = self.scenario
        station = scenario.ground_segment.stations[index]
        terminal = scenario.terminal_for(station)
        if reference and station.height_amsl_m is None:
            raise ValueError(
                f"/ground_segment/stations/{index}/height_amsl_m is required when "
                "propagation.model is itu_reference"
            )
        if hydrometeors is not None and station.name not in hydrometeors:
            raise ValueError(
                f"/propagation/hydrometeors/stations has no entry for station {station.name!r}"
            )
        mask_deg = (
            station.minimum_elevation_deg
            if station.minimum_elevation_deg is not None
            else scenario.ground_segment.default_minimum_elevation_deg
        )
        column = (
            build_reference_column(
                scenario.radio.carrier_frequency_hz,
                station.height_amsl_m / 1000,
                refinement=scenario.propagation.refinement,
            )
            if reference
            else None
        )
        # Only the event integrand reads the table (events off integrates grid samples).
        table = (
            build_gas_table(column) if column is not None and scenario.time.events.enabled else None
        )
        return _StationLink(
            name=station.name,
            mask_deg=mask_deg,
            receive_gain_dbi=terminal.receive_gain_dbi,
            miscellaneous_loss_db=terminal.miscellaneous_loss_db,
            # v1 evaluated this scalar kernel at every sample; it is the same number each time.
            noise_density_dbw_per_hz=noise_density_dbw_per_hz(terminal.system_noise_temperature_k),
            column=column,
            table=table if table is not None and table.verified else None,
            height_amsl_m=station.height_amsl_m,
            hydrometeors=hydrometeors[station.name] if hydrometeors is not None else None,
        )

    def evaluate(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> dict[str, NDArray[np.float64]]:
        """Read-only float64 arrays, one per name in ``columns``, at the offsets in seconds
        after the geometry's ``start_utc``.

        Every offset must be at or above the station's elevation mask: the atmospheric kernels
        are undefined below 5 degrees, and v1 evaluated only visible samples.
        """
        station, topocentric = self._visible(station_index, satellite_index, offsets_s)
        elevation_deg, range_m = topocentric.elevation_deg, topocentric.range_m
        frequency_hz = self.scenario.radio.carrier_frequency_hz
        path = (
            station.column.for_geometry_array(elevation_deg, range_m)
            if station.column is not None
            else None
        )
        hydrometeor_path = (
            evaluate_hydrometeor_path_array(
                frequency_hz, elevation_deg, station.height_amsl_m, station.hydrometeors
            )
            if station.hydrometeors is not None
            else None
        )
        budget = self._budget(
            station,
            satellite_index,
            range_m,
            path.total_db if path is not None else None,
            hydrometeor_path.hydrometeor_attenuation_db if hydrometeor_path is not None else None,
        )
        geometric_delay_s = budget["geometric_delay_s"]
        values = {
            "elevation_deg": elevation_deg,
            "azimuth_deg": topocentric.azimuth_deg,
            "range_m": range_m,
            "range_rate_mps": topocentric.range_rate_mps,
            "doppler_hz": doppler_shift_hz(frequency_hz, topocentric.range_rate_mps),
            "delay_s": geometric_delay_s + path.excess_delay_s
            if path is not None
            else geometric_delay_s,
            "cn0_db_hz": budget["cn0_db_hz"],
            "esn0_db": budget["esn0_db"],
            "snr_db": budget["snr_db"],
            "shannon_upper_bound_bps": budget["shannon_upper_bound_bps"],
        }
        if path is not None:
            values |= {
                "gaseous_dry_attenuation_db": path.dry_air_db,
                "gaseous_water_attenuation_db": path.water_vapour_db,
                "gaseous_attenuation_db": path.total_db,
                "free_space_cn0_db_hz": budget["free_space_cn0_db_hz"],
                "geometric_delay_s": geometric_delay_s,
                "atmospheric_excess_delay_s": path.excess_delay_s,
                "apparent_elevation_deg": path.apparent_elevation_deg,
            }
        if hydrometeor_path is not None:
            values |= {name: getattr(hydrometeor_path, name) for name in _HYDROMETEOR_PATH_FIELDS}
            values["total_atmospheric_attenuation_db"] = budget["total_atmospheric_db"]
        return {name: _readonly(values[name]) for name in self.columns}

    def tabulated_available_db(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> NDArray[np.float64]:
        """``available_db`` with the tabulated gaseous loss (module docstring)."""
        esn0_db = self._tabulated(station_index, satellite_index, offsets_s)["esn0_db"]
        return esn0_db - self.scenario.adaptation.implementation_margin_db

    def tabulated_shannon_bps(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> NDArray[np.float64]:
        """``evaluate``'s ``shannon_upper_bound_bps`` with the tabulated gaseous loss."""
        budget = self._tabulated(station_index, satellite_index, offsets_s)
        return budget["shannon_upper_bound_bps"]

    @property
    def gas_tables(self) -> tuple[float | None, ...]:
        """Per station: the verified loss error bound (dB) of its gas table, or None when the
        tabulated methods trace the ray exactly (no reference model, or not verified)."""
        return tuple(
            station.table.error_bound_db if station.table is not None else None
            for station in self._stations
        )

    def _tabulated(self, station_index, satellite_index, offsets_s):
        station, topocentric = self._visible(station_index, satellite_index, offsets_s)
        elevation_deg, range_m = topocentric.elevation_deg, topocentric.range_m
        if station.table is not None:
            gaseous_db = station.table.total_db_for_geometry(elevation_deg, range_m)
        elif station.column is not None:
            gaseous_db = station.column.for_geometry_array(elevation_deg, range_m).total_db
        else:
            gaseous_db = None
        hydrometeor_db = (
            evaluate_hydrometeor_path_array(
                self.scenario.radio.carrier_frequency_hz,
                elevation_deg,
                station.height_amsl_m,
                station.hydrometeors,
            ).hydrometeor_attenuation_db
            if station.hydrometeors is not None
            else None
        )
        return self._budget(station, satellite_index, range_m, gaseous_db, hydrometeor_db)

    def _visible(self, station_index, satellite_index, offsets_s):
        """The station and the geometry at the offsets, which must be at or above its mask."""
        station_index = _index(station_index, len(self._stations), "station_index")
        satellite_index = _index(satellite_index, len(self.geometry.orbits), "satellite_index")
        station = self._stations[station_index]
        topocentric = self.geometry.topocentric(station_index, satellite_index, offsets_s)
        elevation_deg = topocentric.elevation_deg
        below = np.flatnonzero(~(elevation_deg >= station.mask_deg))
        if below.size:
            offset = np.atleast_1d(np.asarray(offsets_s, dtype=np.float64))[below[0]]
            elevation = float(elevation_deg[below[0]])  # a plain float: no np.float64(...) repr
            raise ValueError(
                f"NORAD {self._norad(satellite_index)} is at {elevation!r} deg, "
                f"below the {station.mask_deg!r} deg elevation mask of station "
                f"{station.name!r}, at {utc_text(self.geometry.start_utc, offset)}; links are "
                "evaluated only at or above the mask"
            )
        return station, topocentric

    def _budget(self, station, satellite_index, range_m, gaseous_db, hydrometeor_db):
        """v1 _radio_metrics operation for operation; Es/N0 as v1 _links had it."""
        radio = self.scenario.radio
        path_loss_db = free_space_path_loss_db(range_m, radio.carrier_frequency_hz)
        carrier_power_dbw = received_carrier_power_dbw(
            radio.eirp_dbw,
            station.receive_gain_dbi,
            path_loss_db,
            station.miscellaneous_loss_db,
        )
        free_space_cn0_db_hz = carrier_to_noise_density_db_hz(
            carrier_power_dbw, station.noise_density_dbw_per_hz
        )
        total_atmospheric_db = (gaseous_db if gaseous_db is not None else 0.0) + (
            hydrometeor_db if hydrometeor_db is not None else 0.0
        )
        self._require_finite(
            total_atmospheric_db, "total_atmospheric_attenuation_db", station, satellite_index
        )
        cn0_db_hz = free_space_cn0_db_hz - total_atmospheric_db
        snr_db = signal_to_noise_ratio_db(cn0_db_hz, radio.channel_bandwidth_hz)
        with np.errstate(over="ignore"):  # an overflow is refused just below, as in v1
            shannon_bps = shannon_capacity_upper_bound_bps(snr_db, radio.channel_bandwidth_hz)
        self._require_finite(shannon_bps, "shannon_upper_bound_bps", station, satellite_index)
        return {
            "free_space_cn0_db_hz": free_space_cn0_db_hz,
            "total_atmospheric_db": total_atmospheric_db,
            "geometric_delay_s": propagation_delay_s(range_m),
            "cn0_db_hz": cn0_db_hz,
            "esn0_db": signal_to_noise_ratio_db(
                cn0_db_hz, self.scenario.adaptation.symbol_rate_baud
            ),
            "snr_db": snr_db,
            "shannon_upper_bound_bps": shannon_bps,
        }

    def available_db(
        self, station_index: int, satellite_index: int, offsets_s: ArrayLike
    ) -> NDArray[np.float64]:
        """Es/N0 less the implementation margin: v1 ``select_modcod``'s ``available_db``."""
        esn0_db = self.evaluate(station_index, satellite_index, offsets_s)["esn0_db"]
        return esn0_db - self.scenario.adaptation.implementation_margin_db

    def _norad(self, satellite_index: int) -> int:
        return int(self.geometry.orbits[satellite_index].satellite.model.satnum)

    def _require_finite(
        self,
        values: NDArray[np.float64],
        name: str,
        station: _StationLink,
        satellite_index: int,
    ) -> None:
        if not np.all(np.isfinite(values)):
            raise ValueError(
                f"{name} must be finite (station {station.name!r}, "
                f"NORAD {self._norad(satellite_index)})"
            )


def _index(value: int, count: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer) or not 0 <= value < count:
        raise ValueError(f"{name}={value!r} must be an integer index in [0, {count})")
    return int(value)


def _readonly(values: NDArray[np.float64]) -> NDArray[np.float64]:
    values = np.asarray(values, dtype=np.float64)
    values.setflags(write=False)
    return values
