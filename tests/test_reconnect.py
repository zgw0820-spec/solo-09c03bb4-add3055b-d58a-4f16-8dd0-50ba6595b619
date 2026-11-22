"""Reconnect takeover, pending retention and batch cleanup."""

from __future__ import annotations

import time

from tests.conftest import (
    BACKUP,
    BACKUP_ROUTES,
    PRIMARY,
    PRIMARY_ROUTES,
    announce,
    end_of_rib,
    lookup,
    match_for,
    prime_two_neighbors,
    reconnect,
    withdraw,
)


def test_reconnect_retains_previous_routes_as_pending(client):
    prime_two_neighbors(client)

    result = reconnect(client, str(PRIMARY), 2, grace=60)

    assert result["previous_session_id"] == 1
    assert result["session_id"] == 2
    assert result["retained_pending_routes"] == len(PRIMARY_ROUTES)

    routes = client.get("/v1/routes").json()
    primary = [r for r in routes["routes"] if r["neighbor"] == str(PRIMARY)]
    assert len(primary) == len(PRIMARY_ROUTES)
    assert all(r["pending"] is True for r in primary)
    assert routes["pending_count"] == len(PRIMARY_ROUTES)

    # The backup neighbour is untouched and still confirmed.
    backup = [r for r in routes["routes"] if r["neighbor"] == str(BACKUP)]
    assert backup and all(r["pending"] is False for r in backup)


def test_current_session_announcements_take_effect_immediately(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    announce(client, str(PRIMARY), 2, [("203.0.113.0/24", "10.9.9.9")])

    match = match_for(lookup(client, "203.0.113.10"), "203.0.113.10")
    assert match["status"] == "confirmed"
    assert match["next_hop"] == "10.9.9.9"
    assert match["route_session_id"] == 2


def test_current_session_withdrawal_takes_effect_immediately(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    result = withdraw(client, str(PRIMARY), 2, ["198.51.100.0/24"])

    assert result["removed"] == 1
    # The /25 is still pending on the same neighbour.
    assert result["pending_remaining"] == 2
    match = match_for(lookup(client, "198.51.100.10"), "198.51.100.10")
    # Backup's /24 now supplies the route to that destination.
    assert match["neighbor"] == str(BACKUP)
    assert match["status"] == "confirmed"


def test_withdrawing_absent_prefix_is_reported(client):
    prime_two_neighbors(client)
    result = withdraw(client, str(PRIMARY), 1, ["192.0.2.0/24"])
    assert result["removed"] == 0
    assert result["not_present"] == ["192.0.2.0/24"]


def test_end_of_rib_removes_pending_routes_in_one_batch(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)
    # Replace one retained route from the new session, keeping the rest pending.
    announce(client, str(PRIMARY), 2, [("203.0.113.0/24", "10.0.0.9")])

    result = end_of_rib(client, str(PRIMARY), 2)

    assert result["purged_pending"] == len(PRIMARY_ROUTES) - 1
    assert result["pending_remaining"] == 0
    assert result["confirmed_remaining"] == 1

    routes = client.get("/v1/routes").json()
    primary = [r for r in routes["routes"] if r["neighbor"] == str(PRIMARY)]
    assert len(primary) == 1
    assert primary[0]["pending"] is False

    # Failover visible: the destinations only reached via the old primary
    # prefixes now resolve through the backup neighbour, or not at all.
    assert match_for(lookup(client, "198.51.100.200"), "198.51.100.200")["neighbor"] == str(BACKUP)
    assert match_for(lookup(client, "203.0.113.10"), "203.0.113.10")["next_hop"] == "10.0.0.9"


def test_end_of_rib_does_not_wait_for_other_neighbors(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    # Backup still updating concurrently; the primary's End-of-RIB applies now.
    announce(client, str(BACKUP), 1, [("192.0.2.0/24", "10.0.0.2")])
    end_of_rib(client, str(PRIMARY), 2)
    announce(client, str(BACKUP), 1, [("203.0.113.0/24", "10.0.1.2")])

    primary = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(PRIMARY)]
    assert primary == []
    backup = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(BACKUP)]
    assert any(r["next_hop"] == "10.0.1.2" for r in backup)


def test_grace_expiry_removes_pending_routes(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=0.2)

    # Pending routes are briefly visible.
    pending = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(PRIMARY)]
    assert pending and all(r["pending"] for r in pending)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = [
            r
            for r in client.get("/v1/routes").json()["routes"]
            if r["neighbor"] == str(PRIMARY)
        ]
        if not remaining:
            break
        time.sleep(0.05)
    assert remaining == []

    # Backup routes survive.
    backup = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(BACKUP)]
    assert len(backup) == len(BACKUP_ROUTES)


def test_end_of_rib_before_grace_cancels_the_timer(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=0.2)
    end_of_rib(client, str(PRIMARY), 2)

    # A new confirmed route from the new session must not be touched later.
    announce(client, str(PRIMARY), 2, [("198.51.100.0/24", "10.0.0.5")])
    time.sleep(0.4)

    primary = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(PRIMARY)]
    assert len(primary) == 1
    assert primary[0]["pending"] is False


def test_second_reconnect_supersedes_first_timer(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=0.2)
    # Session 3 replaces session 2 before its grace window elapses.
    reconnect(client, str(PRIMARY), 3, grace=60)
    time.sleep(0.4)

    primary = [r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(PRIMARY)]
    assert len(primary) == len(PRIMARY_ROUTES)
    assert all(r["pending"] for r in primary)
    assert client.get("/v1/neighbors").json()["neighbors"][0]["session_id"] == 3
