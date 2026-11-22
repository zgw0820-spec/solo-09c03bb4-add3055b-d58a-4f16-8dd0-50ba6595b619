"""Shared test fixtures."""

from __future__ import annotations

from ipaddress import IPv4Address

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.demo import BACKUP, PRIMARY
from app.main import create_app
from app.service import RouteService


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        neighbor_priorities={PRIMARY: 10, BACKUP: 20},
        default_neighbor_priority=100,
        grace_seconds=30.0,
        demo_seed=False,
    )


@pytest.fixture()
def service(settings: Settings) -> RouteService:
    return RouteService(
        neighbor_priorities=settings.neighbor_priorities,
        default_neighbor_priority=settings.default_neighbor_priority,
        grace_period_seconds=settings.grace_seconds,
    )


@pytest.fixture()
def client(service: RouteService, settings: Settings):
    app = create_app(service=service, settings=settings)
    with TestClient(app) as test_client:
        yield test_client


def announce(client: TestClient, neighbor: str, session: int, routes):
    response = client.post(
        f"/v1/neighbors/{neighbor}/announce",
        json={
            "session_id": session,
            "routes": [
                {"prefix": prefix, "next_hop": next_hop} for prefix, next_hop in routes
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def reconnect(client: TestClient, neighbor: str, session: int, grace: float | None = None):
    payload: dict[str, object] = {"session_id": session}
    if grace is not None:
        payload["grace_seconds"] = grace
    response = client.post(f"/v1/neighbors/{neighbor}/reconnect", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def withdraw(client: TestClient, neighbor: str, session: int, prefixes):
    response = client.post(
        f"/v1/neighbors/{neighbor}/withdraw",
        json={"session_id": session, "prefixes": list(prefixes)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def end_of_rib(client: TestClient, neighbor: str, session: int):
    response = client.post(
        f"/v1/neighbors/{neighbor}/end-of-rib", json={"session_id": session}
    )
    assert response.status_code == 200, response.text
    return response.json()


def lookup(client: TestClient, *destinations: str):
    response = client.post(
        "/v1/lookup", json={"destinations": list(destinations)}
    )
    assert response.status_code == 200, response.text
    return response.json()


def match_for(body: dict, destination: str) -> dict:
    return next(
        item for item in body["matches"] if item["destination"] == destination
    )


PRIMARY_ROUTES = [
    ("203.0.113.0/24", "10.0.0.1"),
    ("198.51.100.0/24", "10.0.0.1"),
    ("198.51.100.128/25", "10.0.0.1"),
]
BACKUP_ROUTES = [
    ("203.0.113.0/24", "10.0.0.2"),
    ("198.51.100.0/24", "10.0.0.2"),
]


def prime_two_neighbors(client: TestClient) -> None:
    """Both neighbours at session 1 with the demo route sets."""
    reconnect(client, str(PRIMARY), 1, grace=0)
    announce(client, str(PRIMARY), 1, PRIMARY_ROUTES)
    end_of_rib(client, str(PRIMARY), 1)

    reconnect(client, str(BACKUP), 1, grace=0)
    announce(client, str(BACKUP), 1, BACKUP_ROUTES)
    end_of_rib(client, str(BACKUP), 1)


def assert_neighbor(neighbor: str) -> IPv4Address:  # pragma: no cover - helper
    return IPv4Address(neighbor)
