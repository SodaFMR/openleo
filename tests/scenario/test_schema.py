"""Scenario v2 JSON Schema: generated from the model, committed, and consistent with the parser.

The parser is authoritative. The schema repeats the rules JSON Schema can express, and these
tests hold the two to the same verdict: on the fixture corpus, on every numeric, enumerated and
boolean leaf of the document (probed at its bounds and just outside them), on the string formats
and on the cross-field rules the schema expresses with ``if``/``then``/``else``.
"""

import dataclasses
import json
import math
import random
import re
import types
import typing
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from openleo._validation import _url
from openleo.scenario import model, scenario_json_schema
from openleo.scenario import schema as schema_module
from openleo.scenario.parse import load_scenario, parse_scenario

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "docs" / "schemas" / "scenario-2.schema.json"
FIXTURES = Path(__file__).parent / "fixtures"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))
SCHEMA_ID = "https://github.com/SodaFMR/openleo/schemas/scenario-2.schema.json"
SHA = "0" * 64
MISSING = object()  # an edit that deletes the key

SCHEMA = scenario_json_schema()
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def schema_accepts(document: Any) -> bool:
    return VALIDATOR.is_valid(document)


def parser_accepts(document: Any, directory: Path) -> bool:
    try:
        parse_scenario(document, directory / "scenario.json", SHA)
    except ValueError:
        return False
    return True


def _spec_example() -> dict:
    return json.loads((FIXTURES / "valid" / "spec_example_itu_hydrometeors.json").read_text())


def _free_space_base() -> dict:
    """A valid document no cross-field rule binds: free space, a wide channel, no hydrometeors."""
    document = _spec_example()
    document["propagation"] = {"model": "free_space"}
    document["radio"]["channel_bandwidth_hz"] = 1.0e7
    return document


def _edit(document: dict, *edits: tuple[str, Any]) -> dict:
    for pointer, value in edits:
        *parents, last = (
            int(token) if token.isdigit() else token.replace("~1", "/").replace("~0", "~")
            for token in pointer.strip("/").split("/")
        )
        node = document
        for token in parents:
            node = node[token]
        if value is MISSING:
            del node[last]
        else:
            node[last] = value
    return document


# --- the committed file ---------------------------------------------------------------------


def test_committed_schema_is_current() -> None:
    expected = json.dumps(scenario_json_schema(), indent=2, sort_keys=True) + "\n"
    assert COMMITTED.read_text(encoding="utf-8") == expected


def test_schema_is_valid_draft_2020_12_and_plain_json() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert SCHEMA["$id"] == SCHEMA_ID
    # No tuples, enums as lists: the schema is exactly what a JSON round trip returns.
    assert json.loads(json.dumps(SCHEMA)) == SCHEMA


def test_each_call_builds_an_independent_schema() -> None:
    first = scenario_json_schema()
    first["properties"]["name"]["minLength"] = 99
    first["required"].clear()
    assert scenario_json_schema() == SCHEMA


def test_schema_is_published_by_the_scenario_package() -> None:
    import openleo.scenario

    assert openleo.scenario.scenario_json_schema is schema_module.scenario_json_schema
    assert "scenario_json_schema" in openleo.scenario.__all__


# --- structure --------------------------------------------------------------------------------


def _subschemas(node: Any):
    """Every schema object nested anywhere under ``node``, including ``node``."""
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in {"properties", "$defs"}:
                for child in value.values():
                    yield from _subschemas(child)
            elif key in {"enum", "const", "default", "required"}:
                continue  # values, not schemas
            else:
                yield from _subschemas(value)
    elif isinstance(node, list):
        for child in node:
            yield from _subschemas(child)


def test_every_object_with_named_properties_forbids_additional_ones() -> None:
    objects = [node for node in _subschemas(SCHEMA) if "properties" in node]
    assert len(objects) >= 20
    for node in objects:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, node
            assert sorted(node["required"]) == sorted(set(node["required"]))
            assert set(node["required"]) <= set(node["properties"])


