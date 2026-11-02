"""The local OpenLEO app: a loopback HTTP API for v2 runs and sweeps (design spec section 10).

``create_server`` binds it, ``run_app`` serves it until Ctrl-C, and ``JobQueue`` runs its jobs
one at a time. Routes and security rules are documented in ``openleo.app.api`` and
``openleo.app.server``.
"""

from openleo.app.jobs import JobQueue
from openleo.app.server import create_server, run_app

__all__ = ["JobQueue", "create_server", "run_app"]
