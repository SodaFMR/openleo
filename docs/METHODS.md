# Methods

This document summarizes the shared physical and numerical methods used by OpenLEO.
Model-specific details and literal reference cases remain in
[reference propagation](REFERENCE_PROPAGATION.md), [specific attenuation](GASES.md),
[sensitivity](SENSITIVITY.md), and [validation](VALIDATION.md).

## Inputs, time, and frames

Orbital inputs are archived GP records with OMM-compatible field names. They are fitted
mean elements for SGP4, not time series of measured positions or ordinary osculating
Keplerian elements. OpenLEO records each source fingerprint, element epoch, and signed
age. An age warning is a quality indicator, not an orbit-error bound.

Skyfield supplies the corrected Vallado SGP4 implementation and time/frame
transformations. OpenLEO records the Skyfield, SGP4, and built-in leap-second-table
provenance instead of duplicating those transformations. See Skyfield's
[satellite documentation](https://rhodesmill.org/skyfield/earth-satellites.html) for
the relation between fitted element formats and SGP4.

The frame chain is:

```text
OMM-compatible mean elements
  -> SGP4 state in TEME
  -> Skyfield transformation to ITRS/ECEF
  -> WGS84 station subtraction
  -> local east/north/up geometry
```

Public times are timezone-aware UTC. Ground stations use WGS84 geodetic latitude,
longitude, and ellipsoidal height for orbital geometry. Reference-atmosphere station
heights are separate geometric heights above mean sea level; OpenLEO applies no
implicit geoid conversion.

For east, north, and up components `E`, `N`, and `U`:

$$
\rho=\sqrt{E^2+N^2+U^2},\qquad
elevation=\sin^{-1}(U/\rho),\qquad
azimuth=\operatorname{atan2}(E,N)\bmod 360^\circ.
$$

Azimuth is clockwise from north. Elevation is above the local geometric horizon;
it is not spacecraft altitude.

## Range, delay, and Doppler

The geometric one-way vacuum delay and radial range rate are

$$
\tau=\frac{\rho}{c},\qquad
\dot\rho=\frac{\boldsymbol\rho\cdot\dot{\boldsymbol\rho}}{\rho},
$$

with `c = 299792458 m/s`. Nominal first-order one-way downlink Doppler is

$$
\Delta f=-f_c\frac{\dot\rho}{c}.
$$

OpenLEO uses negative range rate while approaching and positive range rate while
separating. The Doppler sign is opposite: approach is positive and separation is
negative.

Worked example: for range `1,495,209.6389 m`, range rate `-6,709.8042 m/s`,
and carrier `2.2 GHz`,

$$
\tau=4.98748\ \mathrm{ms},\qquad
\Delta f\approx +49.2393\ \mathrm{kHz}.
$$

This is not a two-way tracking observable or a relativistic precision model.

## Free-space receiver chain

For range `rho`, frequency `f`, EIRP, receive gain, miscellaneous loss, system
temperature `T`, and equivalent noise bandwidth `B`:

$$
\begin{aligned}
L_{FSPL}&=20\log_{10}\left(\frac{4\pi\rho f}{c}\right),\\
C_{dBW}&=EIRP+G_{rx}-L_{FSPL}-L_{misc},\\
N_{0,dBW/Hz}&=10\log_{10}(kT),\\
C/N_0&=C_{dBW}-N_{0,dBW/Hz},\\
SNR_{dB}&=C/N_0-10\log_{10}(B),\\
C_{max}&=B\log_2\left(1+10^{SNR_{dB}/10}\right).
\end{aligned}
$$

The free-space expression follows
[ITU-R P.525-5](https://www.itu.int/rec/R-REC-P.525-5-202411-I/en). `C/N0` uses
noise per hertz; SNR uses noise across the declared bandwidth. `C_max` is the
Shannon-Hartley theoretical upper bound, not a modem rate or packet throughput.

Worked receiver example:

| Quantity | Value |
| --- | ---: |
| EIRP | 10 dBW |
| Receive gain | 20 dBi |
| Path loss | 161 dB |
| Miscellaneous loss | 2 dB |
| System temperature | 200 K |
| Bandwidth | 1 MHz |

The received carrier is `-133 dBW`, noise density is
`-205.588867217 dBW/Hz`, `C/N0` is `72.588867217 dB-Hz`, and SNR is
`12.588867217 dB`. Increasing EIRP by 3 dB increases each logarithmic carrier
metric by 3 dB, but the Shannon bound changes nonlinearly.

## Sampling and integration

Time grids include the declared stop timestamp. If the remaining interval is shorter
than the requested step, the final interval is short rather than discarded.
Single-pass AOS and LOS values are sampled mask crossings; they are not interpolated
continuous-time events.

The single-pass Shannon-Hartley bound uses trapezoidal integration between adjacent
visible samples:

$$
I_{bound}=\sum_{i=0}^{n-2}
\frac{C_i+C_{i+1}}{2}(t_{i+1}-t_i).
$$

Constellation link and route metrics use a left-held state instead:

$$
I=\sum_{i=0}^{n-2} R_i(t_{i+1}-t_i).
$$

The final sample contributes no duration beyond the declared stop time. Changing a
step can change mask-boundary samples and quadrature nodes, so errors need not decrease
monotonically for every pair of grids. The smallest declared step is a numerical
reference, not truth or proof of convergence.

Fidelity-study visibility, RF outage, usability, connection, and disconnection
durations use those left-held intervals, including the actual short final interval.

## Reference adaptation

For symbol rate `R_s` and root-raised-cosine roll-off `alpha`, configurations
require

$$
B\geq R_s(1+\alpha),\qquad E_s/N_0=C/N_0-10\log_{10}(R_s).
$$

Five DVB-S2 modes use ideal AWGN thresholds from
[ETSI EN 302 307-1 V1.4.1, Table 13](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf).
A mode requires its threshold plus the declared implementation margin. An upgrade
also requires the hysteresis margin; falling below the current requirement selects a
lower supported mode or an RF outage. Rate is `R_s` times the reference spectral
efficiency. Acquisition, pilots beyond the table assumptions, tracking dynamics,
protocol overhead, and actual decoding are absent.

## Snapshot routing

At every time sample, OpenLEO can calculate:

1. minimum-delay routes through RF-usable adaptive ground links;
2. maximum-bottleneck routes, breaking equal-width ties by minimum delay; and
3. minimum-delay routes with a declared fixed capacity on every geometrically visible
   ground link.

All three use the same positions, visibility mask, inter-satellite assumptions, and
ground-link delays. The fixed-capacity case changes ground-link rate only. Path delay
is the sum of selected edge delays; bottleneck rate is the minimum selected edge rate.
These are graph snapshots without traffic, contention, queues, or transport protocols.

## Reference atmosphere

The optional model combines:

- ITU-R P.835-7 global reference temperature, pressure, and humidity;
- ITU-R P.453-14 radio refractivity; and
- ITU-R P.676-13 Annex 1 line-by-line gaseous attenuation and layered ray geometry.

Gaseous attenuation is subtracted from free-space `C/N0` before SNR and reference
mode selection. A bounded endpoint solve converts geometric range/elevation to apparent
launch elevation through a mean-radius spherical atmosphere. Delay includes bent-path
and refractive optical excess. Visibility, mask crossings, and nominal Doppler remain
geometric.

See [Reference propagation](REFERENCE_PROPAGATION.md) for equations, supported domains,
official workbook cases, dry/total pressure handling, units, and numerical tolerances.
The model is an idealized reference profile, not local weather.

## Verification and interpretation

OpenLEO checks frozen engine results, independently recomputed topocentric geometry,
hand-derived receiver invariants, finite-difference range rate, official ITU workbook
values, analytic atmosphere cases, output schemas, and artifact hashes. Exact evidence
and tolerances are listed in [Validation](VALIDATION.md).

Reproducibility means repeating the declared computation. Physical validation requires
independent observations with sufficient timing, station, antenna, receiver,
processing, and meteorological metadata. Passing software tests does not supply that
evidence. Apply the [scope and claim boundary](SCOPE.md) when reporting results.