def test_maps_constrain_their_keys_and_values() -> None:
    terminals = SCHEMA["properties"]["terminals"]
    assert terminals["type"] == "object"
    assert terminals["propertyNames"] == {"type": "string", "minLength": 1, "pattern": "\\S"}
    assert terminals["additionalProperties"]["required"] == [
        "receive_gain_dbi",
        "system_noise_temperature_k",
        "miscellaneous_loss_db",
    ]
    hydrometeors = SCHEMA["properties"]["propagation"]["properties"]["hydrometeors"]
    stations = hydrometeors["anyOf"][0]["properties"]["stations"]
    assert stations["minProperties"] == 1
    assert stations["propertyNames"] == terminals["propertyNames"]
    assert set(stations["additionalProperties"]["properties"]) == {
        "liquid_water_kg_m2",
        "rain_rate_mm_h",
        "rain_top_height_amsl_m",
        "polarization_tilt_deg",
    }


def test_root_properties_required_keys_and_nullable_fields() -> None:
    properties = SCHEMA["properties"]
    assert list(properties) == [
        "schema",
        "name",
        "description",
        "time",
        "space_segment",
        "terminals",
        "ground_segment",
        "radio",
        "adaptation",
        "propagation",
        "network",
        "outputs",
    ]
    assert SCHEMA["required"] == [
        "schema",
        "name",
        "time",
        "space_segment",
        "terminals",
        "ground_segment",
        "radio",
        "adaptation",
        "propagation",
    ]
    assert properties["schema"]["const"] == "openleo.scenario/2"
    # `default` in the field metadata means the key is optional; a None default also allows null.
    assert [branch.get("type") for branch in properties["network"]["anyOf"]] == ["object", "null"]
    assert properties["network"]["default"] is None
    assert properties["outputs"]["default"] == {}
    station = SCHEMA["properties"]["ground_segment"]["properties"]["stations"]["items"]
    assert station["required"] == ["name", "latitude_deg", "longitude_deg", "height_ellipsoid_m"]
    mask = station["properties"]["minimum_elevation_deg"]
    assert mask["anyOf"] == [
        {"type": "number", "minimum": 0.0, "exclusiveMaximum": 90.0},
        {"type": "null"},
    ]
    assert mask["default"] is None
    assert station["properties"]["terminal"]["default"] == "default"


def test_descriptions_carry_units_and_defaults_are_published() -> None:
    step = SCHEMA["properties"]["time"]["properties"]["step_s"]
    assert step["description"].endswith(" [s]")
    assert step["exclusiveMinimum"] == 0.0
    tolerance = SCHEMA["properties"]["time"]["properties"]["events"]["properties"]["tolerance_s"]
    assert tolerance == {
        "type": "number",
        "minimum": 1e-6,
        "maximum": 1.0,
        "default": 1e-3,
        "description": "Root-finding tolerance for event times. [s]",
    }
    network = SCHEMA["properties"]["network"]["anyOf"][0]["properties"]
    assert network["route_models"]["items"] == {
        "type": "string",
        "enum": ["minimum_delay", "maximum_rate", "fixed_capacity"],
    }
    assert network["route_models"]["uniqueItems"] is True
    assert network["pairs"]["maxItems"] == 16
    outputs = SCHEMA["properties"]["outputs"]["properties"]
    assert outputs["formats"]["items"] == {"type": "string", "enum": ["csv", "olc", "parquet"]}
    assert outputs["formats"]["default"] == ["csv", "olc"]
    norad = SCHEMA["properties"]["space_segment"]["properties"]["select_norad_ids"]
    assert norad["anyOf"][0]["items"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": model.MAX_NORAD_ID,
    }


def _nested(owner: type, name: str, node: dict) -> tuple[type, dict] | None:
    """The model class a field holds (or holds a tuple or name map of) and its schema."""
    hint = typing.get_type_hints(owner)[name]
    candidate = hint
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        candidate = next(arg for arg in typing.get_args(hint) if arg is not type(None))
    branch = node["anyOf"][0] if "anyOf" in node else node
    if typing.get_origin(candidate) is tuple:
        candidate = typing.get_args(candidate)[0]
        if typing.get_origin(candidate) is tuple:  # (name, value) pairs: an object keyed by name
            candidate, branch = typing.get_args(candidate)[1], branch["additionalProperties"]
        else:
            branch = branch["items"]
    if dataclasses.is_dataclass(candidate) and candidate.__module__ == model.__name__:
        return candidate, branch
    return None


