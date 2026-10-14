"""Telemetry sidecar: 1-second GPU + progress snapshots into worker_state.
Runs in its own container sharing the worker image. It NEVER overwrites
worker_state.current_model except for shotvl/vlm progress labels — the
ingest worker owns that field otherwise (whisper, cooldown, idle, etc.)."""
import time, subprocess, json, os
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://cinesearch:cinesearch@db:5432/cinesearch")
RUN_CFG = os.environ.get("RUN_CONFIG", "/app/config.json")

_applied_pl = None
def apply_pl_from_config():
    """Real-time power cap: reads the shared config.json every loop and sets
    nvidia-smi -pl when it changes (container needs SYS_ADMIN)."""
    global _applied_pl
    try:
        cfg = json.load(open(RUN_CFG))
        want = int(cfg.get("power_limit_w") or 170)
    except Exception:
        want = 170
    if want == _applied_pl:
        return
    try:
        r = subprocess.run(["nvidia-smi", "-pl", str(want)],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            _applied_pl = want
            print(f"power limit applied: {want}W", flush=True)
    except Exception:
        pass

def smi():
    out = subprocess.run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout.strip()
    if not out:
        return None
    name, tot, used, free, util, temp, power, plimit = [x.strip() for x in out.splitlines()[0].split(",")]
    return {"available": True, "name": name, "total_mb": int(tot), "used_mb": int(used),
            "free_mb": int(free), "util_pct": int(util), "temp_c": int(temp),
            "power_w": float(power) if power not in ("[N/A]", "") else 0.0,
            "power_limit_w": float(plimit) if plimit not in ("[N/A]", "") else 170.0}

def fmt(s):
    s = int(s)
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m}m" if h >= 1 else f"{max(m, 1)}m"

# Rough per-film cost of the remaining fixed stages (clap+embed+narrative+proxy+joins),
# measured on Dabangg 2: ~1,000s. Stage rates: shotvl 3.3 s/shot, vlm 3.8 s/shot.
DOWNSTREAM_S = 1000
RATE_SHOTVL, RATE_VLM = 3.3, 3.8

def run():
    print("telemetry sidecar starting (1s loop)", flush=True)
    conn = None
    while True:
        try:
            apply_pl_from_config()
            if conn is None:
                conn = psycopg.connect(DB, autocommit=True)
            g = smi()
            if not g:
                time.sleep(1); continue
            q = conn.execute(
                "SELECT queue_id, file_id FROM ingest_queue WHERE status='active' LIMIT 1").fetchone()
            if q:
                fid = q[1]
                t_shots = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s", (fid,)).fetchone()[0]
                c_vl = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s AND camera_movement IS NOT NULL",
                                    (fid,)).fetchone()[0]
                c_vlm = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s AND description IS NOT NULL",
                                     (fid,)).fetchone()[0]
                # which vision stage is actually mid-flight right now?
                ev = conn.execute("""SELECT stage, status FROM ingest_events
                    WHERE queue_id=%s AND stage IN ('shotvl','vlm')
                    ORDER BY event_id DESC LIMIT 1""", (q[0],)).fetchone()
                if t_shots > 0 and c_vl < t_shots and (not ev or ev[0] != 'vlm'):
                    step, cnt = 'shotvl', c_vl
                    step_s = (t_shots - c_vl) * RATE_SHOTVL
                    job_s = step_s + t_shots * RATE_VLM + DOWNSTREAM_S
                elif t_shots > 0 and c_vlm < t_shots and ev and ev[0] == 'vlm' and ev[1] in ('started', 'warning'):
                    step, cnt = 'vlm', c_vlm
                    step_s = (t_shots - c_vlm) * RATE_VLM
                    job_s = step_s + DOWNSTREAM_S
                else:
                    step, cnt, step_s, job_s = None, 0, 0, 0

                if step:
                    pct = int(cnt * 100 / t_shots) if t_shots else 0
                    g.update({"active_step": step,
                              "step_progress": f"{cnt}/{t_shots} shots ({pct}%)",
                              "step_eta_s": step_s, "step_eta_str": fmt(step_s),
                              "job_eta_s": job_s, "job_eta_str": fmt(job_s)})
                    conn.execute("UPDATE worker_state SET current_model=%s WHERE id=1",
                                 (f"{step} ({cnt}/{t_shots} · {pct}%)",))
                else:
                    g.update({"active_step": "processing", "step_progress": "",
                              "step_eta_s": 0, "step_eta_str": "—",
                              "job_eta_s": 0, "job_eta_str": "—"})
            else:
                g.update({"active_step": "idle", "step_progress": "",
                          "step_eta_s": 0, "step_eta_str": "—",
                          "job_eta_s": 0, "job_eta_str": "—"})
            conn.execute("UPDATE worker_state SET gpu_json=%s, updated_at=now() WHERE id=1",
                         (json.dumps(g),))
        except Exception:
            try:
                if conn: conn.close()
            except Exception:
                pass
            conn = None
        time.sleep(1)

if __name__ == "__main__":
    run()
