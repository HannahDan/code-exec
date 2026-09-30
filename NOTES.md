# NOTES — Code Annotation Runner

## M1 Decisions

- **IDs**: `uuid4().hex[:16]` — 16 hex chars, collision-safe for this scale.
- **DB**: stdlib `sqlite3` with WAL mode, single connection per call, `asyncio.to_thread` for async compat.
- **Timestamps**: ISO 8601 UTC via `datetime.now(timezone.utc).isoformat()`.
- **K8s healthcheck**: uses `kubernetes.client.VersionApi().get_code()` to verify cluster reachability.
- **Static files**: served via `FileResponse` for `GET /`, no framework build step.
