# Published orbit reference verification

`openleo.orbit_verification.verify_orbit_reference(config_path)` checks five
published states for Vallado case 06251 (DELTA 1 DEB), a near-Earth orbit with
moderate drag. The offline fixture is `examples/validation/vallado_06251.json`.
No network access or new dependency is required to run the check.

```python
from openleo.orbit_verification import verify_orbit_reference

result = verify_orbit_reference("examples/validation/vallado_06251.json")
assert result["passed"]
```

The TLE is parsed and exported to a new OMM CSV record using the installed SGP4
package. OpenLEO's checksum-verifying `load_catalog` loads that record. Skyfield
propagates the returned satellite and converts its state to TEME with
`frame_xyz_and_velocity(TEME)`. The published ephemeris provides the expected
values as fixed literals; the production implementation never computes them.
Position and velocity residuals are Euclidean vector norms, with tolerances
`1e-6 km` and `1e-9 km/s`. Both thresholds must pass at every epoch.

The result uses schema `1`, kind `openleo.orbit-verification`, and reports the
fixture and converted OMM SHA-256 hashes, recorded source hashes, units, epoch
convention, states and residuals, thresholds, package versions and limitations.
Invalid input raises `ValueError`; a numerical mismatch returns `passed: false`.

## Frame and epoch convention

The published reference frame is TEME, with position in kilometres, velocity in
kilometres per second, and elapsed seconds from the TLE epoch. Comparing these
values directly to Skyfield's usual GCRS output would be incorrect.

The TLE epoch `06176.82412014` corresponds mathematically to
`2006-06-25T19:46:43.980096000Z`. The ephemeris prints
`2006-06-25T19:46:43.980095999Z`, a difference of one nanosecond due to printed
fractional precision. The OMM exporter and existing catalog loader use Python
datetime microseconds. SGP4's OMM initialization also stores its epoch as a
floating point day count; the installed SGP4 2.27 adapter epoch differs from
the TLE's split Julian date by `1.269064853204327e-7 s` (about 127 ns).

The check therefore uses the published elapsed seconds relative to the loaded
SGP4 epoch and retains split Julian dates while propagating. It explicitly
reports the converted OMM epoch and its shift from the original TLE epoch.
It does **not** verify absolute UTC accuracy or require the loader to preserve
nanosecond epochs. No state residual correction or tolerance relaxation is used.

The position literals have eight decimal places and the velocity literals nine.
Rounding a three-component velocity vector can contribute up to
`sqrt(3) * 0.5e-9 km/s` to its vector residual. On Skyfield 1.55 / SGP4 2.27,
the maximum residuals were `7.28e-9 km` and `6.60e-10 km/s`, within the declared
tolerances.

## Provenance and reuse

Vallado, Crawford, Hujsak and Kelso, *Revisiting Spacetrack Report #3*,
AIAA 2006-6753, is published on
[CelesTrak](https://celestrak.org/publications/AIAA/2006-6753/).
The [authors' FAQ](https://celestrak.org/publications/AIAA/2006-6753/faq.php)
describes the TEME output and permits personal and commercial use with source
citation and a link to the publication. The fixture contains only two TLE lines
and five numerical states; no archive, source code or complete ephemeris is
vendored.

The [published archive](https://celestrak.org/publications/AIAA/2006-6753/AIAA-2006-6753.zip)
and extracted files were downloaded and verified on 2026-09-28:

| File | SHA-256 |
| --- | --- |
| AIAA-2006-6753.zip | `3642043b706c76be87cf012db3f22e04da6b80498d00f515e51879e0ffadc115` |
| sgp4/cpp/testsgp4/SGP4-VER.TLE | `d246d1d9d768ace445a38a965713fa9ba52d80fd8a41a0502ff83d7acffe2881` |
| sgp4/cpp/testsgp4/TestSGP4/06251.e | `d806df44648b1009dfd0ba7d8ff5348638c35255df94945299ce7b29cb1718aa` |

These recorded source hashes are provenance from fixture construction, not a
claim that every later user-supplied fixture has been checked against a remote
archive. The runtime hash identifies the exact local fixture used.

## Limits

This is numerical agreement with published output, not independent physical
truth: both implementations share SGP4 theory and Vallado implementation lineage.
Only one near-Earth moderate-drag case and its TEME state are covered. This does
not validate deep-space propagation, GCRS/ITRS transformations, satellite
observations, Doppler, RF measurements, or end-to-end link predictions.
