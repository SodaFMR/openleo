"""JSON Schema (draft 2020-12) for scenario v2, generated from the model's field metadata.

``scenario_json_schema`` walks the dataclasses of ``openleo.scenario.model``:

- the keys of an object are the dataclass fields, and a field is required exactly when its
  metadata has no ``default`` (the rule the parser applies); a ``None`` default also allows null;
- ``Literal`` becomes ``enum``, ``tuple[X, ...]`` an ``array``, ``tuple[tuple[str, X], ...]`` an
  object keyed by name, ``X | None`` an ``anyOf`` with ``null`` and ``datetime`` a UTC string;
- the metadata keys the parser reads (bounds, ``enum``, ``minItems``, ``maxItems``,
  ``uniqueItems``, ``items``) are copied as they are, ``default`` is published, and
  ``description`` takes the unit as `` [unit]``;
- the string rules the parser enforces and the metadata does not carry (non-blank names,
  SHA-256, HTTP(S) URLs, UTC timestamps) are declared once in ``_STRINGS`` below. A string field
  missing from that table is an error, so a new field cannot silently lose its rules.

The hydrometeor block belongs to the physics module, which has no metadata, so ``_hydrometeors``
describes it here; ``test_schema`` holds its bounds to those of the physics kernels.

JSON Schema cannot express every rule. Cross-field rules it can (``if``/``then``/``else``) are
in ``_OBJECT_RULES`` and ``_root_rules``; the rest are parser-only and listed in the schema's
top-level ``description``. The parser stays authoritative.
"""

import dataclasses
from datetime import datetime
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from openleo.physics.gases import MAX_FREQUENCY_HZ, MIN_FREQUENCY_HZ
from openleo.physics.hydrometeor_path import HydrometeorConfig
from openleo.scenario.model import (
    NOT_DOCUMENT_FIELDS,
    CatalogSource,
    NetworkConfig,
    Propagation,
    RoutePair,
    Scenario,
    Station,
)
from openleo.scenario.parse import (
    HYDROMETEOR_FREQUENCY_HZ,
    ITU_MINIMUM_ELEVATION_DEG,
    MAX_GRID_SAMPLES,
    MAX_WINDOW,
    SCENARIO_SCHEMA_ID,
)

DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_URL = "https://github.com/SodaFMR/openleo/schemas/scenario-2.schema.json"

_SCALAR_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum")
_ARRAY_KEYWORDS = ("minItems", "maxItems", "uniqueItems")
_JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string"}

# String fragments. Non-blank: the parser rejects "" and whitespace-only strings (\S matches a
# character that is not whitespace). The others restate the rules of openleo._validation.
_TEXT = {"type": "string"}
_NON_BLANK = {"type": "string", "minLength": 1, "pattern": "\\S"}
_URL = {"type": "string", "format": "uri", "pattern": "^https?://"}
_SHA256 = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[0-9a-f]{64}$"}
_TIMESTAMP = {
    "type": "string",
    "format": "date-time",
    # Each field in its range. A day that its month lacks (February 30) is parser-only.
    "pattern": (
        "^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
        "T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\\.[0-9]+)?Z$"
    ),
}
_STRINGS: dict[tuple[type, str], dict[str, Any]] = {
    (Scenario, "name"): _NON_BLANK,
    (Scenario, "description"): _TEXT,
    (CatalogSource, "source_url"): _URL,
    (CatalogSource, "terms_url"): _URL,
    (CatalogSource, "sha256"): _SHA256,
    (Station, "name"): _NON_BLANK,
    (Station, "terminal"): _NON_BLANK,
    (RoutePair, "source"): _NON_BLANK,
    (RoutePair, "target"): _NON_BLANK,
    (NetworkConfig, "route_models"): _TEXT,  # the members come from the metadata's items.enum
}

_ITU = "itu_reference"
_FIXED_CAPACITY = "fixed_capacity"

# Cross-field rules of one object that the schema expresses. The parser enforces the same rules.
_OBJECT_RULES: dict[type, dict[str, Any]] = {
    # hydrometeors only with itu_reference
    Propagation: {
        "if": {"properties": {"model": {"const": _ITU}}, "required": ["model"]},
        "else": {"properties": {"hydrometeors": {"type": "null"}}},
    },
    # fixed_capacity_bps present (not null) exactly when route_models lists fixed_capacity
    NetworkConfig: {
        "if": {
            "properties": {"route_models": {"contains": {"const": _FIXED_CAPACITY}}},
            "required": ["route_models"],
        },
        "then": {
            "required": ["fixed_capacity_bps"],
            "properties": {"fixed_capacity_bps": {"type": "number"}},
        },
        "else": {"properties": {"fixed_capacity_bps": {"type": "null"}}},
    },
}


