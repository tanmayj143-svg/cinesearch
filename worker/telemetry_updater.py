import time, subprocess, json, os
import psycopg

DB = os.environ.get("DATABASE_URL", "postgresql://cinesearch:cinesearch@db:5432/cinesearch")

def run():
    print("Telemetry updater started (1s loop)...", flush=True)
    conn = psycopg.connect(DB, autocommit=True)
    while True:
        try:
            out = subprocess.run([
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits"
            ], capture_output=True, text=True, timeout=2).stdout.strip()
            
            if out:
                name, tot, used, free, util, temp, power = [x.strip() for x in out.splitlines()[0].split(",")]
                gpu_data = {
                    "available": True,
                    "name": name,
                    "total_mb": int(tot),
                    "used_mb": int(used),
                    "free_mb": int(free),
                    "util_pct": int(util),
                    "temp_c": int(temp),
                    "power_w": float(power) if power not in ("[N/A]", "") else 0.0,
                    "power_limit_w": 170.0
                }
            else:
                gpu_data = None

            # Get active job progress
            q = conn.execute("SELECT queue_id, file_id, status FROM ingest_queue WHERE status='active' LIMIT 1").fetchone()
            if q:
                fid = q[1]
                t_shots = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s", (fid,)).fetchone()[0]
                c_vl = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s AND camera_movement IS NOT NULL", (fid,)).fetchone()[0]
                c_vlm = conn.execute("SELECT count(*) FROM shots WHERE file_id=%s AND description IS NOT NULL", (fid,)).fetchone()[0]
                
                if t_shots > 0 and c_vl < t_shots:
                    rem_s = (t_shots - c_vl) * 3.3
                    rem_str = f"{int(rem_s // 3600)}h {int((rem_s % 3600) // 60)}m" if rem_s >= 3600 else f"{int(rem_s // 60)}m"
                    label = f"shotvl ({c_vl}/{t_shots} · {c_vl*100//t_shots}% · ETA {rem_str})"
                elif t_shots > 0 and c_vlm < t_shots:
                    rem_s = (t_shots - c_vlm) * 3.8
                    rem_str = f"{int(rem_s // 3600)}h {int((rem_s % 3600) // 60)}m" if rem_s >= 3600 else f"{int(rem_s // 60)}m"
                    label = f"vlm ({c_vlm}/{t_shots} · {c_vlm*100//t_shots}% · ETA {rem_str})"
                else:
                    label = "processing"
            else:
                label = "idle"

            if gpu_data:
                conn.execute(
                    "UPDATE worker_state SET current_model=%s, gpu_json=%s, updated_at=now() WHERE id=1",
                    (label, json.dumps(gpu_data))
                )
        except Exception as e:
            try:
                conn = psycopg.connect(DB, autocommit=True)
            except Exception:
                pass
        time.sleep(1)

if __name__ == "__main__":
    run()
