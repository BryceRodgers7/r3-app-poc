# Upload phase — high-level plan

Status: **plan only, no code written** (drafted 2026-11-16).

Goal: after a game finishes, the operator launches a standalone Windows
uploader that sends every camera's MP4 (plus sidecar metadata) to the
InMotion Hosting shared server, where a future multi-tenant web app will let
users review a catalog of their games.

**Out of scope:** the web app UI (catalog browsing, video review). That is a
separate, later phase. This phase includes only the uploader and the small
server-side ingest API and database tables the uploader needs.

## Decisions made

| Topic | Decision |
|---|---|
| Hosting | InMotion Hosting **shared Power plan** ("unlimited" disk) |
| Upload order | **One file at a time**, strictly sequential |
| What is uploaded | The MP4 from **every camera**, plus `plays.json` and a manifest |
| Tenancy | Web app, database and storage are **multi-tenant** |
| Database | PostgreSQL, from InMotion or Supabase (recommendation below: Supabase) |
| Launch model | Operator launches the uploader **separately, after the game**; it is **not** part of the operator/referee app |

## Scope boundary

- **Upload phase:** standalone Windows uploader, server-side ingest API, and
  the minimum database tables (tenants, users, games, files).
- **Web app phase:** review and catalog UI built on those tables.

The upload phase cannot avoid the tenant and login foundation, because the
ingest API has to enforce tenant isolation from day one.

## What gets uploaded

The post-session processor already writes the upload set to
`sessions/session_NNN/processed/game_NNN/`:

- `<feed>.mp4` per camera (libx264, CRF 23)
- `plays.json`

Rules:

- Upload only `processed/`. Never upload the raw `.mkv` segments.
- Each game also gets a `manifest.json` containing the pre-match details
  (venue, Team A, Team B), session and game IDs, camera names, each file's
  size, SHA-256 and duration, and the `challenges.json` contents.
- The manifest is the contract with the future web app and catalog, so the two
  phases stay decoupled.
- **Web playback gap:** `long_form_export.py` does not appear to set
  `-movflags +faststart`. Without it, browsers cannot seek or start playback
  until the whole file has downloaded. Fix this in the export, because the
  shared server cannot remux (see "Shared-host limits").

## What changes on shared hosting

- No root access: no chroot, no daemons, no OS-level separation between
  tenants.
- **SFTP is ruled out for multi-tenant.** One SFTP login is one credential
  with write access to every tenant's files, so one compromised operator laptop
  would expose all tenants.
- All tenant isolation therefore lives in the application layer. The uploader
  never touches the database or storage directly. It talks only to an
  authenticated HTTPS API on the web server.
- **Terms of service:** InMotion's Web Hosting Terms prohibit using shared
  hosting for online file storage or archiving. Get InMotion support to confirm
  in writing that serving game video through the web app is acceptable. The
  design keeps storage swappable (the API decides the upload target), so files
  can move to object storage later without changing the uploader.

## Protocol, login and security

### Transport

- HTTPS only, port 443 (venue firewalls rarely block it).
- Chunked, resumable uploads in the style of the tus protocol. A tus server
  library exists for PHP, but verify it fits the Power plan before committing.
  Fallback: a thin custom offset-based chunk API.
- Chunks of roughly 8–32 MB so each request stays under shared-host PHP time
  and size limits. Final size comes from the U0 spike.
- The server appends chunks at a verified offset to a `.part` file. After a
  drop, the uploader asks the server for the current offset and resumes.
- On completion the server checks SHA-256, then renames the file atomically.

### Login

- The operator signs in with their web-app account (email + password, MFA if
  available).
- The uploader keeps a short-lived access token and a revocable refresh token
  in Windows Credential Manager. Nothing secret goes in `app_settings.toml`.
- The tenant is always derived server-side from the token. The client never
  supplies storage paths.

### Storage

- Files live outside the web root at
  `<root>/<tenant_id>/<game_id>/<opaque_id>.mp4`.
- Never use client-supplied filenames (no path traversal).
- Per-tenant quotas and a per-file size cap.
- Server-side validation: MP4 signature, size, hash. `ffprobe` is probably not
  available on shared hosting.

### Other hardening

- Rate-limited login and an audit log.
- Database credentials stay server-side in a private config; the client never
  sees them. Supabase service keys never reach the client.
- Tenant-isolation tests: one tenant must never read or write another's data.
- This is footage of athletes, possibly minors: access control and a retention
  policy matter.

