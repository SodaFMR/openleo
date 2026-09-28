# Coupled declared hydrometeors

An optional `propagation.hydrometeors` object couples fixed, explicitly declared
cloud and rain losses into the constellation radio budget. Omission preserves
gas-only schema 2; omission of propagation preserves free-space schema 1.
Hydrometeors produce schema 3. This is a deterministic sensitivity experiment,
not inferred local weather, measured link performance or P.618 exceedance statistics.

The legacy `propagation-study` and `fidelity-study` workflows retain their gas-only
reference/refinement scope and reject declared hydrometeors before computing
cases or creating output. Use an integrated experiment for coupled hydrometeor
comparisons, including declarations with zero rain and cloud amounts.

```json
{
  "model": "itu_reference",
  "station_heights_amsl_m": {"Source": 650.0, "Target": 0.0},
  "refinement": 1,
  "hydrometeors": {
    "model": "declared_uniform_layers",
    "stations": {
      "Source": {"liquid_water_kg_m2": 0.5, "rain_rate_mm_h": 25.0,
                 "rain_top_height_amsl_m": 5000.0, "polarization_tilt_deg": 0.0},
      "Target": {"liquid_water_kg_m2": 0.0, "rain_rate_mm_h": 0.0,
                 "rain_top_height_amsl_m": 5000.0, "polarization_tilt_deg": 90.0}
    }
  }
}
```

Every configured station must appear exactly once. Unknown or missing keys,
duplicate JSON names, non-numeric or non-finite values, booleans and out-of-domain
inputs are rejected. Public configuration dataclasses and their station tuples
are immutable. States stay constant throughout the run.

The supported intersection of the gaseous and cloud domains is 1–200 GHz and
geometric elevation 5–90 degrees. Liquid water and rain rate are non-negative;
polarization tilt is 0–180 degrees. Station heights use the existing independent
AMSL map, 0–10000 m, rather than station WGS84 ellipsoid height. Rain-top height is
between the station AMSL height and 20000 m inclusive. Arithmetic overflow is an
error. No rain-top height or water amount is inferred.

## Equations and units

Cloud loss reuses the existing [ITU-R P.840-9](https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.840-9-202308-I!!PDF-E.pdf)
Annex 1 equations (11)–(12): `A_cloud = KL(f) * L / sin(elevation)` in dB.
`KL` is the corrected mass absorption coefficient in dB/(kg/m²), with the
273.75 K reference liquid-water temperature and equation (12) frequency correction.
`L` is a vertical integrated liquid-water column in kg/m² lying above the station;
the model assumes that column is horizontally uniform over the slant path. Cloud
loss uses the geometric elevation and plane-parallel column scaling; it does not
reuse the bent gaseous ray or integrate a spherical cloud layer.

Rain specific attenuation reuses [ITU-R P.838-3](https://www.itu.int/dms_pubrec/itu-r/rec/p/r-rec-p.838-3-200503-i!!pdf-e.pdf)
Annex 1 equations (1)–(5): `gamma = k * R^alpha` in dB/km, with `R` in mm/h.
Polarization and geometric elevation enter the existing `k` and `alpha` fits.
The declared rate is uniform throughout a straight geometric path within a
spherical layer. This path construction is an OpenLEO declared geometry, not a
P.838 path prediction or a P.618 effective-path reduction.

For mean Earth radius `Re = 6371000 m`, station AMSL height `h`, rain-top AMSL
height `H` and geometric elevation `e`, let `r = Re + h`, `d = H - h`,
`a = r*sin(e)` and `D = d*(2*r + d)`. The positive shell-intersection root is
`s = D / (sqrt(a*a + D) + a)` in metres. This rationalized form avoids cancellation
in thin layers. At zenith `s = H-h`; at zero thickness `s = 0`.
`A_rain = gamma * (s / 1000)` in dB; zero rain rate gives zero rain loss.
Visible LEO endpoints are above the declared rain top (and above the existing
100 km reference-column endpoint boundary).

The radio budget uses `A_hydro = A_cloud + A_rain`,
`A_total = A_gas + A_hydro`, and `CN0 = CN0_free_space - A_total` before SNR,
Es/N0, Shannon upper-bound calculation and MODCOD selection. Zero declared
hydrometeors exactly reproduce all legacy gas-only link values. Receiver noise
temperature stays fixed; atmospheric emission is omitted. Delay retains the
existing geometric and reference gaseous excess delays. Hydrometeors do not
change visibility, Doppler, apparent elevation or contact sampling.

## Portable schema 3

Schema 3 retains every schema 2 field and appends:

| Field | Unit |
| --- | --- |
| `cloud_attenuation_db` | dB |
| `rain_specific_attenuation_db_per_km` | dB/km |
| `rain_path_length_m` | m |
| `rain_attenuation_db` | dB |
| `hydrometeor_attenuation_db` | dB |
| `total_atmospheric_attenuation_db` | dB |

Propagation metadata declares state, sources, pinned source hashes, domain,
geometry, units and coupling assumptions. Python bundle readers validate the
schema/configuration pairing, metadata, shell path, attenuation sums, dB/km to dB
conversion, CN0/SNR/EsN0 and delay relations, along with existing bundle hashes
and canonical scenario hashes when supplied. These are artifact consistency
checks; readers do not rerun orbit or atmospheric models. CSV exports carry all
schema 3 fields. Snapshot network routing consumes the corrected rate and
unchanged delay using its existing algorithms.

`examples/constellations/iridium_global_hydrometeors.json` declares synthetic
conditions, including a dry station, for the archived global geometry. Its
water/rain values are experiment assumptions with no observational claim.
