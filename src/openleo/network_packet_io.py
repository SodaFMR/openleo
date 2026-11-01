"""v1 import path of the network replay protocol, now ``openleo.replay.network_packet_io``.

Re-exported until the v1 modules are removed.
"""

from openleo.replay.network_packet_io import (
    EDGE_FIELDS,
    HOP_FIELDS,
    MAX_HOPS,
    PACKET_FIELDS,
    ROUTE_FIELDS,
    STATUSES,
    _parse_network_results,
)

__all__ = [
    "EDGE_FIELDS",
    "HOP_FIELDS",
    "MAX_HOPS",
    "PACKET_FIELDS",
    "ROUTE_FIELDS",
    "STATUSES",
    "_parse_network_results",
]
