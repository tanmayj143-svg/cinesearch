# CineSearch — UPDATE LOG

## v12 — Power cap, job stagger, condensed status panel, IST time (2026-10-14)
- **GPU power cap from the WebUI:** Ingest page slider (100–170 W, factory default 170) writes `power_limit_w` to the shared config; the telemetry sidecar (SYS_ADMIN) applies `nvidia-smi -pl` within ~1 s even mid-stage. Worker also applies it between jobs.
- **Measured data points (RTX 3060, VLM stage, live benchmarks):**
  | Cap | Temp | Fan | VLM shots/min | ≈ per 2 h film |
  |---|---|---|---|---|
  | 170 W | 78 °C | 77 % | 13.0 | ~4.7 h |
  | 150 W | 75 °C | 72 % | 11.5 | ~5.2 h |
  | 120 W | 67 °C | 70 % | 12.8 | ~4.8 h |
  | 100 W | 61 °C | 58 % | 9.5 (est.) | ~6.4 h |
  VLM inference is memory-bound → 120 W costs ~2 % throughput for 11 °C less heat. Batch ETA line on the ingest page multiplies pending films × per-film cost + cooldowns.
- **Job stagger:** `inter_job_sleep_s` (0–1 h) — worker pauses N seconds after each finished film (shows `cooldown Ns` in the worker pill).
- **Telemetry sidecar as its own service** (`cinesearch-telemetry`): 1 s loop, computes per-step progress, **step ETA** and **whole-job ETA** from measured shot rates (shotvl 3.3 s/shot, vlm 3.8 s/shot, ~17 m fixed tail), and never overwrites the worker's own `current_model` outside shotvl/vlm.
- **Admin UI redesign:**
  - Condensed the four status tiles + separate failed tile into ONE panel: active film (`FILM-0001 · processing · step 5/10 · vlm · 1207/2554`) above counts (pending / processed / failed / shots / utterances / scenes). Positioned above GPU telemetry.
  - Job queue ETA is now **whole-job**; details inspector adds step progress + step ETA + job ETA side by side.
  - Pipeline journey cards each show a Step ETA row while running.
  - Layout jitter eliminated: fixed colgroup widths, tabular numerals, ellipsis, min-heights on cards and status panel; "Stage Progress" no longer repeats ETA text.
  - Shot-level `parse shot N` records are `warning` (amber), never `failed` — DB history rewritten and worker logging fixed.
- **Time:** Postgres DB default timezone set to `Asia/Kolkata`; all timestamps now render IST (+05:30).
- **Config whitelist** extended: `power_limit_w`, `inter_job_sleep_s`, `models` (per-model overrides now honored by `worker/config.load()`).

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
