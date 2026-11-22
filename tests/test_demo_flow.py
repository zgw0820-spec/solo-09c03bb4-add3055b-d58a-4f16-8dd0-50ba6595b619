"""End-to-end demo: replay both neighbour samples, then verify failover.

Each sample file is the collector payload for one neighbour, beginning with
that neighbour's session-1 baseline.  Replaying both (in either order) after a
reset reconstructs the two-neighbour topology used in the README walkthrough.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import PRIMARY, lookup, match_for

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _replay(client, name: str) -> dict:
    payload = json.loads((SAMPLES / name).read_text())
    response = client.post("/v1/events", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_replaying_both_neighbour_samples_moves_traffic_to_backup(client):
    client.post("/v1/admin/reset")

    backup = _replay(client, "neighbor-backup-192.0.2.2.json")
    assert backup["applied"] == 3
    assert backup["rejected"] == 0

    primary = _replay(client, "neighbor-primary-192.0.2.1.json")
    assert [o["type"] for o in primary["outcomes"]] == [
        "reconnect",
        "announce",
        "end_of_rib",
        "reconnect",
        "announce",
        "withdraw",
        "announce",
        "end_of_rib",
    ]
    # The session-1 announcement after the session-2 takeover is rejected.
    assert primary["rejected"] == 1
    stale = primary["outcomes"][6]
    assert stale["accepted"] is False
    assert stale["error"]["code"] == "stale_session"

    body = lookup(
        client,
        "203.0.113.10",
        "198.51.100.10",
        "198.51.100.200",
        "192.0.2.77",
    )

    # Re-announced by the primary's new session, so still preferred.
    reannounced = match_for(body, "203.0.113.10")
    assert reannounced["neighbor"] == str(PRIMARY)
    assert reannounced["status"] == "confirmed"
    assert reannounced["next_hop"] == "10.0.0.1"

    # Every primary route that was not re-announced was purged at End-of-RIB,
    # so this destination now resolves through the backup peer.
    moved = match_for(body, "198.51.100.10")
    assert moved["neighbor"] == "192.0.2.2"
    assert moved["status"] == "confirmed"
    assert moved["next_hop"] == "10.0.0.2"

    # The withdrawn /25 is gone; the backup /24 covers the address.
    more_specific = match_for(body, "198.51.100.200")
    assert more_specific["prefix"] == "198.51.100.0/24"
    assert more_specific["neighbor"] == "192.0.2.2"

    assert match_for(body, "192.0.2.77")["status"] == "not_found"


def test_pending_window_is_visible_before_end_of_rib(client):
    """Between reconnect and End-of-RIB the old routes stay, but as pending."""
    client.post("/v1/admin/reset")
    _replay(client, "neighbor-backup-192.0.2.2.json")
    _replay(client, "neighbor-primary-192.0.2.1.json")

    # Give the primary a prefix no other neighbour covers, so a pending route
    # can be the winning match rather than being shadowed by the backup.
    client.post(
        "/v1/neighbors/192.0.2.1/announce",
        json={
            "session_id": 2,
            "routes": [{"prefix": "198.18.0.0/15", "next_hop": "10.0.0.1"}],
        },
    )

    result = client.post(
        "/v1/neighbors/192.0.2.1/reconnect",
        json={"session_id": 3, "grace_seconds": 20},
    ).json()
    assert result["retained_pending_routes"] == 2

    body = lookup(client, "198.18.1.1", "203.0.113.10")

    # Nobody else covers the exclusive prefix, so the retained pending route
    # is still reported, explicitly flagged pending.
    retained = match_for(body, "198.18.1.1")
    assert retained["status"] == "pending"
    assert retained["neighbor"] == str(PRIMARY)
    assert retained["pending"] is True

    # At equal prefix length a confirmed route beats a pending one, so the
    # shared /24 now resolves through the backup even before End-of-RIB.
    shared = match_for(body, "203.0.113.10")
    assert shared["status"] == "confirmed"
    assert shared["neighbor"] == "192.0.2.2"

    # End-of-RIB drops the remaining pending route in one batch.
    purged = client.post(
        "/v1/neighbors/192.0.2.1/end-of-rib", json={"session_id": 3}
    ).json()
    assert purged["purged_pending"] == 2
    assert match_for(lookup(client, "198.18.1.1"), "198.18.1.1")["status"] == "not_found"


def test_demo_seed_is_swagger_ready(settings):
    """Compose starts seeded so Swagger can demonstrate a reconnect immediately."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.service import RouteService

    seeded_settings = settings.__class__(
        neighbor_priorities=settings.neighbor_priorities,
        default_neighbor_priority=100,
        grace_seconds=settings.grace_seconds,
        demo_seed=True,
    )
    service = RouteService(
        neighbor_priorities=seeded_settings.neighbor_priorities,
        default_neighbor_priority=100,
        grace_period_seconds=seeded_settings.grace_seconds,
    )
    with TestClient(create_app(service=service, settings=seeded_settings)) as client:
        neighbors = client.get("/v1/neighbors").json()["neighbors"]
        assert [n["neighbor"] for n in neighbors] == ["192.0.2.1", "192.0.2.2"]
        assert all(n["end_of_rib_received"] for n in neighbors)

        result = client.post(
            "/v1/neighbors/192.0.2.1/reconnect",
            json={"session_id": 2, "grace_seconds": 20},
        ).json()
        assert result["retained_pending_routes"] == 3
