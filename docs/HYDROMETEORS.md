# Rain And Cloud Attenuation

OpenLEO provides two independent scalar kernels for explicitly supplied rain rate
and integrated cloud liquid water. They implement Recommendation ITU-R P.838-3
and the instantaneous method in Recommendation ITU-R P.840-9. They do not infer
weather from humidity or modify a constellation simulation's link budget.

## Batch Workflow

Run the committed reference inputs from the repository root:

```bash
uv run --no-dev openleo hydrometeors \
  examples/atmosphere/hydrometeors_reference.json --output runs/hydrometeors
```

Open `runs/hydrometeors/index.html` locally. The batch keeps the two physical
quantities separate and writes:

| File | Contents |
| --- | --- |
| `rain.csv` | Declared rain inputs, `k`, `alpha`, and specific attenuation in dB/km |
| `cloud.csv` | Declared cloud inputs, mass absorption, and slant attenuation in dB |
| `hydrometeors.json` | Inputs and calculated results, configuration SHA-256, software version, model sources, and limitations |
| `index.html` | Self-contained static report |
| `manifest.json` | SHA-256 fingerprints of the other four artifacts |

Outputs are published atomically to a new or empty directory. Existing nonempty
directories, files, and output symlinks are refused. The Python entry point is
`openleo.hydrometeor_benchmark.run_hydrometeors(config_path, output_dir)`.

## Input Contract

Use the [reference configuration](../examples/atmosphere/hydrometeors_reference.json)
as a template. The JSON object must contain exactly `schema_version` (the string
`"1"`), a nonblank string `name`, and the arrays `rain_cases` and `cloud_cases`.
Each rain case contains exactly `id`, `frequency_hz`, `rain_rate_mm_h`,
`elevation_deg`, and `polarization_tilt_deg`. Each cloud case contains exactly
`id`, `frequency_hz`, `liquid_water_kg_m2`, and `elevation_deg`.

