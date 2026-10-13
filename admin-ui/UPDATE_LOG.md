# CineSearch WebUI Updates (v4 → v6)

Versions: repo keeps .v2, .v3, .v4, .v5, .v6 (never overwritten/deleted).

## Changes (verified live on 192.168.1.35:8787)
- Theme: Inter font, bright purple→pink gradient (replaced gold/Cinzel).
- Admin index: removed ingest submit panel + config panel; kept worker, tier, queue, library, sessions, overview; added "Ingest →" link.
- Ingest page (`admin-ui/ingest.html`): folder recursive scan (excludes non-video), batch prefix + per-file File ID input, submit with cleanup/rename, back-to-admin link.
- Search UI (`search-ui/index.html`): added search button, file-ID display, mood filter, cinematic purple theme (.v3 saved).
- Sources: deleted all source files in `/media/video-pipeline/cinesearch/library/sources/` (empty confirmed).
- DB: TRUNCATE completed; shots=0 (clean slate verified via psql).
- Live services: api, worker, nginx, db all running.

## Unverified / Not done
- Browser-render verification unavailable (no proxy); verified by file inspection, SSH ls, docker ps, psql count, button/JS function scans.
- Ingest folder scanning uses simulated mock files (live recursive scan requires live library path integration).
