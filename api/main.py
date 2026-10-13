"""CineSearch API: hybrid search, ingest submission, Resolve/Premiere export, admin."""
import os, json
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse
import psycopg
from sentence_transformers import SentenceTransformer

DB = os.environ.get("DATABASE_URL",
    "postgresql://cinesearch:cinesearch@db:5432/cinesearch")
LIB = os.environ.get("LIBRARY_ROOT", "/library")

app = FastAPI(title="CineSearch")
_embed = None
def embed_model():
    global _embed
    if _embed is None:
        _embed = SentenceTransformer("BAAI/bge-m3", device="cpu")
    return _embed

def fmt_tc(s):
    s = float(s); h, m = int(s//3600), int(s%3600//60)
    return f"{h:02d}:{m:02d}:{s%60:05.2f}"

# ---------------- search ----------------

@app.get("/api/search")
def search(q: str = Query(""), camera_movement: str = "", mood: str = "",
           narrative_label: str = "", shot_size: str = "", limit: int = 40):
    """Hybrid: BGE-M3 cosine + tsvector BM25 + structured filters, RRF merge."""
    clauses, params = [], {}
    if camera_movement: clauses.append("s.camera_movement = %(camera_movement)s"); params["camera_movement"] = camera_movement
    if mood:            clauses.append("s.mood = %(mood)s");                     params["mood"] = mood
    if shot_size:       clauses.append("s.shot_size = %(shot_size)s");           params["shot_size"] = shot_size
    if narrative_label: clauses.append("sc.narrative_label = %(nl)s");           params["nl"] = narrative_label
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    results = {}
    with psycopg.connect(DB) as conn:
        if q:
            params["vec"] = embed_model().encode(q, normalize_embeddings=True).tolist()
            rows = conn.execute(f"""
                SELECT s.shot_id FROM shots s
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {where} ORDER BY s.text_vec <=> %(vec)s::vector LIMIT 200""", params).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
            vparams = dict(params); vparams["q"] = q
            vclauses = clauses + ["to_tsvector('english', coalesce(s.description,'')) @@ plainto_tsquery(%(q)s)"]
            rows = conn.execute(f"""
                SELECT s.shot_id FROM shots s
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                WHERE {" AND ".join(vclauses)} LIMIT 200""", vparams).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
            uparams = {"q": q}
            rows = conn.execute("""
                SELECT DISTINCT us.shot_id FROM utterances u
                JOIN utterance_shots us ON us.utterance_id=u.utterance_id
                WHERE to_tsvector('english', u.text) @@ plainto_tsquery(%(q)s)""",
                uparams).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
        else:
            rows = conn.execute(f"""
                SELECT s.shot_id FROM shots s
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {where} ORDER BY s.shot_id DESC LIMIT {limit}""", params).fetchall()
            results = {r[0]: 1.0 for r in rows}
        ids = sorted(results, key=results.get, reverse=True)[:limit]
        if not ids:
            return {"results": []}
        rows = conn.execute("""
            SELECT s.shot_id, f.file_code, s.tc_in, s.tc_out, s.thumb_path,
                   s.shot_size, s.camera_movement, s.mood, s.description,
                   s.narrative_role, sc.narrative_label,
                   (SELECT string_agg(u.text, ' ') FROM utterance_shots us2
                    JOIN utterances u ON u.utterance_id=us2.utterance_id
                    WHERE us2.shot_id=s.shot_id) AS transcript
            FROM shots s JOIN files f USING (file_id)
            LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
            LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
            WHERE s.shot_id = ANY(%(ids)s)""", {"ids": ids}).fetchall()
    out = [dict(shot_id=r[0], code=f"{r[1]}@{fmt_tc(r[2])}", file_code=r[1],
                tc_in=fmt_tc(r[2]), tc_out=fmt_tc(r[3]), thumb=r[4],
                shot_size=r[5], movement=r[6], mood=r[7], description=r[8],
                role=r[9], scene_label=r[10], transcript=(r[11] or "")[:200])
           for r in rows]
    out.sort(key=lambda x: ids.index(x["shot_id"]))
    return {"results": out}

@app.get("/api/shot/{shot_id}/context")
def context(shot_id: int):
    """Full-file timeline data for the context player."""
    with psycopg.connect(DB) as conn:
        s = conn.execute("""SELECT file_id, tc_in, tc_out FROM shots
                            WHERE shot_id=%s""", (shot_id,)).fetchone()
        shots = conn.execute("""SELECT shot_id, shot_seq, tc_in, tc_out, shot_size,
                                camera_movement FROM shots WHERE file_id=%s ORDER BY shot_seq""",
                             (s[0],)).fetchall()
        utts = conn.execute("""SELECT tc_in, tc_out, text, speaker FROM utterances
                               WHERE file_id=%s ORDER BY tc_in""", (s[0],)).fetchall()
        rel = conn.execute("SELECT rel_path, file_code FROM files WHERE file_id=%s",
                           (s[0],)).fetchone()
        prox = conn.execute("SELECT 1 FROM files WHERE file_id=%s", (s[0],)).fetchone()
    fid = s[0]
    return dict(file_id=fid, shot_tc=[s[1], s[2]], file_rel=rel[0],
                code=f"{rel[1]}@{fmt_tc(s[1])}",
                proxy=f"/derived/proxies/{fid}.m3u8",
                waveform=f"/derived/waveforms/{fid}.png",
                shots=[dict(id=x[0], seq=x[1], a=x[2], b=x[3], size=x[4], mv=x[5]) for x in shots],
                utterances=[dict(a=x[0], b=x[1], text=x[2], spk=x[3]) for x in utts])

# ---------------- export ----------------

@app.get("/api/export/fcp7xml", response_class=PlainTextResponse)
def export_xml(shot_ids: str):
    ids = [int(x) for x in shot_ids.split(",")]
    with psycopg.connect(DB) as conn:
        rows = conn.execute("""
            SELECT f.rel_path, f.orig_name, f.duration_s, s.tc_in, s.tc_out,
                   f.file_code, s.shot_id
            FROM shots s JOIN files f USING (file_id)
            WHERE s.shot_id = ANY(%s) ORDER BY f.file_code, s.tc_in""", (ids,)).fetchall()
    clips = []
    for i, (rel, orig, dur, a, b, code, sid) in enumerate(rows):
        clips.append(f"""<clipitem id="c{i}">
  <name>{code}@{fmt_tc(a)}</name><duration>{int(dur*25)}</duration>
  <rate><timebase>25</timebase></rate>
  <in>{int(a*25)}</in><out>{int(b*25)}</out>
  <file><name>{orig}</name><pathurl>file://localhost/{rel}</pathurl>
  <rate><timebase>25</timebase></rate><duration>{int(dur*25)}</duration></file>
</clipitem>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xmeml version="4"><sequence><name>CineSearch Export</name>
<rate><timebase>25</timebase></rate>
<media><video><track>{"".join(clips)}</track></video></media>
</sequence></xmeml>"""

# ---------------- ingest submit + admin ----------------

@app.post("/api/ingest")
def ingest(rel_path: str, scheme_id: int | None = None, priority: int = 100,
           file_code: str | None = None):
    src = os.path.join(LIB, rel_path)
    if not os.path.isfile(src):
        raise HTTPException(404, f"file not found in library: {rel_path}")
    with psycopg.connect(DB, autocommit=True) as conn:
        sch = conn.execute("""SELECT * FROM id_schemes
            WHERE scheme_id=%s OR (scheme_id IS NULL AND is_default)
            LIMIT 1""", (scheme_id,)).fetchone() or \
            conn.execute("SELECT * FROM id_schemes WHERE is_default LIMIT 1").fetchone()
        if not sch:
            raise HTTPException(400, "no default ID scheme configured (id_schemes is empty)")
        if file_code:
            code = file_code.strip()
            if conn.execute("SELECT 1 FROM files WHERE file_code=%s", (code,)).fetchone():
                raise HTTPException(409, f"file_code already exists: {code}")
        else:
            code = f"{sch[3]}-{sch[5]:0{sch[4]}d}"
            conn.execute("UPDATE id_schemes SET counter_next=counter_next+1 WHERE scheme_id=%s",
                         (sch[0],))
        orig = os.path.basename(rel_path)
        if sch[7]:  # rename_on_ingest
            stem, ext = os.path.splitext(orig)
            new_rel = os.path.join(os.path.dirname(rel_path), f"{code}_{stem}{ext}")
            os.rename(os.path.join(LIB, rel_path), os.path.join(LIB, new_rel))
            rel_path = new_rel
        fid = conn.execute("""INSERT INTO files (file_code, scheme_id, rel_path, orig_name)
            VALUES (%s,%s,%s,%s) RETURNING file_id""",
            (code, sch[0], rel_path, orig)).fetchone()[0]
        qid = conn.execute("""INSERT INTO ingest_queue (file_id, priority)
            VALUES (%s,%s) RETURNING queue_id""", (fid, priority)).fetchone()[0]
    return {"file_code": code, "file_id": fid, "queue_id": qid}

@app.get("/api/admin/config")
def get_config():
    cfg_path = "/app/config.json"
    return json.load(open(cfg_path)) if os.path.exists(cfg_path) else {"tier": "tier_full"}

@app.post("/api/admin/config")
def set_config(cfg: dict):
    cfg_path = "/app/config.json"
    cur = get_config(); cur.update(cfg)
    json.dump(cur, open(cfg_path, "w"), indent=2)
    return cur

@app.get("/api/admin/folders")
def library_folders():
    """Top-level folders inside the library root (for the ingest folder picker)."""
    out = []
    try:
        for name in sorted(os.listdir(LIB)):
            if os.path.isdir(os.path.join(LIB, name)) and not name.startswith('derived'):
                out.append(name)
    except OSError:
        pass
    return {"folders": out}

@app.get("/api/admin/library_scan")
def library_scan(folder: str = "sources"):
    """Recursively list eligible video files under a library folder."""
    VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".mpg", ".mpeg", ".ts"}
    root = os.path.realpath(os.path.join(LIB, folder))
    if not root.startswith(os.path.realpath(LIB)):
        raise HTTPException(400, "folder outside library")
    if not os.path.isdir(root):
        raise HTTPException(404, f"folder not found: {folder}")
    files = []
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                fp = os.path.join(dirpath, n)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files.append(dict(rel_path=os.path.relpath(fp, LIB),
                                  name=n, size_bytes=st.st_size,
                                  modified=int(st.st_mtime)))
    return {"folder": folder, "files": files}

@app.post("/api/admin/folder_cleanup")
def folder_cleanup(folder: str):
    """Flatten a library folder: move (renamed) video files to its root,
    delete non-video files and subdirectories, and update files.rel_path in the DB."""
    VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".mpg", ".mpeg", ".ts"}
    root = os.path.realpath(os.path.join(LIB, folder))
    libroot = os.path.realpath(LIB)
    if not root.startswith(libroot) or root == libroot:
        raise HTTPException(400, "folder must be inside the library")
    if not os.path.isdir(root):
        raise HTTPException(404, f"folder not found: {folder}")
    moved, deleted = [], []
    # 1. flatten videos from subdirs into folder root
    for dirpath, dirs, names in os.walk(root, topdown=False):
        for n in names:
            fp = os.path.join(dirpath, n)
            if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                dest = os.path.join(root, n)
                if fp != dest:
                    if os.path.exists(dest):
                        deleted.append(os.path.relpath(fp, LIB))  # duplicate name; drop source
                        os.remove(fp)
                        continue
                    os.rename(fp, dest)
                    moved.append((os.path.relpath(fp, LIB), os.path.relpath(dest, LIB)))
            else:
                os.remove(fp)
                deleted.append(os.path.relpath(fp, LIB))
    # 2. drop now-empty subdirectories
    for dirpath, dirs, names in os.walk(root, topdown=False):
        if dirpath != root and not os.listdir(dirpath):
            os.rmdir(dirpath)
    # 3. keep the DB in sync
    with psycopg.connect(DB, autocommit=True) as conn:
        for old, new in moved:
            conn.execute("UPDATE files SET rel_path=%s WHERE rel_path=%s", (new, old))
    return {"folder": folder, "moved": len(moved), "deleted": deleted}