def test_every_field_of_the_model_is_in_the_schema() -> None:
    """Walk the dataclasses and the schema together: keys, required, defaults, units."""

    def check(owner: type, node: dict) -> int:
        fields = [
            item for item in dataclasses.fields(owner) if item.name not in model.NOT_DOCUMENT_FIELDS
        ]
        extra = {"schema"} if owner is model.Scenario else set()
        assert set(node["properties"]) == {item.name for item in fields} | extra, owner
        assert (
            set(node["required"])
            == {item.name for item in fields if "default" not in item.metadata} | extra
        ), owner
        checked = 1
        for item in fields:
            child = node["properties"][item.name]
            meta = item.metadata
            assert child["description"].startswith(meta["description"]), item.name
            assert ("default" in child) == ("default" in meta), item.name
            if "unit" in meta:
                assert child["description"].endswith(f" [{meta['unit']}]"), item.name
            nested = _nested(owner, item.name, child)
            if nested is not None:
                checked += check(*nested)
        return checked

    assert check(model.Scenario, SCHEMA) == 16  # every dataclass of the model


def test_every_nullable_annotation_has_a_null_default() -> None:
    for owner in (cls for cls in vars(model).values() if dataclasses.is_dataclass(cls)):
        hints = typing.get_type_hints(owner)
        for item in dataclasses.fields(owner):
            if type(None) in typing.get_args(hints[item.name]):
                assert item.metadata["default"] is None, f"{owner.__name__}.{item.name}"


def test_description_lists_the_parser_only_rules() -> None:
    description = SCHEMA["description"]
    for phrase in (
        "radio.channel_bandwidth_hz >= adaptation.symbol_rate_baud * (1 + adaptation.rolloff)",
        "adaptation.symbol_rate_baud \u00d7 maximum MODCOD efficiency must be finite (overflow guard)",
        "must name an entry of terminals",
        "station names must be unique",
        "pairs name ground stations, and the source and target of a pair differ",
        "hydrometeors.stations names every ground station exactly once",
        "rain_top_height_amsl_m is at least that station's height_amsl_m",
        "time.stop_utc after start_utc",
        "a window of at most 31 days",
        "at most 1,000,000 samples including the stop point",
        "step_s is at most one eighth of the shortest orbital period",
        "the catalog sha256 matches the catalog file",
        "no duplicate NORAD ids across catalogs",
        "no duplicate JSON object keys",
        "fixed_capacity_bps is present (not null) exactly when route_models lists fixed_capacity",
        "of at least 5 deg",
        "every station has height_amsl_m",
        "propagation.hydrometeors requires itu_reference",
        "the parser alone accepts IPv6-literal hosts, non-ASCII hosts",
    ):
        assert phrase in description, phrase


# --- agreement with the parser ----------------------------------------------------------------


def _loads_text(text: str, directory: Path) -> bool:
    """Whether ``load_scenario`` accepts a file with this text (it also sees duplicate keys)."""
    path = directory / "scenario.json"
    path.write_text(text, encoding="utf-8")
    try:
        load_scenario(path)
    except ValueError:
        return False
    return True


def test_schema_and_parser_agree_on_corpus(tmp_path: Path) -> None:
    problems: list[str] = []
    expressible = 0
    for path in VALID:
        document = json.loads(path.read_text(encoding="utf-8"))
        errors = [error.message for error in VALIDATOR.iter_errors(document)]
        if errors:
            problems.append(f"valid/{path.stem}: schema rejects: {errors[0][:100]}")
        try:
            load_scenario(path)
        except ValueError as exc:
            problems.append(f"valid/{path.stem}: parser rejects: {exc}")

    for path in INVALID:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        flag = fixture.get("schema_expressible")
        if not isinstance(flag, bool):
            problems.append(f"invalid/{path.stem}: schema_expressible must be true or false")
            continue
        if "scenario_text" in fixture:
            document = json.loads(fixture["scenario_text"])
            parser_says = _loads_text(fixture["scenario_text"], tmp_path)
        else:
            document = fixture["scenario"]
            parser_says = parser_accepts(document, tmp_path)
        if parser_says:
            problems.append(f"invalid/{path.stem}: the parser accepts it")
        schema_rejects = not schema_accepts(document)
        if flag:
            expressible += 1
            if not schema_rejects:
                problems.append(f"invalid/{path.stem}: expressible, but the schema accepts it")
        else:
            # An honest marker: a parser-only rule is one the schema does not (yet) express.
            if schema_rejects:
                problems.append(f"invalid/{path.stem}: marked parser-only, but the schema rejects")
            if not str(fixture.get("parser_only_rule", "")).strip():
                problems.append(f"invalid/{path.stem}: parser_only_rule must say why")
        if flag and "parser_only_rule" in fixture:
            problems.append(f"invalid/{path.stem}: parser_only_rule on an expressible fixture")

    assert not problems, "\n".join(problems)
    assert expressible >= 60  # the schema carries most rules, not just the shape


