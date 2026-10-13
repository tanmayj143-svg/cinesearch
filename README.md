# CineSearch

Searchable cinematic database for an editing workflow: natural-language + structured
search over shots, dialogue, camera language, and narrative beats — with a client
web UI and an admin console.

> ⚠️ **AI-GENERATED CODE — READ THIS FIRST**
> This entire project (application code, schema, UI, and documentation) was written
> by an **AI assistant** (Hermes Agent, by Nous Research) under human direction. It
> is provided **as-is, with no warranty of any kind**. It has been deployed and
> exercised end-to-end by its operator, but it has **not** been independently
> audited, security-reviewed, or certified. Review it before trusting it with
> credentials, network exposure, or unattended automation. See
> [`DISCLAIMER.md`](DISCLAIMER.md) for the full statement.

## Architecture

Single machine (192.168.1.35, RTX 3060 12GB). Everything in one compose file.
All models run **in-process in the worker**, sequentially (guaranteed single-model
GPU residency on 12GB — no vLLM sidecars, no giant images).

| Service | Role                                                  | GPU |
|---------|-------------------------------------------------------|-----|
| db      | Postgres 16 + pgvector (vectors + full-text + relational) | — |
| api     | FastAPI: hybrid search, ingest submit, exports, admin | CPU |
| worker  | Ingest pipeline (10 stages, one GPU model at a time)  | yes |
| nginx   | :8787 — search UI, /admin/, /derived/ media (HLS)     | —   |

Models (worker, loaded/unloaded per stage):
whisper (faster-whisper large-v3, int8) → ShotVL-3B (bf16, frame strips) →
Qwen2.5-VL-7B (4-bit, keyframes) → CLAP (audio vectors) → BGE-M3 (text vectors, CPU) →
Qwen2.5-7B (4-bit, narrative pass).

Data model: shots (visual atom, PySceneDetect) × utterances (audio atom, Whisper)
joined by time overlap (`utterance_shots`); scenes from the narrative LLM pass;
definitive file IDs (`FILM-0042`) assigned at ingest, embedded in filenames.

## Deploy (on 192.168.1.35)

```bash
cp .env.example .env            # LIBRARY_ROOT + HF_CACHE, both MUST be on /dev/sdb
bash scripts/preflight.sh       # verifies GPU-in-Docker + sdb + dirs
docker compose up -d            # db, api, worker, nginx
```

Web UIs: `http://192.168.1.35:8787` (search), `http://192.168.1.35:8787/admin/` (admin).

First ingest downloads ~45 GB of model weights into `$HF_CACHE` (on sdb — never the
root LV). Subsequent runs load from cache.

## Ingest

Drop a file into `$LIBRARY_ROOT/sources/`, then admin UI → submit
`sources/yourfile.mov`. The ID scheme assigns the code (e.g. `FILM-0042`) and renames
the file to `FILM-0042_yourfile.mov`. Stages:
probe → scenedetect → whisper → shotvl → vlm → clap → embed → narrative → proxy → joins.

Retry failed jobs from the admin UI — completed stages are not recomputed.
Audio-less files skip the whisper stage automatically.

## Model tiers (admin UI → Model tier)

- `tier_full`: Qwen2.5-VL-7B + ShotVL-3B + whisper-large (12GB)
- `tier_balanced`: Qwen2.5-VL-3B + whisper-medium (8GB)
- `tier_minimal`: whisper-small + 3B VLM (6GB)

## Editor workflow (client WebUI)

Open `http://192.168.1.35:8787`, search, click a shot card → it plays in the timeline
player with full context (shot blocks + waveform + synced transcript). **"copy ID+TC"**
puts `FILM-0042@HH:MM:SS.ss` on the clipboard — use it to locate the source in your
local twin library.

**XML timeline export:** `GET /api/export/fcp7xml?shot_ids=1,2,3` — import in
Resolve/Premiere; relinks by filename.

## Twin-library sync

Server `$LIBRARY_ROOT/sources/` and the editor's local library mirror each other
(same relative paths); sync with robocopy/FreeFileSync/Resilio — sources only, never
`derived/` (server-only, served over HTTP by nginx).

## Known environment fixes baked in

- `LD_LIBRARY_PATH` for cublas/cudnn (ctranslate2 needs it in the pytorch runtime image)
- `transformers>=4.49,<5` pinned (5.x requires torch ≥2.6)
- torch≥2.6 pickle-load gate neutralised in `worker/ingest.py` (CLAP ships `.bin`)
- ShotVL fed frame strips as multiple images (avoids the PyAV `av.AVError` break)
- `HF_CACHE` on sdb (model weights must never fill the 96 GB root LV)