## Database: recommend Supabase

InMotion's shared plans include PostgreSQL (cPanel + phpPgAdmin). Supabase is
still recommended because:

- Row Level Security enforces tenant isolation in the database itself.
- Auth and MFA are included, so login is not hand-rolled.
- Backups are included, and the video bytes stay on InMotion anyway (the
  database holds only metadata).
- Free-tier projects pause when idle (to be verified), so production needs a
  paid tier.

If InMotion Postgres is chosen instead, auth, tenant isolation and backups are
all hand-built.

Test in U0:

1. Whether shared PHP has the `pdo_pgsql` extension (no source found either way).
2. Whether the host allows outbound database ports. If not, the API can use
   Supabase's HTTPS REST interface instead.

## Uploader app (standalone, operator-launched)

- Separate entry point in this repo, not part of the operator/referee app. Can
  later be packaged as its own installer.
- Keeps its **own local upload ledger**. It only reads `processed/`, so it never
  writes to the recorder's `metadata.db`. (This replaces an earlier idea of
  extending `export_artifacts`.)
- Lists only finalized, post-processed sessions and games.
- **Strictly one file at a time.** Per game: all camera MP4s first, then
  `plays.json` and `manifest.json` last as the commit marker.
- Crash, power loss or dropped connection resumes from the last good offset.
  Failures retry with backoff. Finished files are skipped on re-run.
- **Idempotent:** game key is `(tenant, install, session_id, game_subdir)`, so
  re-running never duplicates a game.
- UI: sign in, pick games, then progress, speed, ETA and errors. Pause, Resume,
  Retry, and a summary when done.
- Sizing: at 20 Mbps upstream, 1 GB takes about 7 minutes. The picker should
  show total size and estimated time.
- Never delete local files until the server confirms the hash.
- Warn if the main recording app is still running (CPU, disk and network
  contention).

## Minimum server-side data model

Tenant-scoped throughout (Row Level Security if Supabase):

- tenants, users and memberships
- games (tenant, venue, teams, date, session ref, status)
- game_files (camera/feed name, size, SHA-256, storage path, status)
- upload sessions (resume state)

The full catalog schema belongs to the web app phase.

## Ingest API (high level)

- Login and token refresh
- Create game (idempotent)
- Start or resume a file upload (returns current offset)
- Append chunk
- Complete file (verify hash, mark ready)
- Complete game (accept manifest, write catalog rows)
- Status

## Shared-host limits that shape the design

- **No background workers.** The server cannot remux, transcode or make
  thumbnails. The export must produce web-ready MP4s (`+faststart`), and
  duration and a poster image are generated on the Windows side and sent in
  the manifest.
- PHP-FPM worker and CPU limits: sequential chunked uploads suit the host.
- ModSecurity may reject binary chunk bodies. Test early.
- Inode cap is unconfirmed by InMotion (a third-party review cites 50,000). A
  few large MP4s per game is fine.
- Multi-GB host backups are unlikely, so local copies are the real backup for
  now.
- SSH is on port 2222 per one InMotion support article, but it is not needed
  with this design.

## Milestones

| Step | Scope |
|---|---|
| U0 | **Spike on the real Power plan:** PHP upload/time/memory limits, `pdo_pgsql` and outbound DB access, ModSecurity with binary chunks, `ffprobe` availability. Get the terms-of-service confirmation. Pick the database. Define the minimum schema and manifest v1. |
| U1 | Server ingest API: auth, tenant-scoped tables, storage layout, chunk/offset/complete endpoints. |
| U2 | Uploader core with a test CLI: login, sequential queue, chunk resume, hashing. Tested against flaky-network conditions. |
| U3 | Desktop uploader UI. |
| U4 | Hardening: tenant-isolation tests, quotas, token revocation, large-file soak test. |
| U5 | Packaging and updates for the standalone uploader. |

## Open questions

1. Is Supabase OK for the database and login, versus InMotion Postgres with
   hand-built auth?
2. For v1, are tenants and users admin-provisioned, or is self-signup needed?
3. Will operators upload from the venue, or from home/office after the event
   (bandwidth)?

## Sources

- [InMotion Web Hosting Terms of Service](https://inmotionhosting.com/legal/?p=634)
- [InMotion PostgreSQL Hosting](https://www.inmotionhosting.com/postgresql-hosting)
- [InMotion support: SSH via PuTTY (port 2222)](https://www.inmotionhosting.com/support/wp-json/wp/v2/posts/2676)
