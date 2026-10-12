#!/usr/bin/env bash
# CineSearch preflight: run on the host before first `docker compose up`.
set -e

echo "== GPU visible to Docker =="
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi \
  | grep -E "NVIDIA GeForce|MiB /" || { echo "FAIL: GPU not visible in Docker. Install nvidia-container-toolkit."; exit 1; }

ROOT="${1:-/media/video-pipeline/cinesearch/library}"
CACHE="${2:-/media/video-pipeline/cinesearch/hf_cache}"

echo "== Library + cache on sdb =="
mkdir -p "$ROOT"/{sources,derived/{thumbs,proxies,waveforms}} "$CACHE"
for p in "$ROOT" "$CACHE"; do
  DEV=$(df --output=source "$p" | tail -1)
  echo "$p -> $DEV"
  [ "$DEV" = "/dev/sdb" ] || echo "WARN: $p is NOT on /dev/sdb (media rule violation)"
done

echo "== Disk headroom =="
df -h "$ROOT" / | tail -2

echo "OK"
