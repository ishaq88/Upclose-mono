# Owner session service conventions

T03a locks the owning session before any request-key, part, upload or report write. Future upload initiation/completion must take that same session lock before part/upload rows so finalization cannot race a new upload. Queue code takes the session fence before job rows.

Session creation returns an opaque owner token once and stores only SHA-256. It deliberately ignores Idempotency-Key; a lost creation response requires a new session. It creates eight canonical pending parts and does not create a report. Part-1 processing must not auto-finalize or create a competing full report shell. Explicit finalization creates one shell and building revision; its manifest freezes selection only. The `claims` report stage (`app/pipeline/report_stages.py`) then freezes the hashed evidence snapshot (`app/services/snapshots.py`, `report-manifest-v1`); only `render` marks it ready, after which migration `c4e8a1f2b7d3` blocks snapshot mutation except during deletion.

Request-key rows retain safe response DTOs for the session lifetime. `expires_at` currently records a nominal timestamp for future cleanup planning; the replay service deliberately ignores it and no early expiration/purge runs. Session cleanup must remove these records at session end. Never store the creation token, signed URLs or checkout links in a replay DTO.

Source limits use PostgreSQL atomic upserts over SHA-256 source fingerprints and UTC daily windows, based on the database clock. The default limit is three sessions per source per day. `trusted_proxy_hosts` defaults empty and only accepts explicit IP networks; wildcard trust is rejected. Configure the HTTP server not to rewrite source addresses from untrusted forwarding headers. No source addresses, tokens or listing bodies are logged by these services.

Deleting/deleted owner sessions return 404 on content routes. A later delete endpoint may explicitly authenticate a tombstone for its own naturally idempotent status response; it must not reuse these content serializers to expose retained data.
