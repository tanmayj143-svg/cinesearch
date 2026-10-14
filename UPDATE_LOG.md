# CineSearch — UPDATE LOG

## v9 — Ingest UI rework, live telemetry, and pipeline stability (2026-10-14)
- **Ingest UI Rework:**
  - Category is now a free text box (batch header + per-row text box) with auto-registration into taxonomy table.
  - Category acts as a prefix: renaming on ingest completely replaces original filename with `[category]_[id].[ext]` (or `[id].[ext]` if category is blank).
  - File ID text boxes start blank by default; optional "Auto-number IDs" button populates basic numbering (`1`, `2`, `3`...).
  - Added live "Final File Name" preview column with nowrap formatting and balanced column widths.
  - Removed Step 3 (Review & Submit panel); placed direct "Submit Batch" button beneath the file table.
  - Global `/media` scanning: scans root files and subdirectories indiscriminately or filters by folder.
- **Queue & Telemetry Inspector:**
  - Added ETA column to the Jobs table.
  - Added "Details ▼" inspector button to each job row displaying live stage breakdown, shot progress bar, GPU snapshot, and event log.
  - Telemetry update frequency increased to 1 second via dedicated in-container daemon (`telemetry_updater.py`).
  - Fixed missing `power_limit_w` and populated GPU telemetry directly from worker state in `/api/admin/stats` and `/api/admin/gpu`.
  - Added `Cache-Control: no-cache` header in Nginx for `/admin/`.
- **Worker & Pipeline Robustness:**
  - Fixed `KeyError: 0` in `st_shotvl` and `st_vlm` sanity checks caused by `dict_row` row factory (`["count"]` instead of `[0]`).
  - Added shot-level resumption queries (`WHERE camera_movement IS NULL` and `WHERE description IS NULL`) so interrupted stages resume without recomputing.
  - Bounded ShotVL frame sampling (`<= 16` frames) to prevent OOM on long shots.
  - Chunked narrative scene segmentation prompt into 200-shot windows to prevent LLM context overflow.
  - Accelerated vision inference with `torch.inference_mode()` and explicit garbage collection on cache release.

## v8 — /media as library root, category labels, GPU panel, stage detail (2026-10-14)

### Model change: library root IS /media
- `LIBRARY_ROOT` for api+worker is now `/media` (bind: host `/media/video-pipeline/cinesearch/media`). The separate `sources/` tree and the media→library move step are gone — files are ingested **in place** under `/media/<folder>/`, renamed to `CODE_name.ext`.
- nginx `/derived/` alias now serves from `/media/derived/`.
- Trial file `EP-TEST-0001` and its derived artifacts removed (DB was already at 0 rows).

### Category / prefix labels
- `files.category` column added (live-migrated); `id_schemes.category` default per scheme.
- `/api/ingest` accepts `category=`; batch ingest inherits scheme default otherwise.
- Search: `category` filter param + `/api/filters/category`; category pill rendered on result cards; library table shows category and the filter matches it.
- `taxonomy` gains a `category` axis (interview, b_roll, a_roll, drone, stock, music_video, screen_capture, test) — this is the ONE canonical list: the ingest label pickers (batch + per-row) and the search filter are all populated from it, and `/api/ingest` **rejects** any category not on the list (400). No free-form labels.

### API
- `GET /api/admin/gpu` reads telemetry the **worker** writes to `worker_state.gpu_json` every loop/stage (nvidia-smi in the GPU container; api container has no GPU): name, VRAM total/used/free, util, temp, power.
- `GET /api/admin/folders` — folders of /media with recursive video counts (replaces library-root listing).
- `GET /api/admin/library_scan?folder=` — scans /media subfolders.
- `POST /api/admin/media/ingest_folder` — one-call batch ingest of a whole /media folder (atomic per-file codes under FOR UPDATE lock, rename in place, empty subdirs pruned).
- `POST /api/admin/folder_cleanup` — retargeted at /media folders (dry-run default, _trash preserve).
- Removed: `/api/admin/media` + `/api/admin/media/pick` (the media-drop panel they served is gone).

### UIs
- **Ingest:** media-drop panel removed. Step 1 = /media folder picker (with video counts); Step 2 = scanned files with a **batch prefix/category input (datalist from existing categories) + "Apply to all"** and a **per-row editable label**; Step 3 = review + per-file progress.
- **Queue:** new GPU panel (VRAM bar, utilization, temperature, power, active model — 5s poll). Jobs table now shows **step N/10 · stage (model)** for active jobs and "failed at <stage>" for failures.
- **Search:** Category filter in the rail; category pills on cards.

## v7 — correctness + security + UI rebuild (2026-10-14)
(see v7/UPDATE_LOG.md)

## v6 — separate ingest page, removed admin ingest/config panels, purple theme + Inter
## v5 — same as v6 first pass
## v4 — ingest page + theme swap (admin)
## v3 — search UI rewrite (htmx, mood filter, copy-ID)
## v2 — original gold/Cinzel admin + search