def scenario_json_schema() -> dict[str, Any]:
    """The JSON Schema of a scenario v2 document. Each call builds a new, plain-JSON dict."""
    schema = _object_schema(
        Scenario,
        leading={
            "schema": {
                "type": "string",
                "const": SCENARIO_SCHEMA_ID,
                "description": f"Document format version, exactly {SCENARIO_SCHEMA_ID!r}.",
            }
        },
    )
    return {
        "$schema": DRAFT,
        "$id": SCHEMA_URL,
        "title": "OpenLEO scenario v2",
        "description": _description(),
        **schema,
        "allOf": _root_rules(),
    }


def _object_schema(owner: type, *, leading: dict[str, Any] | None = None) -> dict[str, Any]:
    hints = get_type_hints(owner)
    properties: dict[str, Any] = dict(leading or {})
    required = list(properties)
    for item in dataclasses.fields(owner):
        if item.name in NOT_DOCUMENT_FIELDS:
            continue
        properties[item.name] = _property(hints[item.name], owner, item)
        if "default" not in item.metadata:
            required.append(item.name)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
        **_plain(_OBJECT_RULES.get(owner, {})),
    }


def _property(annotation: Any, owner: type, item: dataclasses.Field) -> dict[str, Any]:
    meta = item.metadata
    node = _schema_for(_without_none(annotation), owner, item.name, meta)
    if "default" in meta and meta["default"] is None:
        node = {"anyOf": [node, {"type": "null"}]}
    node["description"] = meta["description"] + (f" [{meta['unit']}]" if "unit" in meta else "")
    if "default" in meta:
        node["default"] = _plain(meta["default"])
    return node


def _without_none(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, UnionType):
        members = [member for member in get_args(annotation) if member is not NoneType]
        if len(members) == 1:
            return members[0]
    return annotation


def _schema_for(annotation: Any, owner: type, name: str, meta: dict[str, Any]) -> dict[str, Any]:
    where = f"{owner.__name__}.{name}"
    if annotation is HydrometeorConfig:
        return _hydrometeors()
    if dataclasses.is_dataclass(annotation):
        return _object_schema(annotation)
    if get_origin(annotation) is tuple:
        element = get_args(annotation)[0]
        if get_origin(element) is tuple:  # tuple[tuple[str, X], ...]: an object keyed by name
            key, value = get_args(element)
            if key is not str:
                raise TypeError(f"{where}: a map needs str keys, not {key!r}")
            return {
                "type": "object",
                "propertyNames": dict(_NON_BLANK),
                "additionalProperties": _schema_for(value, owner, name, {}),
            }
        items = _schema_for(element, owner, name, {})
        items.update(_plain(meta.get("items", {})))
        node = {"type": "array", "items": items}
        node.update({key: meta[key] for key in _ARRAY_KEYWORDS if key in meta})
        return node
    node = _scalar(annotation, owner, name, where)
    node.update({key: _plain(meta[key]) for key in _SCALAR_KEYWORDS if key in meta})
    return node


def _scalar(annotation: Any, owner: type, name: str, where: str) -> dict[str, Any]:
    if get_origin(annotation) is Literal:
        values = get_args(annotation)
        return {"type": _JSON_TYPES[type(values[0])], "enum": list(values)}
    if annotation in (bool, int, float):
        return {"type": _JSON_TYPES[annotation]}
    if annotation is datetime:
        return dict(_TIMESTAMP)
    if annotation is Path:
        return dict(_NON_BLANK)
    if annotation is str and (owner, name) in _STRINGS:
        return dict(_STRINGS[owner, name])
    raise TypeError(f"{where}: no JSON Schema for {annotation!r}; declare its strings in _STRINGS")


def _plain(value: Any) -> Any:
    """A deep copy as plain JSON: tuples become lists."""
    if isinstance(value, dict):
        return {key: _plain(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(entry) for entry in value]
    return value


def _hydrometeors() -> dict[str, Any]:
    """``HydrometeorConfig``: bounds as in ``HydrometeorStation.__post_init__``."""

    def number(description: str, unit: str, **bounds: float) -> dict[str, Any]:
        return {"type": "number", **bounds, "description": f"{description} [{unit}]"}

    state = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "liquid_water_kg_m2",
            "rain_rate_mm_h",
            "rain_top_height_amsl_m",
            "polarization_tilt_deg",
        ],
        "properties": {
            "liquid_water_kg_m2": number(
                "Integrated cloud liquid water of the column above the station.",
                "kg/m^2",
                minimum=0.0,
            ),
            "rain_rate_mm_h": number("Rain rate at the station.", "mm/h", minimum=0.0),
            "rain_top_height_amsl_m": number(
                "Height of the top of the uniform rain layer above mean sea level, at least the "
                "station's height_amsl_m.",
                "m",
                minimum=0.0,
                maximum=20_000.0,
            ),
            "polarization_tilt_deg": number(
                "Polarisation tilt angle: 0 horizontal, 90 vertical.",
                "deg",
                minimum=0.0,
                maximum=180.0,
            ),
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["model", "stations"],
        "properties": {
            "model": {
                "type": "string",
                "const": "declared_uniform_layers",
                "description": "Hydrometeor model: declared uniform cloud and rain layers.",
            },
            "stations": {
                "type": "object",
                "minProperties": 1,
                "propertyNames": dict(_NON_BLANK),
                "additionalProperties": state,
                "description": (
                    "Declared state per ground station, keyed by station name. Every station "
                    "appears exactly once."
                ),
            },
        },
    }


