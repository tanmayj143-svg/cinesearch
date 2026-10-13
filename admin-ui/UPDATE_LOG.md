# CineSearch WebUI + Admin API Update Log

Repo keeps every version as `.v<N>` files (never overwritten/deleted). Active files: `index.html`, `ingest.html`, `search-ui/index.html`, `api/main.py`.

## v7 (this update) — ingest page made real + bug hunt from "test every button"

### Root causes found while testing each button's backing path
1. **Admin script crashed on load**: `initTiers()` still referenced `getElementById('tier')` after the Ingest Submit panel was removed → null deref killed the whole script, so *no* button worked and nothing populated. Fixed: removed dead refs (`submitIngest` body, `saveSettings`), `initTiers` now guards + preselects the saved tier from `/api/admin/config`.
2. **`/api/admin/queue` returned 500**: `worker_state` row and `id_schemes` rows were missing after the clean-slate TRUNCATE (earlier TRUNCATE had omitted them / CASCADE wiped them). Fixed by re-inserting (`worker_state id=1`, default `film/FILM` scheme, counter reset to 1).
3. **Search UI never got the new theme**: earlier deploy pushed a file that still carried gold/Cinzel styles. Rewritten to the purple→pink gradient + Inter scheme; old file kept as `.v4`.
4. **Admin still had gold remnants** (`var(--gold)`, `var(--acc)`, `--amber`, Cinzel) which no longer resolve → fixed all to violet/pink equivalents.
5. **nginx stale upstream**: api container restart changed its IP → nginx held the old one → 502. `nginx -s reload` after api restarts (kept in mind for deploys).

### New/changed API (api/main.py — mounted live, auto-reloads)
- `POST /api/ingest`: **404** for missing files (was raw 500 crash), **400** when no ID scheme, new optional `file_code=` so the admin ingest page assigns per-file IDs (duplicate rejected **409**).
- `GET /api/admin/folders` — top-level library folders for the picker.
- `GET /api/admin/library_scan?folder=` — recursive eligible-video listing (mov/mp4/mkv/avi/mxf/m4v/webm/mpg/mpeg/ts), non-video skipped.
- `POST /api/admin/folder_cleanup?folder=` — flatten subfolders (DB `rel_path` kept in sync), delete non-video files, remove empty subdirs. Refuses library root (400) + missing folder (404).

### Ingest page (`admin/ingest.html`) rewritten to use real endpoints
Folder dropdown from the library, Scan Folder → table of eligible videos with auto-suggested IDs (prefix + `-{NNNN}`), per-file editable File ID, checkbox multi-select, Submit Selected → `/api/ingest` per file (rename `CODE_name.ext` on disk), Clean Up Folder button, "Back to Admin" link, purple/pink gradient + Inter theme.

### Verification (all via curl against 192.168.1.35:8787)
- Pages: `/`, `/admin/`, `/admin/ingest.html` → 200; served admin/search contain Inter+pink, zero gold (`c9a84c`).
- Buttons' backing endpoints: pause ✓ / resume ✓ / queue ✓ / config get+save ✓ / search_ui ✓ / folders ✓ / library_scan ✓.
- Full ingest round-trip with dummy files: scan found 2 videos recursively and skipped the .txt; explicit `file_code` assigned + renamed on disk; auto-code path works; cleanup flattened subfolder (1 moved), deleted the txt, synced DB rel_paths; bad guards return 400/404.
- Test fixtures purged afterwards — clean slate restored: files=0, queue=0, shots=0, counter_next=1, worker unpaused, sources dir empty.

## v6 — separate ingest page, removed admin ingest/config panels, purple theme + Inter (mock UI data)
## v5 — same as v6 first pass
## v4 — ingest page + theme swap (admin)
## v3 — search UI rewrite (htmx, mood filter, copy-ID)
## v2 — original gold/Cinzel admin + search