def _leaves(node: dict, value: Any, pointer: str = ""):
    """(pointer, schema) of every boolean, number, constant and enum the document holds."""
    if "anyOf" in node:
        node = next(branch for branch in node["anyOf"] if branch.get("type") != "null")
    kind = node.get("type")
    if kind == "object" and isinstance(value, dict):
        if "properties" in node:
            for key, child in node["properties"].items():
                if key in value:
                    yield from _leaves(child, value[key], f"{pointer}/{key}")
        else:  # a map keyed by name
            for key, entry in value.items():
                yield from _leaves(node["additionalProperties"], entry, f"{pointer}/{key}")
    elif kind == "array" and isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _leaves(node["items"], entry, f"{pointer}/{index}")
    elif kind in {"boolean", "number", "integer"} or "enum" in node or "const" in node:
        yield pointer, node


def _outside(bound: float, integer: bool, direction: int) -> float:
    return bound + direction if integer else math.nextafter(bound, direction * math.inf)


def _probes(node: dict) -> list[Any]:
    kind = node["type"]
    wrong = [True, "1", None, [], {}]
    if kind == "boolean":
        return [True, False, 0, 1, "true", None, [], {}]
    if kind == "string":
        allowed = list(node["enum"]) if "enum" in node else [node["const"]]
        return [*allowed, "bogus", "", 1, False, None, [], {}]
    integer = kind == "integer"
    probes: list[Any] = [*wrong, 0, 1, -1.0e9, 1.0e9]
    if integer:
        probes += [2.0, 2.5, 4.0, 3, 5, 17, 1.0e30]
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in node:
            bound = node[key]
            probes += [bound, _outside(bound, integer, 1), _outside(bound, integer, -1)]
            probes.append(float(bound))  # an integral float stands for the integer
    for allowed in node.get("enum", ()):
        probes += [allowed, allowed + 1, allowed - 1]
    return probes


# Parser rules between a leaf and others that the schema leaves out: whole microseconds and the
# window (step_s) and bandwidth >= Rs(1 + a). For these the schema may be looser than the parser,
# never stricter.
PARSER_STRICTER = {"/time/step_s", "/radio/channel_bandwidth_hz", "/adaptation/symbol_rate_baud"}