def _root_rules() -> list[dict[str, Any]]:
    """Cross-field rules between sections: conditions on propagation, constraints elsewhere."""

    def propagation(**properties: Any) -> dict[str, Any]:
        return {
            "properties": {"propagation": {"properties": properties, "required": list(properties)}},
            "required": ["propagation"],
        }

    def carrier(lower: float, upper: float) -> dict[str, Any]:
        return {
            "radio": {"properties": {"carrier_frequency_hz": {"minimum": lower, "maximum": upper}}}
        }

    def masks(**mask: Any) -> dict[str, Any]:
        return {"properties": {"stations": {"items": mask}}}

    mask_floor = {"type": "number", "minimum": ITU_MINIMUM_ELEVATION_DEG}
    return [
        {  # itu_reference: carrier domain, station heights and elevation masks
            "if": propagation(model={"const": _ITU}),
            "then": {
                "properties": {
                    **carrier(MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ),
                    "ground_segment": {
                        **masks(
                            required=["height_amsl_m"],
                            properties={"height_amsl_m": {"type": "number"}},
                        ),
                        # A station without its own mask takes the default, which must then
                        # meet the floor; with a default below it, every station sets its own.
                        "if": {
                            "properties": {"default_minimum_elevation_deg": mask_floor},
                            "required": ["default_minimum_elevation_deg"],
                        },
                        "then": masks(
                            properties={
                                "minimum_elevation_deg": {
                                    "anyOf": [
                                        {"type": "null"},
                                        {"minimum": ITU_MINIMUM_ELEVATION_DEG},
                                    ]
                                }
                            }
                        ),
                        "else": masks(
                            required=["minimum_elevation_deg"],
                            properties={"minimum_elevation_deg": mask_floor},
                        ),
                    },
                }
            },
        },
        {  # declared hydrometeors: the narrower carrier domain of the cloud and rain models
            "if": propagation(hydrometeors={"type": "object"}),
            "then": {"properties": carrier(*HYDROMETEOR_FREQUENCY_HZ)},
        },
    ]


def _description() -> str:
    ghz = (MIN_FREQUENCY_HZ / 1e9, MAX_FREQUENCY_HZ / 1e9)
    ghz_hydro = tuple(value / 1e9 for value in HYDROMETEOR_FREQUENCY_HZ)
    return (
        f"OpenLEO scenario document, format {SCENARIO_SCHEMA_ID}. This schema checks structure, "
        "types, bounds and the cross-field rules JSON Schema can express. The OpenLEO parser "
        "(openleo.scenario.parse) is authoritative: it enforces every rule of this schema and "
        "the parser-only cross-field rules, which JSON Schema cannot express. "
        "Parser-only rules: "
        "(1) radio.channel_bandwidth_hz >= adaptation.symbol_rate_baud * (1 + adaptation.rolloff); "
        "(2) adaptation.symbol_rate_baud × maximum MODCOD efficiency must be finite "
        "(overflow guard); "
        "(3) a station's terminal must name an entry of terminals ('default' when omitted); "
        "(4) station names must be unique; "
        "(5) pairs name ground stations, and the source and target of a pair differ; "
        "(6) hydrometeors.stations names every ground station exactly once, and each "
        "rain_top_height_amsl_m is at least that station's height_amsl_m; "
        "(7) time.stop_utc after start_utc, a window of at most "
        f"{MAX_WINDOW.days} days, step_s at most the window and a whole number of microseconds, "
        f"at most {MAX_GRID_SAMPLES:,} samples including the stop point, and every timestamp a "
        "real calendar date; "
        "(8) step_s is at most one eighth of the shortest orbital period in the catalogs, which "
        "needs the catalogs and is checked when they are loaded; "
        "(9) the catalog sha256 matches the catalog file, there are no duplicate NORAD ids across "
        "catalogs, and the catalogs stay within their size limits (checked when they are read); "
        "(10) a scenario file of at most 1 MB with no duplicate JSON object keys and no "
        "non-finite numbers (properties of the JSON text, not of a parsed instance). "
        "Also expressed by this schema with if/then/else, and listed because the parser "
        "is authoritative: "
        "(a) fixed_capacity_bps is present (not null) exactly when route_models lists "
        f"{_FIXED_CAPACITY}; "
        f"(b) with propagation.model {_ITU}, every station has height_amsl_m and an elevation "
        f"mask, its own or the default, of at least {ITU_MINIMUM_ELEVATION_DEG:g} deg, and "
        f"radio.carrier_frequency_hz lies in [{ghz[0]:g}, {ghz[1]:g}] GHz; "
        f"(c) propagation.hydrometeors requires {_ITU} and "
        f"radio.carrier_frequency_hz in [{ghz_hydro[0]:g}, {ghz_hydro[1]:g}] GHz."
    )
