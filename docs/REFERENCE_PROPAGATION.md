# Reference atmospheric propagation

OpenLEO's optional `itu_reference` model adds gaseous attenuation and a refracted
optical-path delay to constellation ground links. It combines the global reference
atmosphere in ITU-R P.835-7, radio refractivity in P.453-14, and the line-by-line
spectrum and layered slant-path method in P.676-13. The result is an idealized
numerical experiment under declared RF assumptions, not a local weather estimate
or validation of an operational satellite network.

## Configuration and supported domain

The complete example is
[`iridium_global_reference.json`](../examples/constellations/iridium_global_reference.json).
Its top-level propagation configuration is:

```json
{
  "propagation": {
    "model": "itu_reference",
    "station_heights_amsl_m": {
      "Madrid": 650.0,
      "Tromso": 0.0,
      "Singapore": 0.0,
      "Quito": 2850.0
    },
    "refinement": 1
  }
}
```

Every configured station must appear exactly once in `station_heights_amsl_m`.
These values are explicit geometric heights above mean sea level (AMSL), in
metres. Station `height_m` remains the WGS84 ellipsoid height used for orbital
geometry. No geoid model or automatic conversion connects the two fields; equal
example values are declared assumptions, not a measured height conversion.

| Input or calculation | Supported domain |
| --- | --- |
| Carrier frequency | 1–1,000 GHz inclusive; JSON and Python inputs use Hz |
| Constellation station height | 0–10,000 m AMSL inclusive |
| Constellation geometric elevation mask | 5–90 degrees inclusive |
| Profile evaluator | Geometric height 0–100 km AMSL inclusive |
| Direct slant-path column | `0 <= lower_height_km < upper_height_km <= 100` |
| Direct apparent elevation | 0–90 degrees inclusive |
| Ground endpoint solve | Geometric elevation 5–90 degrees; positive chord range; endpoint above 100 km |
| Layer refinement | Integer 1, 2, or 4; a study starts at 1 or 2 |

Inputs must be finite numbers, and booleans are rejected as numeric inputs.
Invalid ascending rays, including an inverse-sine argument above one, raise an
error instead of being clipped into a physical result. Height intervals too small
to resolve numerically are also rejected. Omitting `propagation` retains the
free-space calculation and its schema-1 artifacts; enabling it produces schema-2
artifacts with the additional ground-link fields described below.

## Atmospheric state and absorption

The profile follows
[P.835-7 Annex 1, equations (1)–(8)](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.835-7-202408-I!!PDF-E.pdf).
For geometric height $Z<86$ km it converts to geopotential height

$$
H=\frac{6356.766 Z}{6356.766+Z}
$$

before applying the published piecewise temperature and total-pressure equations.
At 86–100 km it uses the recommendation's geometric-height temperature and
pressure equations. The implementation retains the small discontinuities arising
from the recommendation's rounded pressure constants; it does not smooth the
profile. At the otherwise unspecified $H=11$ km boundary it selects the upper
layer; other published pressure boundaries use their stated lower-layer endpoint.

For temperature $T$ in K, total pressure $P$ in hPa, and density $\rho$ in g/m³,
the humidity profile is

$$
\rho=\max\left(7.5\exp(-Z/2),\frac{2\times10^{-6}P\,216.7}{T}\right),\qquad
e=\frac{\rho T}{216.7},\qquad p=P-e.
$$

The minimum vapour-pressure mixing ratio $e/P=2\times10^{-6}$ is part of P.835.
The spectrum receives **dry pressure** $p$, not total pressure $P$. At sea level,
$T=288.15$ K, $P=1013.25$ hPa, $\rho=7.5$ g/m³,
$e=9.97288878634056$ hPa, and $p=1003.2771112136594$ hPa.
This differs from the separate [homogeneous gases benchmark](GASES.md), which
declares 1013.25 hPa as its dry-pressure input.

The radio refractive index follows
[P.453-14 Annex 1, equations (1)–(2)](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.453-14-201908-I!!PDF-E.pdf):

$$
n=1+10^{-6}\left(77.6\frac{p}{T}+72\frac{e}{T}
+3.75\times10^5\frac{e}{T^2}\right).
$$