def _agreement_problems(base, prefix: str, directory: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    probed = 0
    reference = base()
    for pointer, node in _leaves(SCHEMA, reference):
        if not pointer.startswith(prefix):
            continue
        for value in _probes(node):
            document = _edit(base(), (pointer, value))
            schema_says, parser_says = schema_accepts(document), parser_accepts(document, directory)
            probed += 1
            looser_ok = pointer in PARSER_STRICTER and not parser_says
            if schema_says != parser_says and not (looser_ok and schema_says):
                problems.append(f"{pointer}={value!r}: schema {schema_says}, parser {parser_says}")
    return problems, probed


def test_schema_and_parser_agree_on_every_scalar_leaf_and_its_bounds(tmp_path: Path) -> None:
    problems, probed = _agreement_problems(_free_space_base, "", tmp_path)
    assert not problems, "\n".join(problems)
    assert probed > 500


def test_schema_and_parser_agree_on_the_hydrometeor_station_bounds(tmp_path: Path) -> None:
    # Tromso sits at 0 m, so a rain top of 0 m is legal (Madrid's 650 m is a parser-only rule).
    prefix = "/propagation/hydrometeors/stations/Tromso/"
    problems, probed = _agreement_problems(_spec_example, prefix, tmp_path)
    assert not problems, "\n".join(problems)
    assert probed > 40
    stations = SCHEMA["properties"]["propagation"]["properties"]["hydrometeors"]["anyOf"][0]
    state = stations["properties"]["stations"]["additionalProperties"]["properties"]
    assert state["rain_top_height_amsl_m"]["maximum"] == 20_000
    assert state["polarization_tilt_deg"]["maximum"] == 180


URL_ACCEPTED = [
    "http://example.org",
    "https://example.org/a?b=c#d",
    "https://example.org:8080/x",
    "https://example.org:99999/",  # the parser never reads the port
    "https://example.org?q",
    "https://example.org#fragment",
    "https://a/b",
    "https://127.0.0.1:8080/gp.php?GROUP=iridium-NEXT&FORMAT=CSV",
    "https://xn--mnchen-3ya.de/",
    "https://example.org/caf\u00e9",  # non-ASCII outside the host is fine for both
    "https://example.org/a[1]@b",  # brackets and @ after the host are ordinary characters
    "HTTP://example.org",  # urlsplit lowercases the scheme
    "HtTpS://example.org/Mixed",
]
URL_REJECTED = [
    "",
    "   ",
    "ftp://example.org",
    "example.org",
    "//example.org",
    "mailto:a@b.c",
    "file:///etc/passwd",
    "http:example.org",
    "http:/example.org",
    "https//example.org",
    "htp://example.org",
    "https ://example.org",
    " https://example.org",
    "http://",
    "https:///path",
    "https://user:pw@example.org/",
    "https://@example.org",
    "https://example.org@",
    "https://:@example.org",
    "https://exa mple.org/",
    "https://example.org/a b",
    "https://example.org/\t",
    "https://example.org/\n",
    "https://example.org\n",
    "https://example.org/\u00a0",
    "https://example.org/\u2003x",
    "https://example.org/\x1f",  # str.isspace is true for U+001C to U+001F and U+0085
    "https://example.org/\x85",
    "http://?q",
    "http://#fragment",
    "https://[abc/",
    "https://abc]/",
    "https://[abc]/",
    5,
    None,
]

STRING_CASES = [
    # (pointer, strings both must accept, values both must reject)
    ("/name", ["x", "two words"], ["", " ", "\t\n", " ", 5, None]),
    ("/description", ["", "free text", " "], [5, None, [], {}]),
    ("/space_segment/catalogs/0/path", ["a.csv", "../data/a.csv"], ["", "   ", 5, None]),
    ("/ground_segment/stations/0/name", [], ["", "  ", 5, None]),
    ("/ground_segment/stations/0/terminal", [], ["", "  ", 5, None]),
    ("/network/pairs/0/source", [], ["", "  ", 5, None]),
    ("/network/pairs/0/target", [], ["", "  ", 5, None]),
    (
        "/space_segment/catalogs/0/sha256",
        ["a" * 64, "0123456789abcdef" * 4],
        ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", " " + "a" * 63, 5, None],
    ),
    ("/space_segment/catalogs/0/source_url", URL_ACCEPTED, URL_REJECTED),
    ("/space_segment/catalogs/0/terms_url", URL_ACCEPTED, URL_REJECTED),
    (
        # The parser also reads other ISO 8601 forms ("2026-09-10 22:00:25Z"); the schema accepts
        # the RFC 3339 form only, a subset, so only that form is compared here.
        "/space_segment/catalogs/0/retrieved_at_utc",
        ["2026-09-10T22:00:25Z", "2026-09-10T22:00:25.5Z", "2026-02-28T00:00:00.000001Z"],
        [
            "",
            "2026-09-10T22:00:25",
            "2026-09-10T22:00:25+00:00",
            "2026-09-10T22:00:25z",
            "2026-09-10T22:00:25Z\n",
            "2026-13-10T22:00:25Z",
            "2026-00-10T22:00:25Z",
            "2026-09-32T22:00:25Z",
            "2026-09-00T22:00:25Z",
            "2026-09-10T24:00:00Z",
            "2026-09-10T22:60:00Z",
            "2026-09-10T22:00:60Z",
            "10 September 2026Z",
            "not a date",
            5,
            None,
        ],
    ),
]


@pytest.mark.parametrize(
    ("pointer", "accepted", "rejected"), STRING_CASES, ids=lambda v: str(v)[:40]
)
def test_schema_and_parser_agree_on_strings(pointer, accepted, rejected, tmp_path: Path) -> None:
    for value in accepted:
        document = _edit(_spec_example(), (pointer, value))
        assert schema_accepts(document), (pointer, value)
        assert parser_accepts(document, tmp_path), (pointer, value)
    for value in rejected:
        document = _edit(_spec_example(), (pointer, value))
        assert not schema_accepts(document), (pointer, value)
        assert not parser_accepts(document, tmp_path), (pointer, value)


URL_SCHEMA = SCHEMA["properties"]["space_segment"]["properties"]["catalogs"]["items"]["properties"][
    "source_url"
]


URL_VALIDATOR = jsonschema.Draft202012Validator(URL_SCHEMA)


def _url_verdicts(value: str) -> tuple[bool, bool]:
    """(schema, parser) verdicts for one catalog URL, without building a whole document."""
    try:
        _url(value, "/url")
        parser = True
    except ValueError:
        parser = False
    return URL_VALIDATOR.is_valid(value), parser


def test_both_catalog_urls_share_one_fragment() -> None:
    catalog = SCHEMA["properties"]["space_segment"]["properties"]["catalogs"]["items"]
    fragment = catalog["properties"]["terms_url"]
    assert {key: value for key, value in fragment.items() if key != "description"} == {
        key: value for key, value in URL_SCHEMA.items() if key != "description"
    }
    assert URL_SCHEMA["format"] == "uri"


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1]/x",  # an IPv6 literal
        "https://[2001:db8::1]:8080/gp.php",
        "https://m\u00fcnchen.de/",  # a non-ASCII host name
        "https://example.org\u00e9/",
        "https://ex\x01ample.org/",  # a control character in the host
        "\x01https://example.org/",  # urlsplit drops leading control characters
        "\x00https://example.org/",
    ],
)
def test_the_schema_is_stricter_than_the_parser_only_on_the_documented_urls(url: str) -> None:
    schema_says, parser_says = _url_verdicts(url)
    assert (schema_says, parser_says) == (False, True)


