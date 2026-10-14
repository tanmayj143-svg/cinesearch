import subprocess, time

while True:
    try:
        cmd = """
from ingest import report_gpu, _connect
with _connect() as conn:
    row_vl = conn.execute("SELECT count(*) FROM shots WHERE file_id=10 AND camera_movement IS NOT NULL").fetchone()
    c_vl = row_vl["count"] if row_vl else 0
    row_vlm = conn.execute("SELECT count(*) FROM shots WHERE file_id=10 AND description IS NOT NULL").fetchone()
    c_vlm = row_vlm["count"] if row_vlm else 0
    q = conn.execute("SELECT status FROM ingest_queue WHERE queue_id=10").fetchone()
    if q and q["status"] == "active":
        if c_vl < 2554:
            label = f"shotvl ({c_vl}/2554 · {c_vl*100//2554}%)"
        else:
            label = f"vlm ({c_vlm}/2554 · {c_vlm*100//2554}%)"
        conn.execute("UPDATE worker_state SET current_model=%s WHERE id=1", (label,))
        report_gpu(conn)
"""
        subprocess.run(["docker", "exec", "cinesearch-worker-1", "python", "-c", cmd], timeout=10)
    except Exception:
        pass
    time.sleep(10)
