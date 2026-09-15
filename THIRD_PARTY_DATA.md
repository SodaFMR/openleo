# Third-Party Data

## CelesTrak ISS GP record

- Source query URL: https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=CSV
- Retrieved at: 2026-08-30T22:01:11Z
- Usage policy URL: https://celestrak.org/usage-policy.php
- SHA-256: `ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9`
- Purpose: frozen ISS pass geometry and free-space link-budget regression.

The RF values in `examples/scenarios/iss_cartagena.json` are synthetic OpenLEO
scenario assumptions. They are not CelesTrak data and are not measurements of ISS
hardware, Cartagena station hardware, or achieved throughput.

The repository MIT license covers original OpenLEO code and documentation only. It
does not replace CelesTrak source terms or usage policy for this archived GP record.

## Iridium NEXT constellation geometry

- Archived response: `examples/data/iridium_next_2026-09-10.csv`, 80 GP records.
- Source: https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV
- Retrieved: 2026-09-10T22:00:25Z.
- Usage policy: https://celestrak.org/usage-policy.php
- SHA-256: `e9dac20f80bb4d0ae0090996619827aab1abf16d448ed8b0edf47b965abf6273`.
- Original response bytes, including CRLF line endings, are preserved.

The public fitted elements support a repeatable multi-satellite geometry experiment.
The four example ground locations, Ku-band terminals, DVB-S2 adaptation settings and
reciprocal inter-satellite links are declared OpenLEO assumptions. They do not describe
Iridium's payload, RF network, gateways, operational topology or measured performance.
Example city coordinates and heights are approximate scenario choices, not surveys of
real ground stations. The frozen two-hour window is retrospective.

`examples/constellations/iridium_global_reference.json` reuses these exact archived
orbits. Its independent AMSL heights (Madrid 650 m, Tromso and Singapore 0 m,
Quito 2850 m) are explicit scenario assumptions, not surveyed elevations or a geoid
conversion. Its temperature, pressure and humidity are computed from the idealized
ITU-R P.835-7 global reference profile, not collected weather observations.
Source versions, PDF hashes, official numerical reference cases and model limits
are documented in [Reference propagation](docs/REFERENCE_PROPAGATION.md).

## Natural Earth coastline

- Packaged map: `src/openleo/_web/coastline.geojson`.
- Source: https://raw.githubusercontent.com/nvkelso/natural-earth-vector/ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_110m_coastline.geojson
- Upstream commit: `ca96624a56bd078437bca8184e78163e5039ad19`.
- Retrieved: 2026-09-10 UTC.
- SHA-256: `851f581ff5ffb844deed8ae1a9ce22e3c4bb3d74fa342cadb5d8e39b41ae7c3c`.
- Terms: https://www.naturalearthdata.com/about/terms-of-use/ (public domain).

This generalized 1:110 million cartography supplies visual geographic context only.
It is not a terrain, elevation, horizon-obstruction or station-location dataset.