def _random_urls(alphabet: list[str], count: int, seed: int):
    rng = random.Random(seed)
    prefixes = ["", "https://", "HtTp://", "http:/", "https:", "https://a", "https://a/"]
    for _ in range(count):
        body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        yield rng.choice(prefixes) + body


def test_the_url_pattern_equals_the_parser_on_plain_urls() -> None:
    plain = list("hHtTpPsS:/?#@. %\\-_~0a9Z") + ["\t", "\n", "\r", " ", "\u00a0", "\u2003", "\x1f"]
    for url in _random_urls(plain, 20_000, seed=1):
        schema_says, parser_says = _url_verdicts(url)
        assert schema_says == parser_says, repr(url)


def test_the_url_pattern_never_accepts_what_the_parser_rejects() -> None:
    rough = [
        *"hHtTpPsS:/?#@[]. \t\n\r%\\-_~0a9\x00\x01\x7f\x85\ufeff",
        "\u00e9",  # a non-ASCII letter
        "\u2100",  # NFKC turns it into "a/c": urlsplit refuses it in a host
        "\uff03",  # a full-width number sign
        "\uff1a",  # a full-width colon
        "\u2003",
        "\U0001f600",
    ]
    for url in _random_urls(rough, 30_000, seed=2):
        schema_says, parser_says = _url_verdicts(url)
        assert parser_says or not schema_says, repr(url)


def test_the_url_whitespace_class_is_exactly_str_isspace() -> None:
    pattern = re.compile(URL_SCHEMA["pattern"])
    for code in range(0x110000):
        character = chr(code)
        if character in "/?#@[]":
            continue
        accepted = pattern.search(f"https://e/{character}") is not None
        assert accepted == (not character.isspace()), hex(code)


def test_terminal_names_must_be_non_blank_in_both() -> None:
    terminal = _spec_example()["terminals"]["default"]
    for blank in ("", "   "):
        document = _edit(_spec_example(), ("/terminals", {"default": terminal, blank: terminal}))
        assert not schema_accepts(document)
    document = _edit(_spec_example(), ("/terminals", {"default": terminal, "mobile": terminal}))
    assert schema_accepts(document)


def test_a_json_text_can_say_what_the_schema_cannot() -> None:
    # Duplicate keys and non-finite numbers exist only in the text: json.loads keeps the last key
    # and turns 1e999 into infinity, which a schema over parsed instances cannot tell from 1e300.
    document = json.loads(
        json.dumps(_spec_example()).replace('"eirp_dbw": 15.0', '"eirp_dbw": 1e999')
    )
    assert document["radio"]["eirp_dbw"] == math.inf
    assert schema_accepts(document)


# --- cross-field rules the schema expresses ---------------------------------------------------

ITU = "/propagation/model"
MASK0 = "/ground_segment/stations/0/minimum_elevation_deg"
MASK1 = "/ground_segment/stations/1/minimum_elevation_deg"
DEFAULT_MASK = "/ground_segment/default_minimum_elevation_deg"
FREQUENCY = "/radio/carrier_frequency_hz"
HYDROMETEORS = "/propagation/hydrometeors"
AMSL0 = "/ground_segment/stations/0/height_amsl_m"
MODELS = "/network/route_models"
FIXED = "/network/fixed_capacity_bps"
NORAD = "/space_segment/select_norad_ids"