---

## Data model

Two independent segmentations of the same timeline, reconciled by overlap.

| Table | Role | Key columns |
|-------|------|-------------|
| `files` | source media (twin-library) | `file_code` (definitive ID, e.g. `FILM-0042`), `rel_path`, `duration_s`, `fps` |
| `id_schemes` | ID pattern config (admin UI) | `pattern`, `prefix`, `counter_pad`, `counter_next`, `rename_on_ingest` |
| `shots` | **visual atom** (PySceneDetect) | `tc_in`, `tc_out`, ShotVL tags (`shot_size`, `camera_angle`, `camera_movement`, `lens_type`, `lighting`, `composition`), Qwen fields (`description`, `mood`, `color_scheme`, `objects`), `text_vec vector(1024)` |
| `utterances` | **audio atom** (Whisper sentences) | `tc_in`, `tc_out`, `speaker`, `text`, `audio_vec vector(512)`, `text_vec vector(1024)` |
| `utterance_shots` | many-to-many time join | `(utterance_id, shot_id, overlap_s)` |
| `scenes` | narrative pass output | `tc_in`, `tc_out`, `narrative_label`, `sequence_type`, `summary`, `source` |
| `scene_shots` | scene → shots | `(scene_id, shot_id)` |
| `corrections` | human fixes = future fine-tune set | `entity`, `entity_id`, `field`, `old_value`, `new_value` |
| `ingest_queue` / `ingest_events` | admin UI state | per-stage status + `error_tail` |
| `worker_state` | singleton | `paused`, `current_model` |

Indexes: HNSW on `shots.text_vec` and `utterances.audio_vec`/`text_vec`; GIN full-text
on shot descriptions and utterance text.

**Definitive shot identity** = `{file_code}@{HH:MM:SS.ss}` (e.g. `FILM-0042@00:41:20.08`).
Shot PKs are internal; timecodes never move, so they are the stable display identity.

## Ingest stages (per file)

`probe` (ffprobe) → `scenedetect` (PySceneDetect + thumbnails) → `whisper`
(faster-whisper large-v3, skips audio-less files) → `shotvl` (ShotVL-3B on frame
strips) → `vlm` (Qwen2.5-VL-7B 4-bit on keyframes) → `clap` (audio vectors) →
`embed` (BGE-M3 text vectors) → `narrative` (Qwen2.5-7B over the compressed
timeline) → `proxy` (HLS 360p + waveform) → `joins` (`utterance_shots`, `scene_shots`).

Each stage records `started`/`done`/`failed` in `ingest_events`; retry re-runs only
the stages that are not already `done`.

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/search?q=&camera_movement=&shot_size=&mood=&narrative_label=&limit=` | hybrid search (BGE-M3 cosine + tsvector BM25 + filters, RRF merge) |
| GET | `/api/search_ui?...` | htmx HTML fragment of shot cards |
| GET | `/api/shot/{id}/context` | full-file timeline for the player (shots, utterances, proxy, waveform) |
| GET | `/api/export/fcp7xml?shot_ids=1,2,3` | FCP7 XML timeline for Resolve/Premiere |
| POST | `/api/ingest?rel_path=&scheme_id=&priority=&file_code=` | submit a file, assign ID (or pass explicit `file_code` from admin batch), rename it, queue it |
| GET | `/api/admin/queue` | worker state + queue with per-stage events |
| GET | `/api/admin/folders` | top-level library folders (ingest UI picker) |
| GET | `/api/admin/library_scan?folder=` | recursive eligible-video listing for a folder |
| POST | `/api/admin/folder_cleanup?folder=` | flatten folder, delete non-video files + subdirs, sync DB paths |
| POST | `/api/admin/pause?paused=` | pause/resume the worker |
| POST | `/api/admin/retry/{queue_id}` | re-queue a failed job |
| GET/POST | `/api/admin/config` | read/write the model tier |

## Troubleshooting

- **Ingest stuck at a stage** → admin UI shows the loaded model; the worker holds the
  GPU for one stage at a time, so a job may wait behind another.
- **`libcublas.so.12 not found`** → `LD_LIBRARY_PATH` must include the
  `site-packages/nvidia/{cublas,cudnn}/lib` paths (already set in compose).
- **Disk full kills Postgres** → keep `HF_CACHE` on sdb; the preflight script checks.
- **A shot has no tags** → check `ingest_events.error_tail`; a bad JSON parse is
  logged per shot but does not fail the stage.

