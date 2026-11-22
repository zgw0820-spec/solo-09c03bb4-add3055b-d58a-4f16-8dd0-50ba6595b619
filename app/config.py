"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import IPv4Address


class ConfigError(ValueError):
    """Raised when environment configuration cannot be parsed."""


def _parse_priorities(raw: str) -> dict[IPv4Address, int]:
    """Parse ``NEIGHBOR_PRIORITIES`` such as ``192.0.2.1=10,192.0.2.2=20``.

    Lower numbers are preferred.  Ordering is only used to break ties between
    neighbors announcing the same prefix; it never overrides longest-prefix
    matching or confirmed-over-pending selection.
    """
    priorities: dict[IPv4Address, int] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        neighbor_raw, sep, priority_raw = item.partition("=")
        if not sep:
            raise ConfigError(
                f"invalid NEIGHBOR_PRIORITIES entry {item!r}; expected ip=priority"
            )
        try:
            neighbor = IPv4Address(neighbor_raw.strip())
            priority = int(priority_raw.strip())
        except ValueError as exc:
            raise ConfigError(
                f"invalid NEIGHBOR_PRIORITIES entry {item!r}: {exc}"
            ) from exc
        if neighbor in priorities:
            raise ConfigError(f"duplicate NEIGHBOR_PRIORITIES entry for {neighbor}")
        priorities[neighbor] = priority
    return priorities


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must be non-negative, got {value}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    neighbor_priorities: dict[IPv4Address, int]
    default_neighbor_priority: int
    grace_seconds: float
    demo_seed: bool

    @classmethod
    def from_env(cls) -> "Settings":
        priorities = _parse_priorities(
            os.environ.get(
                "NEIGHBOR_PRIORITIES",
                "192.0.2.1=10,192.0.2.2=20",
            )
        )
        return cls(
            neighbor_priorities=priorities,
            default_neighbor_priority=_env_int("DEFAULT_NEIGHBOR_PRIORITY", 100),
            grace_seconds=_env_float("RECONNECT_GRACE_SECONDS", 30.0),
            demo_seed=os.environ.get("DEMO_SEED", "0").lower() in {"1", "true", "yes"},
        )
