# CineSearch

Searchable cinematic database for an editing workflow: natural-language + structured
search over shots, dialogue, camera language, and narrative beats — with a client
web UI and an admin console.

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
