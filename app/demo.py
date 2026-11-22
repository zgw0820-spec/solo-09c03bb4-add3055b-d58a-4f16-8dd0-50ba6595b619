"""Deterministic demo data used by Docker Compose and the test suite.

Two neighbours share the same destination space:

* ``192.0.2.1`` (priority 10, preferred) — the primary path.
* ``192.0.2.2`` (priority 20, backup)     — the backup path.

Each neighbour carries an aggregate plus two more-specific prefixes, so the
longest-prefix behaviour is visible even before a reconnect.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network

from .service import RouteService

PRIMARY = IPv4Address("192.0.2.1")
BACKUP = IPv4Address("192.0.2.2")
PRIMARY_PRIORITY = 10
BACKUP_PRIORITY = 20

PRIMARY_ROUTES: tuple[tuple[str, str], ...] = (
    ("203.0.113.0/24", "10.0.0.1"),
    ("198.51.100.0/24", "10.0.0.1"),
    ("198.51.100.128/25", "10.0.0.1"),
)

BACKUP_ROUTES: tuple[tuple[str, str], ...] = (
    ("203.0.113.0/24", "10.0.0.2"),
    ("198.51.100.0/24", "10.0.0.2"),
)

DEMO_DESTINATIONS: tuple[str, ...] = (
    "203.0.113.10",   # only the shared /24 exists
    "198.51.100.10",  # less-specific /24
    "198.51.100.200", # longer /25 only the primary has
    "192.0.2.77",     # no route at all
)


def seed(service: RouteService) -> None:
    """Populate the two-neighbour baseline (idempotent per fresh service)."""
    service.reconnect(PRIMARY, 1, grace_seconds=0)
    service.announce(
        PRIMARY,
        1,
        [(IPv4Network(prefix), IPv4Address(next_hop)) for prefix, next_hop in PRIMARY_ROUTES],
    )
    service.end_of_rib(PRIMARY, 1)

    service.reconnect(BACKUP, 1, grace_seconds=0)
    service.announce(
        BACKUP,
        1,
        [(IPv4Network(prefix), IPv4Address(next_hop)) for prefix, next_hop in BACKUP_ROUTES],
    )
    service.end_of_rib(BACKUP, 1)


def sample_payloads() -> dict[str, object]:
    """Payloads referenced from the README and exercised by tests."""
    return {
        "reconnect_primary_session_2": {
            "session_id": 2,
            "grace_seconds": 20,
            "reason": "BGP session reset by peer",
        },
        "stale_primary_session_1_announce": {
            "session_id": 1,
            "routes": [{"prefix": "198.51.100.0/24", "next_hop": "10.0.0.1"}],
        },
        "primary_session_2_withdraw": {
            "session_id": 2,
            "prefixes": ["198.51.100.128/25"],
        },
        "primary_session_2_end_of_rib": {"session_id": 2},
        "lookup": {"destinations": list(DEMO_DESTINATIONS)},
    }
