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

## Vallado published numerical orbit reference

- Fixture: `examples/validation/vallado_06251.json`; two TLE lines and five
  numerical TEME states for case 06251 (DELTA 1 DEB).
- Authors: David A. Vallado, Paul Crawford, Richard Hujsak, and T. S. Kelso,
  *Revisiting Spacetrack Report #3*, AIAA 2006-6753.
- Primary publication: https://celestrak.org/publications/AIAA/2006-6753/
- Source archive: https://celestrak.org/publications/AIAA/2006-6753/AIAA-2006-6753.zip
- Reuse statement: https://celestrak.org/publications/AIAA/2006-6753/faq.php
- Source verified: 2026-09-28T14:16:19Z.
- Archive SHA-256: `3642043b706c76be87cf012db3f22e04da6b80498d00f515e51879e0ffadc115`.
- TLE source member: `sgp4/cpp/testsgp4/SGP4-VER.TLE`;
  SHA-256 `d246d1d9d768ace445a38a965713fa9ba52d80fd8a41a0502ff83d7acffe2881`.
- Ephemeris member: `sgp4/cpp/testsgp4/TestSGP4/06251.e`;
  SHA-256 `d806df44648b1009dfd0ba7d8ff5348638c35255df94945299ce7b29cb1718aa`.

The authors' FAQ permits personal and commercial use with source citation and
a link to the publication. The fixture retains small numerical excerpts for
verification; the archive, complete ephemeris, and upstream source code are
not vendored. The local fixture and converted OMM bytes receive their own
runtime hashes. Source-member hashes record fixture-construction provenance,
not a remote validation of every later user-supplied fixture.

These are published numerical outputs, not satellite observations. The comparison
shares SGP4 theory and Vallado lineage; it does not establish physical orbit
accuracy or calibrated RF performance. Frame, epoch precision, tolerances, and
the exact verification procedure are in
[Orbit verification](docs/ORBIT_VERIFICATION.md).

## Natural Earth coastline

- Packaged map: `src/openleo/_web/coastline.geojson`.
- Source: https://raw.githubusercontent.com/nvkelso/natural-earth-vector/ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_110m_coastline.geojson
- Upstream commit: `ca96624a56bd078437bca8184e78163e5039ad19`.
- Retrieved: 2026-09-10 UTC.
- SHA-256: `851f581ff5ffb844deed8ae1a9ce22e3c4bb3d74fa342cadb5d8e39b41ae7c3c`.
- Terms: https://www.naturalearthdata.com/about/terms-of-use/ (public domain).

This generalized 1:110 million cartography supplies visual geographic context only.
It is not a terrain, elevation, horizon-obstruction or station-location dataset.
