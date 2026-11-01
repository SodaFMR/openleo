"""Scenario v2: immutable dataclasses that mirror the JSON document (design spec section 3).

The field metadata is the single source of truth for the document's bounds. The parser
(``openleo.scenario.parse``) reads it to check every value and the JSON Schema generator turns
the same keys into schema keywords, so the two cannot drift. The keys, all JSON Schema names:

- ``description`` (every document field) and ``unit`` (every numeric field);
- ``minimum``, ``maximum``, ``exclusiveMinimum``, ``exclusiveMaximum`` and ``enum`` for scalars;
- ``minItems``, ``maxItems``, ``uniqueItems`` and ``items`` (a nested schema fragment with the
  same bound keys) for arrays;
- ``default``: present exactly on the fields a document may omit, holding the JSON value the
  parser uses for them. A ``None`` default also lets the field be ``null``. ``{}`` stands for an
  object whose own fields all take their defaults.

Allowed strings come from the ``Literal`` annotations. A few document maps are stored as
tuples of ``(name, value)`` pairs so the dataclasses stay hashable: ``Scenario.terminals`` and
the stations of ``HydrometeorConfig``. Their document form is a JSON object keyed by name.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from openleo.physics.adaptation import AdaptationConfig
from openleo.physics.hydrometeor_path import HydrometeorConfig

ROUTE_MODELS = ("minimum_delay", "maximum_rate", "fixed_capacity")
MAX_NORAD_ID = 2_147_483_647
# Dataclass fields of Scenario that are not keys of the document.
NOT_DOCUMENT_FIELDS = frozenset(("source_path", "source_sha256"))


def _meta(description: str, unit: str | None = None, **schema: Any) -> dict[str, Any]:
    """Schema metadata for one field. ``schema`` holds the JSON Schema keywords and ``default``."""
    metadata: dict[str, Any] = {"description": description, **schema}
    if unit is not None:
        metadata["unit"] = unit
    return metadata


@dataclass(frozen=True, slots=True)
class CatalogSource:
    path: Path = field(
        metadata=_meta(
            "Catalog file. Relative paths resolve against the directory of the scenario file."
        )
    )
    format: Literal["celestrak_gp_csv"] = field(metadata=_meta("Catalog file format."))
    source_url: str = field(metadata=_meta("Absolute HTTP(S) URL the catalog was retrieved from."))
    retrieved_at_utc: datetime = field(metadata=_meta("Retrieval time, ISO 8601 UTC ending in Z."))
    terms_url: str = field(
        metadata=_meta("Absolute HTTP(S) URL of the data provider's terms of use.")
    )
    sha256: str = field(
        metadata=_meta("SHA-256 of the catalog file, 64 lowercase hexadecimal characters.")
    )


@dataclass(frozen=True, slots=True)
class EventSettings:
    enabled: bool = field(
        default=True,
        metadata=_meta(
            "Locate visibility, MODCOD and routing event instants within the tolerance.",
            default=True,
        ),
    )
    tolerance_s: float = field(
        default=1e-3,
        metadata=_meta(
            "Root-finding tolerance for event times.", "s", minimum=1e-6, maximum=1.0, default=1e-3
        ),
    )


@dataclass(frozen=True, slots=True)
class ConvergenceSettings:
    mode: Literal["error", "warn", "off"] = field(
        default="error",
        metadata=_meta(
            "Reaction to a failed refinement gate. 'off' is recorded in every artefact.",
            default="error",
        ),
    )
    refinement_factor: int = field(
        default=4,
        metadata=_meta(
            "Factor by which the convergence gate refines the sample grid.",
            "dimensionless",
            minimum=2,
            maximum=16,
            default=4,
        ),
    )
    relative_tolerance: float = field(
        default=0.01,
        metadata=_meta(
            "Largest accepted relative change of a gated statistic under refinement.",
            "dimensionless",
            exclusiveMinimum=0.0,
            maximum=1.0,
            default=0.01,
        ),
    )
    absolute_tolerance_bits: float = field(
        default=1.0,
        metadata=_meta(
            "Largest accepted absolute change of delivered bits under refinement.",
            "bit",
            exclusiveMinimum=0.0,
            default=1.0,
        ),
    )


@dataclass(frozen=True, slots=True)
class TimeConfig:
    start_utc: datetime = field(metadata=_meta("First sample time, ISO 8601 UTC ending in Z."))
    stop_utc: datetime = field(
        metadata=_meta("Last sample time, ISO 8601 UTC ending in Z. After start_utc.")
    )
    step_s: float = field(
        metadata=_meta(
            "Sample spacing: a whole number of microseconds, at most the window length. The window "
            "is at most 31 days and the grid at most 1,000,000 samples including the stop point.",
            "s",
            exclusiveMinimum=0.0,
        )
    )
    events: EventSettings = field(metadata=_meta("Event localisation.", default={}))
    convergence: ConvergenceSettings = field(metadata=_meta("Grid convergence gate.", default={}))


@dataclass(frozen=True, slots=True)
class SpaceSegment:
    catalogs: tuple[CatalogSource, ...] = field(
        metadata=_meta(
            "Orbital element catalogs, read by the engine and checked against their SHA-256.",
            minItems=1,
        )
    )
    select_norad_ids: tuple[int, ...] | None = field(
        metadata=_meta(
            "NORAD catalog numbers to keep, or null for every object in the catalogs.",
            minItems=1,
            uniqueItems=True,
            items={"minimum": 1, "maximum": MAX_NORAD_ID},
            default=None,
        )
    )
    synthetic_rf: bool = field(
        metadata=_meta(
            "True when the radio block is declared rather than the named operator's real RF. "
            "Must be true when a real operator's catalog is paired with a declared RF block."
        )
    )


@dataclass(frozen=True, slots=True)
class Terminal:
    receive_gain_dbi: float = field(
        metadata=_meta("Receive antenna gain toward the satellite.", "dBi")
    )
    system_noise_temperature_k: float = field(
        metadata=_meta("Receive system noise temperature.", "K", exclusiveMinimum=0.0)
    )
    miscellaneous_loss_db: float = field(
        metadata=_meta("Fixed implementation and pointing loss.", "dB", minimum=0.0)
    )


@dataclass(frozen=True, slots=True)
class Station:
    name: str = field(
        metadata=_meta("Unique station name; routes and hydrometeor states refer to it.")
    )
    latitude_deg: float = field(
        metadata=_meta("Geodetic latitude.", "deg", minimum=-90.0, maximum=90.0)
    )
    longitude_deg: float = field(
        metadata=_meta("Geodetic longitude.", "deg", minimum=-180.0, maximum=180.0)
    )
    height_ellipsoid_m: float = field(
        metadata=_meta("Geometric height above the WGS84 ellipsoid.", "m")
    )
    height_amsl_m: float | None = field(
        metadata=_meta(
            "Height above mean sea level. Required when propagation.model is not free_space.",
            "m",
            minimum=0.0,
            maximum=10_000.0,
            default=None,
        )
    )
    terminal: str = field(
        metadata=_meta("Name of an entry of the scenario's terminals.", default="default")
    )
    minimum_elevation_deg: float = field(
        metadata=_meta(
            "Elevation mask. Null or omitted takes ground_segment.default_minimum_elevation_deg, "
            "resolved at parse. At least 5 when propagation.model is itu_reference.",
            "deg",
            minimum=0.0,
            exclusiveMaximum=90.0,
            default=None,
        )
    )


@dataclass(frozen=True, slots=True)
class GroundSegment:
    default_minimum_elevation_deg: float = field(
        metadata=_meta(
            "Elevation mask of stations that do not set their own.",
            "deg",
            minimum=0.0,
            exclusiveMaximum=90.0,
        )
    )
    stations: tuple[Station, ...] = field(
        metadata=_meta("Ground stations.", minItems=1, maxItems=16)
    )


@dataclass(frozen=True, slots=True)
class RadioConfig:
    carrier_frequency_hz: float = field(
        metadata=_meta("Carrier frequency.", "Hz", exclusiveMinimum=0.0)
    )
    channel_bandwidth_hz: float = field(
        metadata=_meta(
            "Channel bandwidth, at least symbol_rate_baud * (1 + rolloff).",
            "Hz",
            exclusiveMinimum=0.0,
        )
    )
    eirp_dbw: float = field(metadata=_meta("Satellite effective isotropic radiated power.", "dBW"))


@dataclass(frozen=True, slots=True)
class Adaptation:
    standard: Literal["dvb-s2"] = field(metadata=_meta("Adaptive coding and modulation standard."))
    table: Literal["v1-reference-subset"] = field(
        metadata=_meta("MODCOD table. S1 carries the five v1 reference rows.")
    )
    symbol_rate_baud: float = field(metadata=_meta("Symbol rate.", "baud", exclusiveMinimum=0.0))
    rolloff: float = field(
        metadata=_meta("Root-raised-cosine roll-off.", "dimensionless", minimum=0.0, maximum=1.0)
    )
    implementation_margin_db: float = field(
        metadata=_meta("Margin subtracted from Es/N0 before MODCOD selection.", "dB", minimum=0.0)
    )
    hysteresis_db: float = field(
        metadata=_meta("Es/N0 margin required to step up a MODCOD.", "dB", minimum=0.0)
    )
    feedback_delay_s: float = field(
        default=0.0,
        metadata=_meta(
            "Delay before a measured Es/N0 changes the MODCOD.",
            "s",
            minimum=0.0,
            maximum=10.0,
            default=0.0,
        ),
    )

    def kernel(self) -> AdaptationConfig:
        """The v1 selection parameters, validated by the physics kernel."""
        return AdaptationConfig(
            self.symbol_rate_baud,
            self.rolloff,
            self.implementation_margin_db,
            self.hysteresis_db,
        )


@dataclass(frozen=True, slots=True)
class Propagation:
    model: Literal["free_space", "itu_reference"] = field(
        metadata=_meta("Atmospheric propagation model.")
    )
    refinement: int = field(
        default=1,
        metadata=_meta(
            "Split-layer refinement of the ray trace.", "dimensionless", enum=(1, 2, 4), default=1
        ),
    )
    hydrometeors: HydrometeorConfig | None = field(
        default=None,
        metadata=_meta(
            "Declared uniform cloud and rain layers per station. Requires itu_reference.",
            default=None,
        ),
    )


@dataclass(frozen=True, slots=True)
class IslConfig:
    rule: Literal["all_pairs_within_range"] = field(metadata=_meta("Inter-satellite link rule."))
    max_range_m: float = field(
        metadata=_meta("Longest inter-satellite link; zero disables the links.", "m", minimum=0.0)
    )
    capacity_bps: float = field(
        metadata=_meta("Capacity of every inter-satellite link.", "bit/s", exclusiveMinimum=0.0)
    )


@dataclass(frozen=True, slots=True)
class RoutePair:
    source: str = field(metadata=_meta("Name of the source station."))
    target: str = field(metadata=_meta("Name of the target station, different from the source."))


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    pairs: tuple[RoutePair, ...] = field(
        metadata=_meta(
            "Ordered source-target station pairs to route.",
            minItems=1,
            maxItems=16,
            uniqueItems=True,
        )
    )
    isl: IslConfig = field(metadata=_meta("Inter-satellite links."))
    fixed_capacity_bps: float | None = field(
        metadata=_meta(
            "Ground-link capacity of the fixed_capacity baseline. Required exactly when "
            "route_models lists fixed_capacity, otherwise null or omitted.",
            "bit/s",
            exclusiveMinimum=0.0,
            default=None,
        )
    )
    route_models: tuple[str, ...] = field(
        metadata=_meta(
            "Routing models to evaluate: a non-empty set of minimum_delay, maximum_rate and "
            "fixed_capacity.",
            minItems=1,
            maxItems=len(ROUTE_MODELS),
            uniqueItems=True,
            items={"enum": ROUTE_MODELS},
        )
    )


@dataclass(frozen=True, slots=True)
class OutputConfig:
    formats: tuple[Literal["csv", "olc", "parquet"], ...] = field(
        default=("csv", "olc"),
        metadata=_meta(
            "Table formats to write.",
            minItems=1,
            maxItems=3,
            uniqueItems=True,
            default=["csv", "olc"],
        ),
    )
    satellite_positions: Literal["grid", "none"] = field(
        default="grid",
        metadata=_meta(
            "Whether satellite positions are written at every grid sample.", default="grid"
        ),
    )


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str = field(metadata=_meta("Scenario title."))
    description: str = field(metadata=_meta("Free text.", default=""))
    time: TimeConfig = field(metadata=_meta("Time grid, events and convergence gate."))
    space_segment: SpaceSegment = field(metadata=_meta("Orbital catalogs."))
    terminals: tuple[tuple[str, Terminal], ...] = field(
        metadata=_meta("Named receive terminals. A station's terminal defaults to 'default'.")
    )
    ground_segment: GroundSegment = field(metadata=_meta("Ground stations and elevation masks."))
    radio: RadioConfig = field(metadata=_meta("Downlink radio parameters."))
    adaptation: Adaptation = field(metadata=_meta("Adaptive coding and modulation."))
    propagation: Propagation = field(metadata=_meta("Atmospheric propagation."))
    network: NetworkConfig | None = field(
        metadata=_meta("Routing between stations. Null or omitted disables routing.", default=None)
    )
    outputs: OutputConfig = field(metadata=_meta("Result tables.", default={}))
    source_path: Path = field(
        metadata=_meta("Resolved path of the scenario file (not part of the document).")
    )
    source_sha256: str = field(
        metadata=_meta("SHA-256 of the scenario file bytes (not part of the document).")
    )

    def terminal_for(self, station: Station) -> Terminal:
        """The terminal a station of this scenario uses."""
        for name, terminal in self.terminals:
            if name == station.terminal:
                return terminal
        raise ValueError(f"station {station.name!r} names unknown terminal {station.terminal!r}")
