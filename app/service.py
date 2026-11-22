"""In-memory RIB used for reconnect and failover visibility.

The service is deliberately synchronous and single-threaded.  FastAPI runs the
mutating calls on the event loop, so every public call is atomic with respect
to other requests.  Grace-period expiry is driven by ``asyncio`` timer handles
that are bound to the session generation that armed them: a timer left over
from a superseded session can never change the table.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv4Network
from typing import Mapping

from .errors import SessionConflict, UnknownNeighbor


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Route:
    prefix: IPv4Network
    next_hop: IPv4Address
    neighbor: IPv4Address
    pending: bool
    announced_session: int
    updated_at: datetime


@dataclass
class NeighborState:
    neighbor: IPv4Address
    session_id: int
    generation: int
    established_at: datetime
    routes: dict[IPv4Network, Route] = field(default_factory=dict)
    end_of_rib_received: bool = False
    grace_expires_at: datetime | None = None
    timer: asyncio.TimerHandle | None = None
    last_cleanup_reason: str | None = None
    last_cleanup_at: datetime | None = None

    @property
    def pending_count(self) -> int:
        return sum(1 for route in self.routes.values() if route.pending)

    @property
    def confirmed_count(self) -> int:
        return sum(1 for route in self.routes.values() if not route.pending)

    @property
    def converging(self) -> bool:
        return self.grace_expires_at is not None and not self.end_of_rib_received


@dataclass(frozen=True)
class ReconnectResult:
    neighbor: IPv4Address
    previous_session_id: int | None
    session_id: int
    retained_pending_routes: int
    grace_seconds: float
    grace_expires_at: datetime | None
    revision: int


@dataclass(frozen=True)
class AnnounceResult:
    neighbor: IPv4Address
    session_id: int
    accepted: int
    added: int
    replaced: int
    route_count: int
    pending_remaining: int
    revision: int


@dataclass(frozen=True)
class WithdrawResult:
    neighbor: IPv4Address
    session_id: int
    removed: int
    not_present: tuple[IPv4Network, ...]
    route_count: int
    pending_remaining: int
    revision: int


@dataclass(frozen=True)
class EndOfRibResult:
    neighbor: IPv4Address
    session_id: int
    purged_pending: int
    pending_remaining: int
    confirmed_remaining: int
    revision: int


@dataclass(frozen=True)
class Match:
    destination: IPv4Address
    found: bool
    prefix: IPv4Network | None = None
    next_hop: IPv4Address | None = None
    neighbor: IPv4Address | None = None
    neighbor_priority: int | None = None
    pending: bool | None = None
    route_session_id: int | None = None


@dataclass(frozen=True)
class LookupResult:
    revision: int
    route_count: int
    matches: tuple[Match, ...]


@dataclass(frozen=True)
class NeighborView:
    neighbor: IPv4Address
    priority: int
    session_id: int
    established_at: datetime
    end_of_rib_received: bool
    grace_expires_at: datetime | None
    converging: bool
    pending_routes: int
    confirmed_routes: int
    total_routes: int
    last_cleanup_reason: str | None


class RouteService:
    def __init__(
        self,
        *,
        neighbor_priorities: Mapping[IPv4Address, int] | None = None,
        default_neighbor_priority: int = 100,
        grace_period_seconds: float = 30.0,
    ) -> None:
        if grace_period_seconds < 0 or not math.isfinite(grace_period_seconds):
            raise ValueError("grace_period_seconds must be a finite, non-negative number")
        self._priorities: dict[IPv4Address, int] = dict(neighbor_priorities or {})
        self._default_priority = default_neighbor_priority
        self._default_grace = grace_period_seconds
        self._neighbors: dict[IPv4Address, NeighborState] = {}
        self._revision = 0

    # ------------------------------------------------------------------ views
    @property
    def revision(self) -> int:
        return self._revision

    def priority_for(self, neighbor: IPv4Address) -> int:
        return self._priorities.get(neighbor, self._default_priority)

    def route_count(self) -> int:
        return sum(len(state.routes) for state in self._neighbors.values())

    def neighbors(self) -> tuple[NeighborView, ...]:
        views = [
            NeighborView(
                neighbor=state.neighbor,
                priority=self.priority_for(state.neighbor),
                session_id=state.session_id,
                established_at=state.established_at,
                end_of_rib_received=state.end_of_rib_received,
                grace_expires_at=state.grace_expires_at,
                converging=state.converging,
                pending_routes=state.pending_count,
                confirmed_routes=state.confirmed_count,
                total_routes=len(state.routes),
                last_cleanup_reason=state.last_cleanup_reason,
            )
            for state in self._neighbors.values()
        ]
        views.sort(key=lambda view: (view.priority, int(view.neighbor)))
        return tuple(views)

    # --------------------------------------------------------------- mutation
    def reconnect(
        self,
        neighbor: IPv4Address,
        session_id: int,
        *,
        grace_seconds: float | None = None,
        reason: str | None = None,
    ) -> ReconnectResult:
        """Take over the neighbor with a new, higher session id.

        Routes held for the previous session are kept but flagged pending.
        They are removed in one batch when the new session reaches End-of-RIB
        or when this reconnect's grace period expires.
        """
        del reason  # recorded by the caller/logging in a real deployment
        grace = self._default_grace if grace_seconds is None else grace_seconds
        if grace < 0 or not math.isfinite(grace):
            raise ValueError("grace_seconds must be a finite, non-negative number")

        now = _utcnow()
        state = self._neighbors.get(neighbor)
        previous_session: int | None = None

        if state is None:
            state = NeighborState(
                neighbor=neighbor,
                session_id=session_id,
                generation=0,
                established_at=now,
            )
            self._neighbors[neighbor] = state
        else:
            if session_id < state.session_id:
                raise SessionConflict(
                    neighbor=neighbor,
                    current_session=state.session_id,
                    received_session=session_id,
                    reason="stale",
                )
            if session_id == state.session_id:
                raise SessionConflict(
                    neighbor=neighbor,
                    current_session=state.session_id,
                    received_session=session_id,
                    reason="not_increasing",
                )
            previous_session = state.session_id
            if state.timer is not None:
                # The superseded timer must never touch the table.
                state.timer.cancel()
                state.timer = None
            # Every route from the old session stays visible, but pending.
            for route in state.routes.values():
                route.pending = True

            state.session_id = session_id
            state.generation += 1
            state.established_at = now
            state.end_of_rib_received = False
            state.last_cleanup_reason = None
            state.last_cleanup_at = None

        state.grace_expires_at = (
            now + timedelta(seconds=grace) if grace > 0 else None
        )
        self._revision += 1
        retained = state.pending_count

        if grace > 0:
            self._arm_timer(state, grace)
        else:
            # No grace window configured: settle immediately.
            self._purge_pending(state, reason="grace_expired")
            state.grace_expires_at = None

        return ReconnectResult(
            neighbor=neighbor,
            previous_session_id=previous_session,
            session_id=session_id,
            retained_pending_routes=retained,
            grace_seconds=grace,
            grace_expires_at=state.grace_expires_at,
            revision=self._revision,
        )

    def announce(
        self,
        neighbor: IPv4Address,
        session_id: int,
        routes: list[tuple[IPv4Network, IPv4Address]],
    ) -> AnnounceResult:
        """Apply IPv4 announcements from the current session immediately."""
        state = self._require_current(neighbor, session_id)
        now = _utcnow()
        added = replaced = 0
        for prefix, next_hop in routes:
            if prefix in state.routes:
                replaced += 1
            else:
                added += 1
            state.routes[prefix] = Route(
                prefix=prefix,
                next_hop=next_hop,
                neighbor=neighbor,
                pending=False,
                # Announcements from the current session are confirmed at once;
                # only routes carried over from a previous session stay pending.
                announced_session=session_id,
                updated_at=now,
            )
        self._revision += 1
        return AnnounceResult(
            neighbor=neighbor,
            session_id=session_id,
            accepted=len(routes),
            added=added,
            replaced=replaced,
            route_count=len(state.routes),
            pending_remaining=state.pending_count,
            revision=self._revision,
        )

    def withdraw(
        self,
        neighbor: IPv4Address,
        session_id: int,
        prefixes: list[IPv4Network],
    ) -> WithdrawResult:
        """Apply withdrawals from the current session immediately."""
        state = self._require_current(neighbor, session_id)
        removed = 0
        not_present: list[IPv4Network] = []
        for prefix in prefixes:
            if prefix in state.routes:
                del state.routes[prefix]
                removed += 1
            else:
                not_present.append(prefix)
        self._revision += 1
        return WithdrawResult(
            neighbor=neighbor,
            session_id=session_id,
            removed=removed,
            not_present=tuple(not_present),
            route_count=len(state.routes),
            pending_remaining=state.pending_count,
            revision=self._revision,
        )

    def end_of_rib(self, neighbor: IPv4Address, session_id: int) -> EndOfRibResult:
        """End-of-RIB for the current session: batch-remove pending routes."""
        state = self._require_current(neighbor, session_id)
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        purged = self._purge_pending(state, reason="end_of_rib")
        state.end_of_rib_received = True
        state.grace_expires_at = None
        self._revision += 1
        return EndOfRibResult(
            neighbor=neighbor,
            session_id=session_id,
            purged_pending=purged,
            pending_remaining=state.pending_count,
            confirmed_remaining=state.confirmed_count,
            revision=self._revision,
        )

    # ---------------------------------------------------------------- lookups
    def snapshot(self) -> tuple[Route, ...]:
        """All routes in a stable order, with their owning neighbor priority."""
        routes = [route for state in self._neighbors.values() for route in state.routes.values()]
        routes.sort(
            key=lambda route: (
                int(route.prefix.network_address),
                route.prefix.prefixlen,
                self.priority_for(route.neighbor),
                int(route.neighbor),
            )
        )
        return tuple(routes)

    def reset(self) -> tuple[int, int]:
        """Drop all neighbors and routes (demo convenience)."""
        for state in self._neighbors.values():
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
        cleared_neighbors = len(self._neighbors)
        cleared_routes = self.route_count()
        self._neighbors.clear()
        self._revision += 1
        return cleared_neighbors, cleared_routes

    def lookup(self, destinations: list[IPv4Address]) -> LookupResult:
        """Resolve a batch of destinations against one shared table snapshot."""
        revision = self._revision
        matches: list[Match] = []

        for destination in destinations:
            best: tuple[tuple[int, int, int, int], NeighborState, Route] | None = None
            for state in self._neighbors.values():
                priority = self.priority_for(state.neighbor)
                for route in state.routes.values():
                    if destination not in route.prefix:
                        continue
                    key = (
                        -route.prefix.prefixlen,          # longest match first
                        route.pending,                     # confirmed before pending
                        priority,                          # lower number = preferred
                        int(state.neighbor),               # deterministic final tie
                    )
                    if best is None or key < best[0]:
                        best = (key, state, route)

            if best is None:
                matches.append(
                    Match(destination=destination, found=False, pending=None)
                )
            else:
                _, state, route = best
                matches.append(
                    Match(
                        destination=destination,
                        found=True,
                        prefix=route.prefix,
                        next_hop=route.next_hop,
                        neighbor=state.neighbor,
                        neighbor_priority=self.priority_for(state.neighbor),
                        pending=route.pending,
                        route_session_id=route.announced_session,
                    )
                )

        return LookupResult(
            revision=revision,
            route_count=self.route_count(),
            matches=tuple(matches),
        )

    # ---------------------------------------------------------------- helpers
    def _require_current(
        self, neighbor: IPv4Address, session_id: int
    ) -> NeighborState:
        state = self._neighbors.get(neighbor)
        if state is None:
            raise UnknownNeighbor(neighbor)
        if session_id < state.session_id:
            raise SessionConflict(
                neighbor=neighbor,
                current_session=state.session_id,
                received_session=session_id,
                reason="stale",
            )
        if session_id > state.session_id:
            raise SessionConflict(
                neighbor=neighbor,
                current_session=state.session_id,
                received_session=session_id,
                reason="not_established",
            )
        return state

    def _purge_pending(self, state: NeighborState, *, reason: str) -> int:
        """Remove every pending route of one neighbor as a single batch."""
        doomed = [prefix for prefix, route in state.routes.items() if route.pending]
        for prefix in doomed:
            del state.routes[prefix]
        state.last_cleanup_reason = reason
        state.last_cleanup_at = _utcnow()
        return len(doomed)

    def _arm_timer(self, state: NeighborState, grace: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (unit tests, scripted demo): expiry is driven
            # explicitly by end_of_rib() or a later reconnect.
            state.timer = None
            return
        state.timer = loop.call_later(
            grace,
            self._on_grace_expired,
            state.neighbor,
            state.session_id,
            state.generation,
        )

    def _on_grace_expired(
        self, neighbor: IPv4Address, session_id: int, generation: int
    ) -> None:
        state = self._neighbors.get(neighbor)
        if state is None:
            return
        # Stale timer from a superseded session/generation: no table changes.
        if state.session_id != session_id or state.generation != generation:
            return
        if state.end_of_rib_received or state.grace_expires_at is None:
            return
        state.timer = None
        self._purge_pending(state, reason="grace_expired")
        state.grace_expires_at = None
        self._revision += 1

    def close(self) -> None:
        """Cancel outstanding timers (application shutdown)."""
        for state in self._neighbors.values():
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