RULE_CASES = [
    # (id, base, edits, both accept?)
    ("spec_example", "spec", [], True),
    # mask >= 5 with itu_reference, own or inherited
    ("inherited_mask_3", "spec", [(DEFAULT_MASK, 3.0)], False),
    (
        "default_mask_3_but_own_masks_5",
        "spec",
        [(DEFAULT_MASK, 3.0), (MASK0, 5.0), (MASK1, 5.0)],
        True,
    ),
    ("default_mask_3_one_inherits", "spec", [(DEFAULT_MASK, 3.0), (MASK0, 5.0)], False),
    ("default_mask_3_own_masks_omitted", "spec", [(DEFAULT_MASK, 3.0), (MASK0, MISSING)], False),
    ("own_mask_below_5", "spec", [(MASK0, 4.999)], False),
    ("own_mask_5", "spec", [(MASK0, 5.0)], True),
    ("own_mask_null_inherits_a_valid_default", "spec", [(MASK0, None)], True),
    ("own_mask_89", "spec", [(MASK1, 89.999)], True),
    ("own_mask_90", "spec", [(MASK1, 90.0)], False),
    ("free_space_low_masks", "free", [(DEFAULT_MASK, 0.0), (MASK0, 0.0)], True),
    # height_amsl_m required with itu_reference
    ("amsl_missing_itu", "spec", [(AMSL0, MISSING)], False),
    ("amsl_null_itu", "spec", [(AMSL0, None)], False),
    ("amsl_missing_free_space", "free", [(AMSL0, MISSING)], True),
    ("amsl_null_free_space", "free", [(AMSL0, None)], True),
    ("amsl_too_high_free_space", "free", [(AMSL0, 10_000.5)], False),
    ("itu_on_the_free_base", "free", [(ITU, "itu_reference")], True),
    # hydrometeors only with itu_reference
    (
        "hydrometeors_with_free_space",
        "free",
        [(HYDROMETEORS, _spec_example()["propagation"]["hydrometeors"])],
        False,
    ),
    ("null_hydrometeors_with_free_space", "free", [(HYDROMETEORS, None)], True),
    ("null_hydrometeors_with_itu", "spec", [(HYDROMETEORS, None)], True),
    ("omitted_hydrometeors_with_itu", "spec", [(HYDROMETEORS, MISSING)], True),
    # carrier frequency domains
    ("itu_1_GHz", "spec", [(FREQUENCY, 1e9)], True),
    ("itu_below_1_GHz", "spec", [(FREQUENCY, math.nextafter(1e9, 0.0))], False),
    ("hydrometeors_200_GHz", "spec", [(FREQUENCY, 200e9)], True),
    ("hydrometeors_above_200_GHz", "spec", [(FREQUENCY, math.nextafter(200e9, math.inf))], False),
    ("itu_without_hydrometeors_1_THz", "spec", [(HYDROMETEORS, None), (FREQUENCY, 1e12)], True),
    (
        "itu_without_hydrometeors_above_1_THz",
        "spec",
        [(HYDROMETEORS, None), (FREQUENCY, math.nextafter(1e12, math.inf))],
        False,
    ),
    ("free_space_100_MHz", "free", [(FREQUENCY, 1e8)], True),
    ("free_space_5_THz", "free", [(FREQUENCY, 5e12)], True),
    # fixed_capacity_bps exactly when fixed_capacity is listed
    ("fixed_listed_with_bps", "spec", [], True),
    ("fixed_listed_bps_null", "spec", [(FIXED, None)], False),
    ("fixed_listed_bps_missing", "spec", [(FIXED, MISSING)], False),
    ("fixed_listed_bps_zero", "spec", [(FIXED, 0.0)], False),
    ("not_listed_bps_null", "spec", [(MODELS, ["minimum_delay"]), (FIXED, None)], True),
    ("not_listed_bps_missing", "spec", [(MODELS, ["maximum_rate"]), (FIXED, MISSING)], True),
    ("not_listed_bps_given", "spec", [(MODELS, ["minimum_delay"])], False),
    ("only_fixed_listed", "spec", [(MODELS, ["fixed_capacity"])], True),
    ("models_empty", "spec", [(MODELS, [])], False),
    ("models_repeat", "spec", [(MODELS, ["minimum_delay", "minimum_delay"]), (FIXED, None)], False),
    ("models_unknown", "spec", [(MODELS, ["shortest"]), (FIXED, None)], False),
    ("network_null", "spec", [("/network", None)], True),
    ("network_omitted", "spec", [("/network", MISSING)], True),
    # NORAD selections
    ("norad_1", "free", [(NORAD, [1])], True),
    ("norad_max", "free", [(NORAD, [2_147_483_647])], True),
    ("norad_integral_float", "free", [(NORAD, [4.0])], True),
    ("norad_zero", "free", [(NORAD, [0])], False),
    ("norad_above_max", "free", [(NORAD, [2_147_483_648])], False),
    ("norad_fraction", "free", [(NORAD, [4.5])], False),
    ("norad_boolean", "free", [(NORAD, [True])], False),
    ("norad_empty", "free", [(NORAD, [])], False),
    ("norad_repeat", "free", [(NORAD, [7, 7])], False),
    ("norad_repeat_as_float", "free", [(NORAD, [7, 7.0])], False),
    ("norad_two", "free", [(NORAD, [7, 8])], True),
    # collections
    ("formats_one", "spec", [("/outputs/formats", ["parquet"])], True),
    ("formats_all", "spec", [("/outputs/formats", ["parquet", "csv", "olc"])], True),
    ("formats_empty", "spec", [("/outputs/formats", [])], False),
    ("formats_repeat", "spec", [("/outputs/formats", ["csv", "csv"])], False),
    ("formats_unknown", "spec", [("/outputs/formats", ["json"])], False),
    (
        "pairs_reversed_too",
        "spec",
        [
            (
                "/network/pairs",
                [
                    {"source": "Madrid", "target": "Tromso"},
                    {"source": "Tromso", "target": "Madrid"},
                ],
            )
        ],
        True,
    ),
    (
        "pairs_repeat",
        "spec",
        [
            (
                "/network/pairs",
                [
                    {"source": "Madrid", "target": "Tromso"},
                    {"source": "Madrid", "target": "Tromso"},
                ],
            )
        ],
        False,
    ),
    ("pairs_empty", "spec", [("/network/pairs", [])], False),
    ("catalogs_empty", "spec", [("/space_segment/catalogs", [])], False),
    ("no_stations", "spec", [("/ground_segment/stations", [])], False),
    ("hydrometeor_stations_empty", "spec", [(HYDROMETEORS + "/stations", {})], False),
    ("hydrometeor_model_other", "spec", [(HYDROMETEORS + "/model", "weather_api")], False),
    ("hydrometeor_extra_key", "spec", [(HYDROMETEORS + "/stations/Madrid/snow", 1.0)], False),
    ("unknown_root_key", "spec", [("/extra", 1)], False),
    ("schema_v1", "spec", [("/schema", "openleo.scenario/1")], False),
]


