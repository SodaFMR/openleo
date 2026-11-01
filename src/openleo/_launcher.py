"""Console-script entry point: limit BLAS threads before numpy loads, then run the CLI.

Many worker processes each running multi-threaded OpenBLAS oversubscribe the machine, so the
script sets one thread per process unless the user already chose otherwise. The library import
never does this (``import openleo`` leaves the environment alone), and this module imports
nothing from ``openleo`` before the variables are set: the package ``__init__`` is lazy for the
same reason.
"""

from __future__ import annotations

import os


def main() -> int:
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from openleo.cli import main as cli_main

    return cli_main()
