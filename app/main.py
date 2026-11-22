"""Application factory and HTTP routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import IPv4Address

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .errors import ApiError
from .models import (
    AnnouncementEvent,
    AnnouncementRequest,
    AnnouncementResponse,
    EndOfRibEvent,
    EndOfRibRequest,
    EndOfRibResponse,
    ErrorEnvelope,
    EventBatchRequest,
    EventBatchResponse,
    EventOutcome,
    LookupRequest,
    LookupResponse,
    MatchView,
    NeighborView,
    NeighborsResponse,
    ReconnectEvent,
    ReconnectRequest,
    ReconnectResponse,
    ResetResponse,
    RouteStatus,
    RouteView,
    TableResponse,
    WithdrawalEvent,
    WithdrawalRequest,
    WithdrawalResponse,
)
from .service import (
    AnnounceResult,
    EndOfRibResult,
    ReconnectResult,
    RouteService,
    WithdrawResult,
)

DESCRIPTION = """
Route-visibility service for on-call staff after a BGP neighbor reconnects.

* A reconnect (a strictly increasing `session_id`) takes over the neighbor's
  session. The neighbour's existing routes are **retained** and flagged
  **pending** until the new session proves itself.
* Announcements and withdrawals from the **current** session take effect
  immediately.
* Messages from a **superseded** session, and timers armed by a superseded
  reconnect, never modify the table.
* Pending routes for one neighbour are removed **as a batch** when either its
  current session sends End-of-RIB or that reconnect's grace period expires.
  Other neighbours are never blocked by this cleanup.
* `POST /v1/lookup` resolves a batch of destinations against one shared table:
  longest-prefix match, then confirmed-over-pending, then neighbour priority.
  Destinations without a route are reported explicitly as `not_found`.