@pytest.mark.parametrize(
    ("base", "edits", "expected"),
    [case[1:] for case in RULE_CASES],
    ids=[case[0] for case in RULE_CASES],
)
def test_schema_and_parser_agree_on_cross_field_rules(base, edits, expected, tmp_path) -> None:
    document = _edit(_spec_example() if base == "spec" else _free_space_base(), *edits)
    assert schema_accepts(document) is expected
    assert parser_accepts(document, tmp_path) is expected


# --- the generator itself ---------------------------------------------------------------------


def test_the_generator_rejects_what_it_cannot_describe() -> None:
    @dataclasses.dataclass
    class WithMapping:
        values: dict[str, int] = dataclasses.field(metadata={"description": "Not supported."})

    @dataclasses.dataclass
    class WithBareString:
        text: str = dataclasses.field(metadata={"description": "No format declared."})

    @dataclasses.dataclass
    class WithNumberedMap:
        pairs: tuple[tuple[int, float], ...] = dataclasses.field(
            metadata={"description": "Keys of a name map are strings."}
        )

    with pytest.raises(TypeError, match="WithNumberedMap.pairs"):
        schema_module._object_schema(WithNumberedMap)
    with pytest.raises(TypeError, match="WithMapping.values"):
        schema_module._object_schema(WithMapping)
    with pytest.raises(TypeError, match="WithBareString.text"):
        schema_module._object_schema(WithBareString)
