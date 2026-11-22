"""Request/response schemas for the collector-facing API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from ipaddress import IPv4Address, IPv4Network
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class RouteAnnouncement(BaseModel):
    """One IPv4 NLRI plus its next hop, as forwarded by the collector."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"}
        }
    )

    prefix: IPv4Network = Field(
        description="IPv4 prefix in CIDR form. Host bits must be zero."
    )
    next_hop: IPv4Address = Field(description="BGP next hop for this prefix.")


class ReconnectRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": 2,
                "grace_seconds": 10,
                "reason": "BGP session reset by peer",
            }
        }
    )

    session_id: int = Field(
        ge=1,
        description="New session number. Must be greater than the neighbor's "
        "current session number.",
    )
    grace_seconds: float | None = Field(
        default=None,
        ge=0,
        le=86400,
        description="Grace window before retained routes are discarded. "
        "Defaults to the service configuration.",
    )
    reason: str | None = Field(
        default=None, max_length=200, description="Free-form reason for the audit log."
    )


class AnnouncementRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": 2,
                "routes": [
                    {"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"},
                    {"prefix": "198.51.100.0/24", "next_hop": "10.0.0.1"},
                ],
            }
        }
    )

    session_id: int = Field(ge=1, description="Session that originated this UPDATE.")
    routes: list[RouteAnnouncement] = Field(
        min_length=1, max_length=10000, description="IPv4 announcements to apply."
    )


class WithdrawalRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "session_id": 2,
                "prefixes": ["198.51.100.0/24"],
            }
        }
    )

    session_id: int = Field(ge=1, description="Session that originated this UPDATE.")
    prefixes: list[IPv4Network] = Field(
        min_length=1,
        max_length=10000,
        description="IPv4 prefixes to withdraw. Host bits must be zero.",
    )


class EndOfRibRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"session_id": 2}})

    session_id: int = Field(
        ge=1, description="Session whose initial routing update is complete."
    )


class LookupRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "destinations": [
                    "203.0.113.10",
                    "203.0.113.200",
                    "198.51.100.7",
                    "192.0.2.250",
                ]
            }
        }
    )

    destinations: list[IPv4Address] = Field(
        min_length=1,
        max_length=10000,
        description="Destination IPv4 addresses. All are resolved against one "
        "shared table revision.",
    )


# ---------------------------------------------------------------- event stream
class ReconnectEvent(ReconnectRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "reconnect",
                "neighbor": "192.0.2.1",
                "session_id": 2,
                "grace_seconds": 10,
                "reason": "BGP session reset by peer",
            }
        }
    )

    type: Literal["reconnect"]
    neighbor: IPv4Address


class AnnouncementEvent(AnnouncementRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "announce",
                "neighbor": "192.0.2.2",
                "session_id": 1,
                "routes": [
                    {"prefix": "203.0.113.0/24", "next_hop": "10.0.0.2"}
                ],
            }
        }
    )

    type: Literal["announce"]
    neighbor: IPv4Address


class WithdrawalEvent(WithdrawalRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "withdraw",
                "neighbor": "192.0.2.1",
                "session_id": 2,
                "prefixes": ["198.51.100.0/24"],
            }
        }
    )

    type: Literal["withdraw"]
    neighbor: IPv4Address


class EndOfRibEvent(EndOfRibRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": "end_of_rib",
                "neighbor": "192.0.2.1",
                "session_id": 2,
            }
        }
    )

    type: Literal["end_of_rib"]
    neighbor: IPv4Address


Event = Annotated[
    Union[ReconnectEvent, AnnouncementEvent, WithdrawalEvent, EndOfRibEvent],
    Field(discriminator="type"),
]


class EventBatchRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "type": "reconnect",
                        "neighbor": "192.0.2.1",
                        "session_id": 2,
                        "grace_seconds": 10,
                    },
                    {
                        "type": "announce",
                        "neighbor": "192.0.2.2",
                        "session_id": 1,
                        "routes": [
                            {
                                "prefix": "203.0.113.0/24",
                                "next_hop": "10.0.0.2",
                            }
                        ],
                    },
                    {
                        "type": "end_of_rib",
                        "neighbor": "192.0.2.1",
                        "session_id": 2,
                    },
                ]
            }
        }
    )

    events: list[Event] = Field(
        min_length=1,
        max_length=1000,
        description="Events are applied in order. A rejected event (for example "
        "from a superseded session) does not abort later events.",
    )


# --------------------------------------------------------------- response views
class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReconnectResponse(ResponseModel):
    neighbor: IPv4Address
    previous_session_id: int | None
    session_id: int
    retained_pending_routes: int
    grace_seconds: float
    grace_expires_at: datetime | None
    revision: int


class AnnouncementResponse(ResponseModel):
    neighbor: IPv4Address
    session_id: int
    accepted: int
    added: int
    replaced: int
    route_count: int
    pending_remaining: int
    revision: int


class WithdrawalResponse(ResponseModel):
    neighbor: IPv4Address
    session_id: int
    removed: int
    not_present: list[IPv4Network]
    route_count: int
    pending_remaining: int
    revision: int


class EndOfRibResponse(ResponseModel):
    neighbor: IPv4Address
    session_id: int
    purged_pending: int
    pending_remaining: int
    confirmed_remaining: int
    revision: int


class EventOutcome(BaseModel):
    index: int
    type: str
    neighbor: IPv4Address
    accepted: bool
    result: (
        ReconnectResponse
        | AnnouncementResponse
        | WithdrawalResponse
        | EndOfRibResponse
        | None
    ) = None
    error: dict[str, object] | None = None


class EventBatchResponse(ResponseModel):
    applied: int
    rejected: int
    revision: int
    outcomes: list[EventOutcome]


class RouteStatus(str, Enum):
    confirmed = "confirmed"
    pending = "pending"
    not_found = "not_found"


class MatchView(ResponseModel):
    destination: IPv4Address
    found: bool
    status: RouteStatus
    prefix: IPv4Network | None = None
    next_hop: IPv4Address | None = None
    neighbor: IPv4Address | None = None
    neighbor_priority: int | None = None
    pending: bool | None = None
    route_session_id: int | None = None


class LookupResponse(ResponseModel):
    revision: int
    route_count: int
    found_count: int
    not_found_count: int
    matches: list[MatchView]


class RouteView(ResponseModel):
    neighbor: IPv4Address
    neighbor_priority: int
    prefix: IPv4Network
    next_hop: IPv4Address
    pending: bool
    announced_session: int
    updated_at: datetime


class TableResponse(ResponseModel):
    revision: int
    route_count: int
    pending_count: int
    grace_seconds: float
    routes: list[RouteView]


class NeighborView(ResponseModel):
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


class NeighborsResponse(ResponseModel):
    revision: int
    neighbors: list[NeighborView]


class ResetResponse(ResponseModel):
    cleared_neighbors: int
    cleared_routes: int
    revision: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "stale_session",
                    "message": "session 1 for neighbor 192.0.2.1 is stale; "
                    "current session is 2",
                    "details": {
                        "neighbor": "192.0.2.1",
                        "current_session": 2,
                        "received_session": 1,
                        "reason": "stale",
                    },
                }
            }
        }
    )

    error: ErrorDetail