At each layer midpoint, the existing P.676-13 Annex 1 line-by-line calculation
evaluates separate dry-air and water-vapour specific attenuation in dB/km.
The profile, refractive indices, and specific attenuations are prepared once per
station column and reused for its links within that simulation run.

## Layered slant path

The ray geometry uses P.676's mean Earth radius $R=6371$ km. Layers follow
[P.676-13 Annex 1, equation (16)](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.676-13-202208-I!!PDF-E.pdf):
the recommendation's exponential grid is normalized to the requested lower and
upper heights. The atmospheric column for a ground-to-space link ends exactly at
100 km. Refinement 2 or 4 splits each base interval evenly and reevaluates the
profile and spectrum at each new midpoint. This is distinct from the fixed
922-layer surface-to-space grid in equations (14)–(15), whose final layer extends
to approximately 100.45668 km. P.676 cautions that normalized intervals spanning
fewer than 50 base layers can have degraded accuracy.

For apparent elevation $E_a$, bottom radius $r_1$, and first midpoint index $n_1$,
the ray's Snell invariant is $K=n_1r_1\cos E_a$. In layer $i$,

$$
\beta_i=\arcsin\frac{K}{n_i r_i},\qquad
\alpha_i=\arcsin\frac{K}{n_i r_{i+1}}.
$$

The implementation evaluates the layer distance $a_i$ with a rationalized form
of equation (17) to avoid subtracting nearly equal Earth-scale lengths.
Dry-air and water-vapour attenuation are summed separately as
$A_o=\sum_i a_i\gamma_{o,i}$ and $A_w=\sum_i a_i\gamma_{w,i}$, with $a_i$ in km
and attenuation in dB. The reported total is $A_o+A_w$.

The direct slant API also reports ray central angle
$\sum_i(\beta_i-\alpha_i)$, bent geometric length $\sum_i a_i$, and refractive
excess length $\sum_i a_i(n_i-1)$. Bending follows equation (22):
$\sum_{i=1}^{N-1}(\beta_{i+1}-\alpha_i)$. Positive bending is toward Earth;
the sum excludes a terminal transition to vacuum. These angular outputs use
radians, while geometric and excess path lengths use metres.

## Geometric endpoints, apparent elevation, and delay

An orbit calculation supplies geometric elevation $E_g$ and geometric chord range
$s$. Substituting $E_g$ directly for $E_a$ would generally send the refracted ray
to a different endpoint. OpenLEO derives the endpoint radius and central angle on
the mean sphere:

$$
r_t=\sqrt{r_0^2+s^2+2r_0s\sin E_g},\qquad
\theta_t=\operatorname{atan2}(s\cos E_g,r_0+s\sin E_g),
$$

where $r_0=R+h_{\rm AMSL}$ and all lengths in these equations use km.
It solves $E_a\in[E_g,90^\circ]$ by bounded bisection so that the layered ray's
central angle plus its vacuum continuation above 100 km equals $\theta_t$.
The vacuum contribution is $\arcsin(K/r_{\rm top})-\arcsin(K/r_t)$.
An endpoint solver therefore requires a full column ending at 100 km; a partial
column ending at 8 km cannot be followed by an assumed vacuum segment.

If $L_v$ is the geometric vacuum continuation length, the atmospheric excess
delay relative to the supplied endpoint chord is

$$
\Delta\tau=\frac{\sum_i n_i a_i+L_v-s}{c},\qquad
c=299792458\ \mathrm{m/s},
$$

with lengths converted to metres before division. It includes both refractive
excess and the bent ray's extra geometric distance. It uses P.453's
**non-dispersive** index; it is not a frequency-dependent group-delay calculation
near molecular lines. The total ground-link delay is $s/c+\Delta\tau$.

The mean-sphere endpoint construction approximates the WGS84 topocentric geometry.
It does not replace SGP4 propagation, the geometric elevation visibility mask,
sampled contact boundaries, or geometric range-rate Doppler. Apparent elevation is
reported separately and does not extend a contact's visible interval.

## Link-budget effects and controlled study