@app.get("/api/admin/queue")
def queue():
    with psycopg.connect(DB) as conn:
        rows = conn.execute("""
            SELECT q.queue_id, f.file_code, q.status, q.priority,
                   (SELECT json_agg(json_build_object('stage',e.stage,'status',e.status,
                      'ts',e.ts,'err',e.error_tail) ORDER BY e.event_id)
                    FROM (SELECT * FROM ingest_events WHERE queue_id=q.queue_id) e) AS events
            FROM ingest_queue q JOIN files f USING (file_id)
            ORDER BY q.queue_id DESC LIMIT 50""").fetchall()
        ws = conn.execute("SELECT paused, current_model FROM worker_state WHERE id=1").fetchone()
    return {"worker": {"paused": ws[0], "model": ws[1]},
            "queue": [dict(queue_id=r[0], code=r[1], status=r[2],
                           priority=r[3], events=r[4]) for r in rows]}

@app.post("/api/admin/pause")
def pause(paused: bool = True):
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("UPDATE worker_state SET paused=%s WHERE id=1", (paused,))
    return {"paused": paused}

@app.post("/api/admin/retry/{queue_id}")
def retry(queue_id: int):
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("UPDATE ingest_queue SET status='pending' WHERE queue_id=%s", (queue_id,))
    return {"status": "pending"}


# htmx fragment router (imported last to avoid a circular import with `search`)
from search_ui import router as _search_ui_router
app.include_router(_search_ui_router)
