"""Verified OpenLEO physics kernels.

The modules hold the v1.0.0 scalar kernels unchanged, plus array forms that evaluate
the same functions element-wise on numpy arrays:

- ``link``: free-space link budget (delay, Doppler, FSPL, noise, C/N0, SNR, Shannon);
- ``adaptation``: DVB-S2 reference MODCOD selection;
- ``gases`` and ``_p676_coefficients``: ITU-R P.676-13 specific gaseous attenuation;
- ``reference_atmosphere``: ITU-R P.835-7 / P.453-14 reference profile;
- ``atmospheric_path``: P.676-13 Annex 1 ray tracing through the reference column;
- ``hydrometeors`` and ``hydrometeor_path``: P.838-3 rain and P.840-9 cloud losses.
"""