"""


def _error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorEnvelope.model_validate(
            {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        ).model_dump(mode="json"),
    )


def _reconnect_response(result: ReconnectResult) -> ReconnectResponse:
    return ReconnectResponse(
        neighbor=result.neighbor,
        previous_session_id=result.previous_session_id,
        session_id=result.session_id,
        retained_pending_routes=result.retained_pending_routes,
        grace_seconds=result.grace_seconds,
        grace_expires_at=result.grace_expires_at,
        revision=result.revision,
    )


def _announce_response(result: AnnounceResult) -> AnnouncementResponse:
    return AnnouncementResponse(
        neighbor=result.neighbor,
        session_id=result.session_id,
        accepted=result.accepted,
        added=result.added,
        replaced=result.replaced,
        route_count=result.route_count,
        pending_remaining=result.pending_remaining,
        revision=result.revision,
    )


def _withdraw_response(result: WithdrawResult) -> WithdrawalResponse:
    return WithdrawalResponse(
        neighbor=result.neighbor,
        session_id=result.session_id,
        removed=result.removed,
        not_present=list(result.not_present),
        route_count=result.route_count,
        pending_remaining=result.pending_remaining,
        revision=result.revision,
    )


def _end_of_rib_response(result: EndOfRibResult) -> EndOfRibResponse:
    return EndOfRibResponse(
        neighbor=result.neighbor,
        session_id=result.session_id,
        purged_pending=result.purged_pending,
        pending_remaining=result.pending_remaining,
        confirmed_remaining=result.confirmed_remaining,
        revision=result.revision,
    )


def _neighbor_view(view) -> NeighborView:
    return NeighborView(
        neighbor=view.neighbor,
        priority=view.priority,
        session_id=view.session_id,
        established_at=view.established_at,
        end_of_rib_received=view.end_of_rib_received,
        grace_expires_at=view.grace_expires_at,
        converging=view.converging,
        pending_routes=view.pending_routes,
        confirmed_routes=view.confirmed_routes,
        total_routes=view.total_routes,
        last_cleanup_reason=view.last_cleanup_reason,
    )


def create_app(
    *,
    settings: Settings | None = None,
    service: RouteService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or RouteService(
        neighbor_priorities=settings.neighbor_priorities,
        default_neighbor_priority=settings.default_neighbor_priority,
        grace_period_seconds=settings.grace_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.demo_seed:
            from .demo import seed

            seed(service)
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="BGP Reconnect Route Visibility",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "events", "description": "Collector events, single or batched."},
            {"name": "lookup", "description": "Batch destination lookup."},
            {"name": "admin", "description": "Table inspection and demo reset."},
        ],
    )
    app.state.service = service
    app.state.settings = settings

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc)

    # ------------------------------------------------------------- mutating
    @app.post(
        "/v1/neighbors/{neighbor}/reconnect",
        response_model=ReconnectResponse,
        status_code=200,
        tags=["events"],
        summary="Neighbour reconnected with a new session",
        responses={409: {"model": ErrorEnvelope}},
    )
    async def reconnect(
        neighbor: IPv4Address, body: ReconnectRequest
    ) -> ReconnectResponse:
        result = service.reconnect(
            neighbor,
            body.session_id,
            grace_seconds=body.grace_seconds,
            reason=body.reason,
        )
        return _reconnect_response(result)

    @app.post(
        "/v1/neighbors/{neighbor}/announce",
        response_model=AnnouncementResponse,
        tags=["events"],
        summary="Apply IPv4 announcements from the current session",
        responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
    )
    async def announce(
        neighbor: IPv4Address, body: AnnouncementRequest
    ) -> AnnouncementResponse:
        result = service.announce(
            neighbor,
            body.session_id,
            [(route.prefix, route.next_hop) for route in body.routes],
        )
        return _announce_response(result)

    @app.post(
        "/v1/neighbors/{neighbor}/withdraw",
        response_model=WithdrawalResponse,
        tags=["events"],
        summary="Apply IPv4 withdrawals from the current session",
        responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
    )
    async def withdraw(
        neighbor: IPv4Address, body: WithdrawalRequest
    ) -> WithdrawalResponse:
        result = service.withdraw(neighbor, body.session_id, list(body.prefixes))
        return _withdraw_response(result)

    @app.post(
        "/v1/neighbors/{neighbor}/end-of-rib",
        response_model=EndOfRibResponse,
        tags=["events"],
        summary="Current session initial update complete; drop pending routes",
        responses={404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}},
    )
    async def end_of_rib(
        neighbor: IPv4Address, body: EndOfRibRequest
    ) -> EndOfRibResponse:
        result = service.end_of_rib(neighbor, body.session_id)
        return _end_of_rib_response(result)

    # ----------------------------------------------------------- batch event
    @app.post(
        "/v1/events",
        response_model=EventBatchResponse,
        tags=["events"],
        summary="Apply a batch of collector events in order",
        description="A rejected event (stale session, unknown neighbour) is "
        "reported in place and does not abort the remaining events.",
    )
    async def events(body: EventBatchRequest) -> EventBatchResponse:
        outcomes: list[EventOutcome] = []
        applied = rejected = 0
        for index, event in enumerate(body.events):
            try:
                if isinstance(event, ReconnectEvent):
                    outcome = _reconnect_response(
                        service.reconnect(
                            event.neighbor,
                            event.session_id,
                            grace_seconds=event.grace_seconds,
                            reason=event.reason,
                        )
                    )
                elif isinstance(event, AnnouncementEvent):
                    outcome = _announce_response(
                        service.announce(
                            event.neighbor,
                            event.session_id,
                            [(r.prefix, r.next_hop) for r in event.routes],
                        )
                    )
                elif isinstance(event, WithdrawalEvent):
                    outcome = _withdraw_response(
                        service.withdraw(
                            event.neighbor, event.session_id, list(event.prefixes)
                        )
                    )
                else:
                    assert isinstance(event, EndOfRibEvent)
                    outcome = _end_of_rib_response(
                        service.end_of_rib(event.neighbor, event.session_id)
                    )
            except ApiError as exc:
                rejected += 1
                outcomes.append(
                    EventOutcome(
                        index=index,
                        type=event.type,
                        neighbor=event.neighbor,
                        accepted=False,
                        error={
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                        },
                    )
                )
            else:
                applied += 1
                outcomes.append(
                    EventOutcome(
                        index=index,
                        type=event.type,
                        neighbor=event.neighbor,
                        accepted=True,
                        result=outcome,
                    )
                )
        return EventBatchResponse(
            applied=applied,
            rejected=rejected,
            revision=service.revision,
            outcomes=outcomes,
        )

    # ---------------------------------------------------------------- lookup
    @app.post(
        "/v1/lookup",
        response_model=LookupResponse,
        tags=["lookup"],
        summary="Resolve destinations with longest-prefix match",
        description="One shared table snapshot per request. Selection order: "
        "longest prefix, then confirmed over pending, then neighbour priority. "
        "Unrouted destinations are returned with status `not_found`.",
    )
    async def lookup(body: LookupRequest) -> LookupResponse:
        result = service.lookup(list(body.destinations))
        matches = [
            MatchView(
                destination=match.destination,
                found=match.found,
                status=(
                    RouteStatus.not_found
                    if not match.found
                    else RouteStatus.pending
                    if match.pending
                    else RouteStatus.confirmed
                ),
                prefix=match.prefix,
                next_hop=match.next_hop,
                neighbor=match.neighbor,
                neighbor_priority=match.neighbor_priority,
                pending=match.pending,
                route_session_id=match.route_session_id,
            )
            for match in result.matches
        ]
        found = sum(1 for match in matches if match.found)
        return LookupResponse(
            revision=result.revision,
            route_count=result.route_count,
            found_count=found,
            not_found_count=len(matches) - found,
            matches=matches,
        )

    # ----------------------------------------------------------------- admin
    @app.get(
        "/v1/neighbors",
        response_model=NeighborsResponse,
        tags=["admin"],
        summary="Neighbour session state and pending counts",
    )
    async def list_neighbors() -> NeighborsResponse:
        return NeighborsResponse(
            revision=service.revision,
            neighbors=[_neighbor_view(view) for view in service.neighbors()],
        )

    @app.get(
        "/v1/routes",
        response_model=TableResponse,
        tags=["admin"],
        summary="Current routing table snapshot",
    )
    async def list_routes() -> TableResponse:
        routes = service.snapshot()
        return TableResponse(
            revision=service.revision,
            route_count=len(routes),
            pending_count=sum(1 for route in routes if route.pending),
            grace_seconds=settings.grace_seconds,
            routes=[
                RouteView(
                    neighbor=route.neighbor,
                    neighbor_priority=service.priority_for(route.neighbor),
                    prefix=route.prefix,
                    next_hop=route.next_hop,
                    pending=route.pending,
                    announced_session=route.announced_session,
                    updated_at=route.updated_at,
                )
                for route in routes
            ],
        )

    @app.post(
        "/v1/admin/reset",
        response_model=ResetResponse,
        tags=["admin"],
        summary="Clear all neighbours and routes (demo helper)",
    )
    async def reset() -> ResetResponse:
        cleared_neighbors, cleared_routes = service.reset()
        return ResetResponse(
            cleared_neighbors=cleared_neighbors,
            cleared_routes=cleared_routes,
            revision=service.revision,
        )

    @app.get("/healthz", tags=["admin"], summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