The declared `system_noise_temperature_k` stays fixed. The model subtracts total
gaseous loss from the free-space `C/N0`, then recomputes SNR, the Shannon upper
bound, and the reference MODCOD selection from that corrected `C/N0`. Atmospheric
sky emission is not added to the receiver noise temperature.

| Ground-link field | Unit and meaning |
| --- | --- |
| `gaseous_dry_attenuation_db` | dB; integrated dry-air component |
| `gaseous_water_attenuation_db` | dB; integrated water-vapour component |
| `gaseous_attenuation_db` | dB; sum of the two gaseous components |
| `free_space_cn0_db_hz` | dB-Hz; original declared fixed-noise link budget |
| `cn0_db_hz` | dB-Hz; free-space value minus gaseous attenuation |
| `geometric_delay_s` | s; original chord range divided by $c$ |
| `atmospheric_excess_delay_s` | s; optical excess relative to that chord |
| `delay_s` | s; geometric plus atmospheric excess delay |
| `elevation_deg` / `apparent_elevation_deg` | degrees; geometric / solved apparent elevation |

Ground-edge capacities and delays feed the existing snapshot routing model.
Inter-satellite links retain their declared capacity and geometric delay. Rates and
integrated reference bits remain model outputs without scheduling, protocol
overhead, or measured-throughput interpretation.

Run the three-case study with the same orbit, station geometry, time grid, RF,
adaptation, and routing assumptions:

```bash
uv run openleo propagation-study \
  examples/constellations/iridium_global_reference.json \
  --output runs/reference-propagation
```

The cases are `free_space`, `reference`, and `refined`. `reference` uses the
configuration's refinement; `refined` doubles it, so studies must start at 1 or 2.
The output directory contains a complete workbench bundle per case,
`propagation-study.json`, and `comparison.csv`. The summary records per-case
configurations and fingerprints, statistics, and maximum differences in gaseous
loss, apparent elevation, and excess delay, plus MODCOD disagreements between
the two atmospheric grids. These differences describe numerical discretization;
they are not measurement errors, uncertainty intervals, or service probabilities.

The optional figure requires the `plot` extra:

```bash
uv run --extra plot openleo propagation-study \
  examples/constellations/iridium_global_reference.json \
  --output runs/reference-propagation \
  --figure runs/reference-propagation/overview.svg
```

## Numerical checks and source provenance

The complete bundled example covers 80 archived objects and 121 samples over two
hours at 12 GHz, with 954 above-mask ground-link samples. Its base-grid gaseous
loss ranges from 0.02736 to 0.33312 dB; atmospheric excess delay ranges from
5.568 to 44.506 ns. The controlled study produces:

| Station | Free-space best-link Gbit | Reference-atmosphere Gbit | Refined-grid Gbit |
| --- | ---: | ---: | ---: |
| Madrid | 1.82872716 | 1.79931258 | 1.79931258 |
| Tromso | 4.01093160 | 3.80402490 | 3.80402490 |
| Singapore | 1.65123504 | 1.62182046 | 1.62182046 |
| Quito | 1.71157116 | 1.68215658 | 1.68215658 |

Doubling the grid changes gaseous loss by at most $1.7133\times10^{-6}$ dB,
apparent elevation by $6.1053\times10^{-5}$ degrees, and excess delay by
$2.5094\times10^{-13}$ s. No reference MODCOD selections differ between the two
atmospheric grids. These are model-derived results for the declared example,
not measured throughput or general evidence of discretization error bounds.
The study command above regenerates the table and
[comparison figure](images/reference-propagation-ablation.svg).

The path tests compare the following literal official-workbook cases, using the
normalized equation-(16) grid, 28 GHz, lower height 1.3 km AMSL, and apparent
elevation 30 degrees:

| Upper height (km AMSL) | Total gaseous attenuation (dB) | Bending (rad) |
| ---: | ---: | ---: |
| 8 | 0.24376211236218553 | 0.0002517972739610741 |
| 100 | 0.27744110604568128 | 0.00045579353223956787 |

The source worksheet is `P.676-13 A_Gas_A1_2.2.1b`. Its exact cell locations are:

| Case | Frequency / lower height / upper height / apparent elevation | Total attenuation | Bending |
| --- | --- | --- | --- |
| 1.3–8 km | `C24` / `D24` / `E24` / `F24` | `AF24` | `AE24` |
| 1.3–100 km | `AI24` / `AJ24` / `AK24` / `AL24` | `BL24` | `BK24` |

These use 182 and 434 normalized base layers, respectively. The separate
`P.676-13 A_Gas_A1_2.2.1a` surface-to-space case uses the fixed 922-layer grid;
its different upper boundary prevents using its total as an exact regression for
a normalized 0–100 km column.

Both quantities are checked at relative tolerance `1e-8`. Independent analytic
checks cover a uniform shell, a finite endpoint through a uniform refractive
medium followed by vacuum, vacuum zero loss and delay, and zenith integration.
Refinement tests compare 1×, 2×, and 4× layers. The profile tests compare 14 literal
workbook midpoint states across the altitude range, including the humidity floor
and upper atmosphere. These checks establish agreement with the stated numerical
references, not observational or calibrated RF validation.

For example, the profile sheet `P.676-13 A_Gas_A1_2.2.1a` has the following
literal midpoint state at row 645:

| Cell | Quantity | Value |
| --- | --- | ---: |
| `L645` | Geometric height (km AMSL) | 5.0171241287354311 |
| `M645` | Total pressure (hPa) | 539.24921032551606 |
| `N645` | Temperature (K) | 255.56441157757112 |
| `O645` | Water-vapour density (g/m³) | 0.61038886346739474 |
| `Q645` | Water-vapour pressure (hPa) | 0.71986004026556139 |
| `R645` | Refractive index (dimensionless) | 1.0001678558724172 |

The workbook labels this profile P.835-6; the implemented P.835-7 Annex 1 retains
the equations used by these reference cases. The profile assertions specify
relative tolerance `5e-13`; temperature and total/dry pressure also allow an
absolute floor of `1e-12` in their respective units. Density and vapour pressure
have no absolute tolerance floor, and refractive index uses absolute tolerance
`3e-16`. The executable references are
[`test_reference_atmosphere.py`](../tests/test_reference_atmosphere.py) and
[`test_atmospheric_path.py`](../tests/test_atmospheric_path.py).

The official workbook is
[`CG-3M3J-13-ValEx-Rev8.3.0.xlsx`](https://www.itu.int/en/ITU-R/study-groups/rsg3/rwp3m/Validation%20Example/CG-3M3J-13-ValEx-Rev8.3.0.xlsx),
SHA-256 `e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546`.
The workbook and ITU recommendation PDFs are not redistributed. The public tests
retain a small set of numeric reference values and their provenance.

The scientific source PDF fingerprints recorded in propagation metadata are:

| Recommendation | SHA-256 |
| --- | --- |
| P.835-7, August 2024 | `0c355e9471dd382592b46b35cd16346172700fb2e472b95dea9fd1229b087df3` |
| P.453-14, August 2019 | `fd163ef75cb4fd03848d4fe1fc0a043cb78e0edc164ba29a6bb69209204c17a0` |
| P.676-13, August 2022 | `8c09b2d2c120bdae33f60c2a7abff873374d54075ce0dc71060de8a5507bbe2f` |

The profile and layered geometry are implementations of the cited recommendation
equations. The existing spectrum equations and coefficient data are adapted from
[ITU-Rpy](https://github.com/inigodelportillo/ITU-Rpy) commit
`f739993c4b6d34076de22249ef53d03fa5a53d73`, specifically `itur/models/itu676.py`,
`itur/data/676/v13_lines_oxygen.txt`, and `itur/data/676/v13_lines_water_vapour.txt`.
The complete upstream MIT notice is retained in
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

The current model does not ingest weather, radiosondes, or ERA5; select seasonal
or latitude-dependent profiles; add rain, cloud, fog, scintillation, atmospheric
emission, or interference; or correct contacts, Doppler, or dispersive group delay.
The frozen orbit data and synthetic RF assumptions remain subject to the evidence
boundaries in [THIRD_PARTY_DATA.md](../THIRD_PARTY_DATA.md) and
[VALIDATION.md](VALIDATION.md).
