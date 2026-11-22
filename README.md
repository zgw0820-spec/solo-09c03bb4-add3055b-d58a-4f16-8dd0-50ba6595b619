# BGP Reconnect Route Visibility

After a BGP neighbour reconnects, the on-call engineer needs to know whether the
affected networks have already moved to the backup path. This FastAPI service
receives the reconnects, IPv4 announcements, withdrawals and End-of-RIB messages
relayed by the collector, keeps one shared routing table, and answers
longest-prefix lookups with next hop and a confirmed/pending flag.

## Delivery semantics

* A **reconnect** carries a strictly increasing `session_id`. It supersedes the
  old session. The neighbour's existing routes are **retained** and flagged
  **pending** for a grace window.
* **Announcements and withdrawals from the current session** take effect
  immediately, and the announced routes are confirmed.
* **Messages from a superseded (older) session are rejected** with HTTP 409 and no
  table change. A message that claims a newer session than the neighbour has
  established is rejected too, until the matching reconnect arrives.
* **Grace timers are bound to the session that armed them.** A reconnect that is
  itself superseded leaves a timer that cannot touch the table.
* Pending routes for one neighbour are removed **as one batch** when either the
  current session's **End-of-RIB** arrives or that reconnect's **grace period
  expires**. Both paths cancel the timer first, so the batch runs once.
* Cleanup is per neighbour: another neighbour's updates and its End-of-RIB never
  wait for this one's convergence.

### Lookup precedence

For each destination in a batch, all neighbours are considered against one
shared table revision, then the winner is chosen by:

1. **longest prefix match**;
2. among equal prefixes, **confirmed before pending**;
3. among equal prefixes and status, **lower neighbour priority number**
   (configured in `NEIGHBOR_PRIORITIES`, default 100);
4. final deterministic tie-break on neighbour address.

Destinations with no route are returned explicitly with `status: "not_found"`.

## Run with Docker Compose

```bash
docker compose up --build
```

* API + Swagger UI: <http://localhost:8000/docs>
* ReDoc: <http://localhost:8000/redoc>
* OpenAPI JSON: <http://localhost:8000/openapi.json>
* Health: <http://localhost:8000/healthz>

`DEMO_SEED=1` is set in `docker-compose.yml`, so the service starts with both
neighbours at session 1 and their routes confirmed — Swagger is demo-ready.

Environment variables (see `app/config.py`):

| Variable | Default (compose) | Meaning |
| --- | --- | --- |
| `NEIGHBOR_PRIORITIES` | `192.0.2.1=10,192.0.2.2=20` | `ip=priority`, lower wins ties |
| `DEFAULT_NEIGHBOR_PRIORITY` | `100` | Priority for unlisted neighbours |
| `RECONNECT_GRACE_SECONDS` | `20` | Default grace window |
| `DEMO_SEED` | `1` | Seed the two-neighbour baseline on startup |
| `HOST_PORT` | `8000` | Host port mapped to container port 8000 |

## Demo walkthrough in Swagger (`/docs`)

The two local samples under `samples/` are the collector payloads for the two
neighbours. `samples/neighbor-primary-192.0.2.1.json` contains the reconnect and
failover story; `samples/neighbor-backup-192.0.2.2.json` is the standing backup.

1. **`GET /v1/routes`** — both neighbours' routes are confirmed. The seed is
   equivalent to replaying the backup sample plus the primary's session-1 prefix
   of the primary sample.
2. **`POST /v1/lookup`** with body from `samples/lookup-destinations.json`:
   `203.0.113.10` and `198.51.100.10` resolve via the primary (`10.0.0.1`),
   `198.51.100.200` hits the more specific `/25`, and `192.0.2.77` is
   `not_found`.
3. **`POST /v1/neighbors/192.0.2.1/reconnect`** with
   `{"session_id": 2, "grace_seconds": 20}` — response reports
   `retained_pending_routes: 3`.
4. **`POST /v1/lookup`** again — the primary's retained routes are now
   **pending**, and at equal prefix length the confirmed backup route wins, so
   `203.0.113.10` and `198.51.100.10` already show `10.0.0.2` / `192.0.2.2`.
   The `/25` only the primary had is still `pending` until the guest session
   proves itself.
5. **`POST /v1/neighbors/192.0.2.1/announce`** with `session_id: 1` — returns
   **409 `stale_session`**; `GET /v1/routes` shows the table unchanged.
6. **`POST /v1/neighbors/192.0.2.1/announce`** with `session_id: 2` and
   `{"prefix": "203.0.113.0/24", "next_hop": "10.0.0.1"}` — confirmed
   immediately; lookups return to the primary for that prefix.
7. **`POST /v1/neighbors/192.0.2.1/end-of-rib`** with `{"session_id": 2}` —
   purges the remaining pending routes in one batch. The `/25` disappears and
   `198.51.100.200` falls back to the backup's `/24`.
8. **`POST /v1/events`** accepts the whole sample file at once and reports
   per-event outcomes (`applied` / `rejected`); the stale event is reported in
   place without aborting later events.

For a clean slate between runs, `POST /v1/admin/reset` clears all state.

Replaying both sample files after a reset reconstructs the same end state; this
is asserted by `tests/test_demo_flow.py`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/neighbors/{ip}/reconnect` | New session takes over, old routes pending |
| POST | `/v1/neighbors/{ip}/announce` | IPv4 announcements from current session |
| POST | `/v1/neighbors/{ip}/withdraw` | IPv4 withdrawals from current session |
| POST | `/v1/neighbors/{ip}/end-of-rib` | Batch-remove this neighbour's pending routes |
| POST | `/v1/events` | Ordered batch with per-event accept/reject |
| POST | `/v1/lookup` | Batch longest-prefix lookup, one shared revision |
| GET | `/v1/routes` | Table snapshot with pending flags |
| GET | `/v1/neighbors` | Session state and pending counts per neighbour |
| POST | `/v1/admin/reset` | Clear state (demo helper) |
| GET | `/healthz` | Liveness |

Error bodies are always `{"error": {"code", "message", "details"}}`. Relevant
codes: `stale_session`, `session_not_increasing`, `session_not_established`,
`unknown_neighbor`.

## Tests

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

`requirements.txt` pins the runtime; the tests additionally need `pytest` and
`httpx` (installed above). The suite covers longest-prefix selection, priority
tie-breaks, explicit not-found, pending retention, immediate announce/withdraw,
batch End-of-RIB cleanup, grace expiry, superseded-timer protection, stale/future
session rejection, per-neighbour independence and sample replay.

`TestClient` drives the app's real lifespan and event loop, so the grace-timer
tests exercise the same `asyncio` timer path used in production.
