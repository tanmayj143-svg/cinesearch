# CineSearch

AI-powered cinematic search engine and metadata indexing pipeline for film editors, directors, and video researchers. CineSearch provides natural language and structured search over shots, dialogue, camera language, visual composition, and narrative scene beats.

---

## Overview & Capabilities

CineSearch turns raw video libraries into rich, searchable editorial databases:
- **Visual Shot Classification:** Automatic shot boundary detection with camera movement (pan, tilt, dolly, crane, handheld), shot size (close-up, medium, wide), lighting, and lens characterization via vision-language models.
- **Dialogue Transcription:** Audio extraction and word-level speech-to-text with precise timecodes.
- **Visual Frame Descriptions:** Rich scene descriptions, color palette tagging, mood analysis, and editorial role identification.
- **Acoustic & Semantic Embeddings:** Multimodal vector embeddings for audio effects and dialogue retrieval.
- **Narrative Scene Segmentation:** Automated storyline segmentation grouping shots into narrative beats and scene summaries.
- **Editor Tooling:** Synchronized timeline player, clip citation clipboard (`FILM-0042@01:23:45.12`), and Final Cut Pro 7 XML timeline exports compatible with DaVinci Resolve and Adobe Premiere.

---

## System Architecture

All services run cleanly within Docker via a unified `docker-compose.yml`:

| Service | Technology | Role |
|---|---|---|
| **`db`** | PostgreSQL 16 + `pgvector` | Relational storage, HNSW vector indexes, and GIN full-text indexes |
| **`api`** | Python / FastAPI | Hybrid search engine (vector cosine + BM25 RRF), ingest endpoints, and export router |
| **`worker`** | PyTorch / CUDA / Hugging Face | Ingest pipeline executing 10 sequential AI models on the GPU |
| **`nginx`** | Nginx Alpine | Web reverse proxy serving the Search UI, Pipeline Workstation, and HLS proxies |

### Automated Ingest Pipeline (10 Stages)
```
[1. Probe] ──────► [2. SceneDetect] ──► [3. Whisper] ────► [4. ShotVL] ──────► [5. VLM]
  (Metadata)         (Cut detection)     (Speech-to-text)   (Camera motion)    (Visual mood)
                                                                                     │
[10. Joins] ◄───── [9. Proxy] ◄──────── [8. Narrative] ◄─ [7. Embed] ◄────── [6. CLAP]
 (Time index)       (NVENC HLS)          (Scene beats)      (Text vectors)     (Audio vectors)
```

---

## Hardware & Prerequisites

- **Operating System:** Linux (Ubuntu/Debian recommended)
- **Container Runtime:** Docker Engine 24+ with Docker Compose v2
- **NVIDIA GPU:** Modern NVIDIA GPU with `nvidia-container-toolkit` installed
  - **12 GB VRAM:** Recommended for Full Tier (`Qwen2.5-VL-7B` + `ShotVL-3B` + `Whisper large-v3`)
  - **8 GB VRAM:** Balanced Tier (`Qwen2.5-VL-3B` + `Whisper medium`)
  - **6 GB VRAM:** Minimal Tier (`Qwen2.5-VL-3B` + `Whisper small`)
- **Storage:** Dedicated drive or directory recommended for video assets and Hugging Face model cache (~40 GB for weights).

---

## Quickstart & Installation

### 1. Clone Repository
```bash
git clone https://github.com/tanmayj143-svg/cinesearch.git
cd cinesearch
```

### 2. Configure Environment
Copy the example environment file and customize paths to your media storage:
```bash
cp .env.example .env
```

Edit `.env` to specify your media drop directory and model cache:
```bash
# Path where your video library resides
MEDIA_DROP=/path/to/your/media

# Cache directory for downloaded model weights
HF_CACHE=/path/to/your/hf_cache
```

### 3. Launch Services
```bash
docker compose up -d
```

Verify containers are running:
```bash
docker compose ps
```

### 4. Access Web Interfaces
- **Search Engine UI:** `http://localhost:8787/`
- **Pipeline & Queue Dashboard:** `http://localhost:8787/admin/`
- **Media Ingest Workstation:** `http://localhost:8787/admin/ingest.html`

*(Replace `localhost` with your host IP if accessing across a local network).*

---

## Ingesting Media

1. Place your video files (`.mp4`, `.mkv`, `.mov`) into your configured `MEDIA_DROP` directory.
2. Open the **Ingest Workstation** at `http://localhost:8787/admin/ingest.html`.
3. Select your source folder (or scan all media) and click **Scan folder**.
4. Customize your File IDs and optional category prefixes (e.g. `feature_film`, `interview`, `b_roll`).
5. Click **Submit Batch**. The background GPU worker will sequentially execute all 10 pipeline stages, with real-time GPU telemetry and progress tracking visible on the dashboard.

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/search` | `GET` | Hybrid multimodal search (text query, mood, camera movement, shot size) |
| `/api/shot/{id}/context` | `GET` | Shot timeline context, keyframe thumbnail, waveform, and synced transcript |
| `/api/export/fcp7xml` | `GET` | Generates FCP7 XML timeline file for Resolve and Premiere |
| `/api/ingest` | `POST` | Submits a file to the ingest queue with custom ID and category |
| `/api/admin/queue` | `GET` | Returns live worker telemetry, active stage, and queue status |
| `/api/admin/gpu` | `GET` | Real-time GPU VRAM, utilization percentage, temperature, and power draw |
| `/api/admin/config` | `GET/POST` | Read or update active model tier and residency profiles |

---

## License & Disclaimer

This project is provided as-is for self-hosted cinematic research and editorial cataloging. Review security configurations before exposing endpoints to untrusted networks.
