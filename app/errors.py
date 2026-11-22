"""Domain errors mapped to HTTP responses by the API layer."""

from __future__ import annotations

from ipaddress import IPv4Address


class ApiError(Exception):
    """Base class for errors that carry an explicit HTTP status."""

    status_code = 400
    code = "api_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnknownNeighbor(ApiError):
    status_code = 404
    code = "unknown_neighbor"

    def __init__(self, neighbor: IPv4Address) -> None:
        super().__init__(
            f"neighbor {neighbor} has no established BGP session; "
            "send a reconnect event first",
            neighbor=str(neighbor),
        )


class SessionConflict(ApiError):
    """A message belongs to a session that is not the neighbor's current one."""

    status_code = 409
    code = "session_conflict"

    def __init__(
        self,
        *,
        neighbor: IPv4Address,
        current_session: int,
        received_session: int,
        reason: str,
    ) -> None:
        if reason == "stale":
            code = "stale_session"
            message = (
                f"session {received_session} for neighbor {neighbor} is stale; "
                f"current session is {current_session}"
            )
        elif reason == "not_increasing":
            code = "session_not_increasing"
            message = (
                f"reconnect session {received_session} for neighbor {neighbor} "
                f"must be greater than current session {current_session}"
            )
        else:
            code = "session_not_established"
            message = (
                f"session {received_session} for neighbor {neighbor} has not been "
                f"established (current session is {current_session}); "
                "send the reconnect event first"
            )
        self.code = code
        self.reason = reason
        super().__init__(
            message,
            neighbor=str(neighbor),
            current_session=current_session,
            received_session=received_session,
            reason=reason,
        )
