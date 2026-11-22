"""Longest-prefix and neighbour-priority selection."""

from __future__ import annotations

from tests.conftest import (
    match_for,
    lookup,
    prime_two_neighbors,
)


def test_longest_prefix_wins_within_one_neighbor(client):
    prime_two_neighbors(client)

    body = lookup(client, "198.51.100.10", "198.51.100.200")

    assert body["found_count"] == 2
    less_specific = match_for(body, "198.51.100.10")
    assert less_specific["prefix"] == "198.51.100.0/24"
    assert less_specific["next_hop"] == "10.0.0.1"

    more_specific = match_for(body, "198.51.100.200")
    assert more_specific["prefix"] == "198.51.100.128/25"
    assert more_specific["status"] == "confirmed"


def test_lower_priority_number_breaks_same_prefix_tie(client):
    prime_two_neighbors(client)

    body = lookup(client, "203.0.113.10")
    match = match_for(body, "203.0.113.10")

    assert match["found"] is True
    assert match["prefix"] == "203.0.113.0/24"
    assert match["next_hop"] == "10.0.0.1"
    assert match["neighbor"] == "192.0.2.1"
    assert match["neighbor_priority"] == 10


def test_unrouted_destination_is_explicitly_not_found(client):
    prime_two_neighbors(client)

    body = lookup(client, "192.0.2.200", "203.0.113.10")

    missing = match_for(body, "192.0.2.200")
    assert missing["found"] is False
    assert missing["status"] == "not_found"
    assert missing["prefix"] is None
    assert missing["next_hop"] is None
    assert missing["neighbor"] is None
    assert body["not_found_count"] == 1


def test_lookup_with_empty_table_reports_everything_not_found(client):
    body = lookup(client, "203.0.113.10", "10.0.0.1")
    assert body["route_count"] == 0
    assert body["found_count"] == 0
    assert all(match["status"] == "not_found" for match in body["matches"])


def test_batch_uses_one_shared_revision(client):
    prime_two_neighbors(client)
    body = lookup(client, "203.0.113.10", "198.51.100.200", "192.0.2.200")
    assert body["revision"] == client.get("/v1/routes").json()["revision"]
    assert len({match["route_session_id"] for match in body["matches"] if match["found"]}) == 1


def test_priority_preference_only_applies_after_prefix_and_pending(client):
    """A more specific prefix from the backup neighbour beats the primary /24."""
    prime_two_neighbors(client)

    # Backup advertises a /25 that is more specific than the primary /24.
    client.post(
        "/v1/neighbors/192.0.2.2/announce",
        json={
            "session_id": 1,
            "routes": [{"prefix": "203.0.113.0/25", "next_hop": "10.0.0.2"}],
        },
    )

    match = match_for(lookup(client, "203.0.113.10"), "203.0.113.10")
    assert match["prefix"] == "203.0.113.0/25"
    assert match["neighbor"] == "192.0.2.2"
    assert match["neighbor_priority"] == 20
