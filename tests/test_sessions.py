"""Superseded sessions must not modify the table."""

from __future__ import annotations

from tests.conftest import (
    PRIMARY,
    PRIMARY_ROUTES,
    prime_two_neighbors,
    reconnect,
)


def test_stale_announcement_is_rejected_and_table_unchanged(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)
    before = client.get("/v1/routes").json()

    response = client.post(
        f"/v1/neighbors/{PRIMARY}/announce",
        json={
            "session_id": 1,
            "routes": [{"prefix": "203.0.113.0/24", "next_hop": "10.6.6.6"}],
        },
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "stale_session"
    assert error["details"]["current_session"] == 2
    assert error["details"]["received_session"] == 1
    assert client.get("/v1/routes").json() == before


def test_stale_withdrawal_is_rejected_and_table_unchanged(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)
    before = client.get("/v1/routes").json()

    response = client.post(
        f"/v1/neighbors/{PRIMARY}/withdraw",
        json={"session_id": 1, "prefixes": ["203.0.113.0/24"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_session"
    assert client.get("/v1/routes").json() == before


def test_stale_end_of_rib_cannot_purge_pending_routes(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    response = client.post(
        f"/v1/neighbors/{PRIMARY}/end-of-rib", json={"session_id": 1}
    )

    assert response.status_code == 409
    remaining = [
        r for r in client.get("/v1/routes").json()["routes"] if r["neighbor"] == str(PRIMARY)
    ]
    assert len(remaining) == len(PRIMARY_ROUTES)
    assert all(r["pending"] for r in remaining)


def test_future_session_must_reconnect_first(client):
    prime_two_neighbors(client)

    for path, payload in (
        ("announce", {"session_id": 9, "routes": [{"prefix": "203.0.113.0/24", "next_hop": "10.0.0.9"}]}),
        ("withdraw", {"session_id": 9, "prefixes": ["203.0.113.0/24"]}),
        ("end-of-rib", {"session_id": 9}),
    ):
        response = client.post(f"/v1/neighbors/{PRIMARY}/{path}", json=payload)
        assert response.status_code == 409, path
        assert response.json()["error"]["code"] == "session_not_established"


def test_reconnect_must_be_strictly_increasing(client):
    prime_two_neighbors(client)

    same = client.post(
        f"/v1/neighbors/{PRIMARY}/reconnect", json={"session_id": 1}
    )
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "session_not_increasing"

    older = client.post(
        f"/v1/neighbors/{PRIMARY}/reconnect", json={"session_id": 0}
    )
    assert older.status_code == 422  # pydantic ge=1


def test_unknown_neighbor_is_rejected(client):
    response = client.post(
        "/v1/neighbors/198.18.0.1/announce",
        json={"session_id": 1, "routes": [{"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_neighbor"


def test_stale_messages_in_batch_are_reported_but_do_not_abort_batch(client):
    prime_two_neighbors(client)
    reconnect(client, str(PRIMARY), 2, grace=60)

    response = client.post(
        "/v1/events",
        json={
            "events": [
                {
                    "type": "announce",
                    "neighbor": str(PRIMARY),
                    "session_id": 1,
                    "routes": [{"prefix": "203.0.113.0/24", "next_hop": "10.6.6.6"}],
                },
                {
                    "type": "announce",
                    "neighbor": str(PRIMARY),
                    "session_id": 2,
                    "routes": [{"prefix": "203.0.113.0/24", "next_hop": "10.0.0.7"}],
                },
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == 1
    assert body["rejected"] == 1
    assert body["outcomes"][0]["accepted"] is False
    assert body["outcomes"][0]["error"]["code"] == "stale_session"
    assert body["outcomes"][1]["accepted"] is True


def test_invalid_ipv4_is_rejected_at_validation(client):
    response = client.post(
        "/v1/lookup", json={"destinations": ["2001:db8::1"]}
    )
    assert response.status_code == 422
    assert client.post(
        f"/v1/neighbors/{PRIMARY}/announce",
        json={"session_id": 1, "routes": [{"prefix": "203.0.113.5/24", "next_hop": "10.0.0.1"}]},
    ).status_code == 422