Each array accepts at most 32 cases; one may be empty, but at least one case is
required overall. IDs must be unique across both arrays and match
`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. Configurations are limited to 1,000,000 bytes.
Duplicate, unknown, or missing JSON keys are rejected, including any supplied
expected-result fields. All numerical inputs must satisfy the model domains below.

## Public API And Units

```python
from openleo.hydrometeors import (
    cloud_slant_attenuation,
    rain_specific_attenuation,
)
```

```python
rain_specific_attenuation(
    frequency_hz,
    rain_rate_mm_h,
    elevation_deg,
    polarization_tilt_deg,
)
```

This returns a frozen `RainAttenuation` with fields:

- `k`: the P.838-3 power-law coefficient;
- `alpha`: the dimensionless P.838-3 exponent; and
- `specific_attenuation_db_per_km`: rain specific attenuation in dB/km.

The numerical convention for `k` is tied to a rain rate `R` expressed in mm/h:

```text
specific_attenuation_db_per_km = k * R**alpha
```

Consequently, `k` is not described as dimensionless. Equivalently its units are
the units required by that relationship, `(dB/km)/(mm/h)^alpha`.

```python
cloud_slant_attenuation(
    frequency_hz,
    liquid_water_kg_m2,
    elevation_deg,
)
```

This returns a frozen `CloudAttenuation` with fields:

- `mass_absorption_db_per_kg_m2`: cloud liquid mass absorption in dB/(kg/m²),
  equivalently dB/mm; and
- `attenuation_db`: instantaneous cloud slant attenuation in dB.

The cloud input is vertically integrated liquid water `L` in kg/m². P.840-9
uses the equivalent millimetres of liquid water. The slant result is
`K_L * L / sin(elevation)`.

## Implemented Equations

The rain kernel evaluates the four P.838-3 Gaussian coefficient fits in
equations (2) and (3), then combines horizontal and vertical coefficients with
elevation and polarization tilt using equations (4) and (5). Frequency is
converted from public hertz to the equations' gigahertz exactly once.

The cloud kernel evaluates the double-Debye water-permittivity model in P.840-9
equations (2) through (10) at the fixed liquid-water temperature **273.75 K**.
It then applies the frequency-dependent correction in **equation (12)** to
obtain `K_L`; it is not the older uncorrected `K_l` result. Finally, equation
(11) supplies instantaneous slant attenuation.

## Domains And Validation

| Kernel | Frequency | Other inputs |
| --- | --- | --- |
| Rain | 1 GHz to 1,000 GHz inclusive | `R >= 0`, elevation 0° to 90°, tilt 0° to 180° |
| Cloud | 1 GHz to 200 GHz inclusive | `L >= 0`, elevation 5° to 90° |

All inputs must be real, finite scalars and may not be booleans. Invalid domains,
numeric overflow, or non-finite calculated attenuation raise `ValueError`. Zero
rain rate or zero integrated liquid water returns exactly zero loss while still
returning the frequency-dependent coefficient.

No configurable cloud temperature is exposed: 273.75 K is part of the P.840-9
instantaneous mass-absorption prescription used here, not a claim that all real
clouds have that temperature.

## Official Sources

The module exposes immutable `RAIN_SOURCE` and `CLOUD_SOURCE` mappings with the
fields `recommendation`, `url`, and `sha256` so result writers can reuse one
provenance record.

- [Recommendation ITU-R P.838-3 English PDF](https://www.itu.int/dms_pubrec/itu-r/rec/p/r-rec-p.838-3-200503-i%21%21pdf-e.pdf),
  SHA-256 `3ab7482993e51fc63c5127a72e9e8930614e73652ac614882760817e7c1469cb`.
- [Recommendation ITU-R P.840-9 English PDF](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.840-9-202308-I%21%21PDF-E.pdf),
  including the 2026 editorial amendments, SHA-256
  `cd554242549a4f3c5854133dce6f7f93dd86e04cfa8a20b5c1ff5fe9357f37b6`.

Both digests identify the exact PDF bytes used to implement and verify the
equations.

## Workbook Validation

Literal test oracles come from the official ITU validation workbook
`CG-3M3J-13-ValEx-Rev8.3.0.xlsx`, SHA-256:

```text
e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546
```

The workbook is not redistributed. Rain inputs come from worksheet
`P.838-3 Sp.Att` columns G, H, F, and I; expected `k`, `alpha`, and specific
attenuation come from columns N, O, and P:

| Case | Input cells | Oracle cells |
| --- | --- | --- |
| `p838-row-20` | G20, H20, F20, I20 | N20, O20, P20 |
| `p838-row-32` | G32, H32, F32, I32 | N32, O32, P32 |
| `p838-row-60` | G60, H60, F60, I60 | N60, O60, P60 |

Cloud inputs come from worksheet `P.840-9 A_Clouds` columns F, L, and G;
expected corrected mass absorption and slant attenuation come from columns K
and M:

| Case | Input cells | Oracle cells |
| --- | --- | --- |
| `p840-row-21` | F21, L21, G21 | K21, M21 |
| `p840-row-22` | F22, L22, G22 | K22, M22 |
| `p840-row-24` | F24, L24, G24 | K24, M24 |

The small public fixture
[`examples/atmosphere/hydrometeors_reference.json`](../examples/atmosphere/hydrometeors_reference.json)
contains only these replayable function inputs and case identifiers. Expected
values remain literal test oracles, so the input configuration cannot silently
become its own authority.

## Claim Boundary

These functions calculate specific rain attenuation for one supplied rain rate
and instantaneous cloud slant attenuation for one supplied integrated liquid
water value. They do not provide:

- rain-rate or cloud-water maps, interpolation, or exceedance statistics;
- a humidity-to-rain or humidity-to-cloud conversion;
- live weather, climatology, forecasts, or a probability model;
- rain path length, rain height, or total rain slant attenuation;
- cloud temperature selection or validity outside the stated frequency range;
- combined gas, rain, cloud, fog, or scintillation loss; or
- automatic changes to trace `C/N0`, SNR, capacity, routing, or availability.
