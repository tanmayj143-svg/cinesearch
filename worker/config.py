"""Model tiers: admin UI selects based on GPU availability. Worker reads config per job.
All models run IN-PROCESS in the worker. Default mode is sequential (one GPU model
resident at a time — guaranteed fit on 12GB). RESIDENCY_PROFILES opt into keeping
several GPU models loaded simultaneously to skip reload time; each profile is sized
for the VRAM budget in its label and must not be selected on a smaller GPU.
No vLLM sidecars.
"""
import json, os

CONFIG_PATH = os.environ.get("CINESEARCH_CONFIG", "/app/config.json")

TIERS = {
    "tier_full": {          # 12GB GPU (RTX 3060)
        "vlm":       "unsloth/Qwen2.5-VL-7B-Instruct-bnb-4bit",     # 4-bit ~6.9GB
        "shotvl":    "Vchitect/ShotVL-3B",                          # bf16 ~7GB
        "whisper":   "large-v3",
        "narrative": "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",        # 4-bit ~5.5GB
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

# Per-stage GPU VRAM estimates (weights + working set), GB.
_WHISPER_COST = {"large-v3": 4.5, "medium": 2.5, "small": 1.5}
_VLM_COST = {
    "Qwen/Qwen2.5-VL-7B-Instruct": 5.5,
    "unsloth/Qwen2.5-VL-7B-Instruct-bnb-4bit": 5.5,
    "Qwen/Qwen2.5-VL-3B-Instruct": 3.0,
}
_NARR_COST = {
    "Qwen/Qwen2.5-7B-Instruct": 5.5,
    "unsloth/Qwen2.5-7B-Instruct-bnb-4bit": 5.5,
    "Qwen/Qwen2.5-3B-Instruct": 3.0,
}
_SHOTVL_COST = 7.0
_CLAP_COST = 1.5

def _stage_cost(key: str, tier: dict) -> float:
    if key == "whisper":   return _WHISPER_COST.get(tier["whisper"], 1.5)
    if key == "shotvl":    return _SHOTVL_COST
    if key == "vlm":       return _VLM_COST.get(tier["vlm"], 3.0)
    if key == "narrative": return _NARR_COST.get(tier["narrative"], 3.0)
    if key == "clap":      return _CLAP_COST
    return 0.0

# RESIDENCY: which GPU models may stay loaded simultaneously between stages.
# "sequential" is the default and the only 12GB-safe profile (current RTX 3060).
# pair/trio/full_24 are written for larger GPUs (8GB headroom rule: the worker
# refuses to activate a profile whose estimate exceeds detected GPU memory).
RESIDENCY_PROFILES = {
    "sequential": {
        "label": "Sequential — one GPU model at a time (12GB safe, default)",
        "models": [],
        "gpu_gb": 12,
    },
    "pair_vlm_shot": {
        "label": "ShotVL + Qwen-VL resident together (needs ~10GB at 3B tier)",
        "models": ["shotvl", "vlm"],
        "gpu_gb": 10,
    },
    "trio_vlm_narr": {
        "label": "ShotVL + Qwen-VL + Narrative resident (needs ~16GB GPU)",
        "models": ["shotvl", "vlm", "narrative"],
        "gpu_gb": 16,
    },
    "full_24": {
        "label": "All GPU models resident simultaneously (needs 24GB GPU)",
        "models": ["whisper", "shotvl", "vlm", "clap", "narrative"],
        "gpu_gb": 24,
    },
}

def profile_vram(profile: str, tier: str) -> float:
    """GB the given residency profile needs under the given tier."""
    t = TIERS.get(tier, TIERS["tier_full"])
    p = RESIDENCY_PROFILES.get(profile, RESIDENCY_PROFILES["sequential"])
    if not p["models"]:   # sequential: peak = largest single GPU stage
        return max(_stage_cost(k, t) for k in
                   ("whisper", "shotvl", "vlm", "clap", "narrative"))
    return sum(_stage_cost(m, t) for m in p["models"])

DEFAULTS = {
    "tier": "tier_full",
    "residency": "sequential",   # 12GB-safe default; bigger profiles need bigger GPU
    "library_root": "/library",
    "media_root": "/media",
    "derived_root": "/library/derived",
    "database_url": os.environ.get("DATABASE_URL",
        "postgresql://cinesearch:***@db:5432/cinesearch"),
    "proxy_height": 360,
    "shotvl_num_frames": 12,
}

def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH)))
    cfg["models"] = TIERS[cfg["tier"]]
    p = RESIDENCY_PROFILES.get(cfg.get("residency", "sequential"),
                               RESIDENCY_PROFILES["sequential"])
    cfg["residency_models"] = p["models"]
    cfg["residency_vram_gb"] = profile_vram(cfg.get("residency", "sequential"), cfg["tier"])
    return cfg

def save(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)

def merge(update: dict):
    """Update selected keys of the shared config.json without rewriting derived keys."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.load(open(CONFIG_PATH))
        except json.JSONDecodeError:
            cfg = {}
    cfg.update(update)
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)
