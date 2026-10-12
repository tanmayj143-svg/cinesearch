"""Model tiers: admin UI selects based on GPU availability. Worker reads at startup.
All models run IN-PROCESS in the worker, sequentially (guaranteed single-model GPU
residency on 12GB). No vLLM sidecars.
"""
import json, os

CONFIG_PATH = os.environ.get("CINESEARCH_CONFIG", "/app/config.json")

TIERS = {
    "tier_full": {          # 12GB GPU (RTX 3060)
        "vlm":       "Qwen/Qwen2.5-VL-7B-Instruct",     # 4-bit ~5GB
        "shotvl":    "Vchitect/ShotVL-3B",              # bf16 ~7GB
        "whisper":   "large-v3",
        "narrative": "Qwen/Qwen2.5-7B-Instruct",        # 4-bit ~5GB
        "vlm_frames": 1,
    },
    "tier_balanced": {      # 8GB
        "vlm":       "Qwen/Qwen2.5-VL-3B-Instruct",
        "shotvl":    "Vchitect/ShotVL-3B",
        "whisper":   "medium",
        "narrative": "Qwen/Qwen2.5-3B-Instruct",
        "vlm_frames": 1,
    },
    "tier_minimal": {       # 6GB / weak GPU
        "vlm":       "Qwen/Qwen2.5-VL-3B-Instruct",
        "shotvl":    "Vchitect/ShotVL-3B",
        "whisper":   "small",
        "narrative": "Qwen/Qwen2.5-3B-Instruct",
        "vlm_frames": 1,
    },
}

DEFAULTS = {
    "tier": "tier_full",
    "library_root": "/library",
    "derived_root": "/library/derived",
    "database_url": os.environ.get("DATABASE_URL",
        "postgresql://cinesearch:cinesearch@db:5432/cinesearch"),
    "proxy_height": 360,
    "shotvl_num_frames": 12,
}

def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH)))
    cfg["models"] = TIERS[cfg["tier"]]
    return cfg

def save(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)
