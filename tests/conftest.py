"""Shared pytest configuration."""

import os

# OpenBLAS threads spin on the engine's many small matrix products (the ray-trace layer sums)
# and slow a run by about 40 %, and more with worker processes. One thread per process for the
# test session; worker processes inherit the environment. Set before numpy is first imported.
for _variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_variable, "1")

# Frozen v1.0.0 fixtures and their generator are data, not tests: the generator only runs
# against the v1.0.0 tag and must never be collected or imported by the 2.0 suite.
collect_ignore = ["fixtures"]
