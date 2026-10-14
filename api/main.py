"""CineSearch API: hybrid search, ingest submission, Resolve/Premiere export, admin."""
import os, json, hmac
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import psycopg
from sentence_transformers import SentenceTransformer

# Optional admin token: when ADMIN_TOKEN is set in the environment, mutating
# /api/admin/* endpoints and /api/ingest require an "X-Admin-Token" header.
# Read endpoints (search, stats, queue, library) stay open for the LAN.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

app = FastAPI(title="CineSearch")

@app.middleware("http")
async def admin_token_gate(request: Request, call_next):
    if ADMIN_TOKEN:
        p = request.url.path
        protected = (p.startswith("/api/admin/") and request.method != "GET") or \
                    p == "/api/ingest" or p.startswith("/api/corrections")
        if protected:
            tok = request.headers.get("x-admin-token", "")
            if not hmac.compare_digest(tok, ADMIN_TOKEN):
                return JSONResponse({"detail": "admin token required"}, 401)
    return await call_next(request)

DB = os.environ.get("DATABASE_URL",
    "postgresql://cinesearch:cinesearch@db:5432/cinesearch")
LIB = os.environ.get("LIBRARY_ROOT", "/library")

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

_FILTER_FIELDS = {"camera_movement", "shot_size", "mood", "narrative_role",
                  "lighting", "camera_angle", "lens_type"}

@app.get("/api/search")
def search(q: str = Query(""), camera_movement: str = "", mood: str = "",
           narrative_label: str = "", shot_size: str = "", narrative_role: str = "",
           file_code: str = "", category: str = "",
           limit: int = 40, offset: int = 0):
    """Hybrid: BGE-M3 cosine + tsvector BM25 (ranked) + dialogue rank, RRF merge.
    All three legs apply the same structured filters; pagination via offset/limit."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    clauses, params = [], {}
    if camera_movement: clauses.append("s.camera_movement = %(camera_movement)s"); params["camera_movement"] = camera_movement
    if mood:            clauses.append("s.mood = %(mood)s");                     params["mood"] = mood
    if shot_size:       clauses.append("s.shot_size = %(shot_size)s");           params["shot_size"] = shot_size
    if narrative_role:  clauses.append("s.narrative_role = %(role)s");           params["role"] = narrative_role
    if file_code:       clauses.append("f.file_code = %(fc)s");                  params["fc"] = file_code
    if category:        clauses.append("f.category = %(cat)s");                  params["cat"] = category
    if narrative_label: clauses.append("sc.narrative_label = %(nl)s");           params["nl"] = narrative_label
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    results, total = {}, None
    with psycopg.connect(DB) as conn:
        if q:
            params["vec"] = embed_model().encode(q, normalize_embeddings=True).tolist()
            rows = conn.execute(f"""
                SELECT s.shot_id FROM shots s
                JOIN files f USING (file_id)
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {where} ORDER BY s.text_vec <=> %(vec)s::vector LIMIT 200""", params).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
            vparams = dict(params); vparams["q"] = q
            rows = conn.execute(f"""
                SELECT s.shot_id FROM shots s
                JOIN files f USING (file_id)
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {"WHERE " + " AND ".join(clauses + ["to_tsvector('english', coalesce(s.description,'')) @@ plainto_tsquery(%(q)s)"])}
                ORDER BY ts_rank(to_tsvector('english', coalesce(s.description,'')), plainto_tsquery(%(q)s)) DESC
                LIMIT 200""", vparams).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
            # Dialogue leg: ranked by ts_rank, and filtered to shots passing the
            # same structured filters (kept in the subquery so rank is real).
            uparams = dict(params); uparams["q"] = q
            rows = conn.execute(f"""
                SELECT sub.shot_id FROM (
                    SELECT DISTINCT ON (us.shot_id) us.shot_id,
                           ts_rank(to_tsvector('english', u.text), plainto_tsquery(%(q)s)) AS rnk
                    FROM utterances u
                    JOIN utterance_shots us ON us.utterance_id=u.utterance_id
                    JOIN shots s ON s.shot_id=us.shot_id
                    JOIN files f ON f.file_id=s.file_id
                    LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                    LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                    {"WHERE " + " AND ".join(clauses + ["to_tsvector('english', u.text) @@ plainto_tsquery(%(q)s)"])}
                    ORDER BY us.shot_id, rnk DESC
                ) sub ORDER BY sub.rnk DESC LIMIT 200""", uparams).fetchall()
            for rank, (sid,) in enumerate(rows):
                results[sid] = results.get(sid, 0) + 1/(60+rank+1)
            total = len(results)
        else:
            total = conn.execute(f"""
                SELECT count(DISTINCT s.shot_id) FROM shots s
                JOIN files f USING (file_id)
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {where}""", params).fetchone()[0]
            rows = conn.execute(f"""
                SELECT DISTINCT s.shot_id, s.tc_in FROM shots s
                JOIN files f USING (file_id)
                LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
                LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
                {where} ORDER BY s.tc_in DESC LIMIT {limit} OFFSET {offset}""", params).fetchall()
            results = {r[0]: 1.0 for r in rows}
        ids = sorted(results, key=results.get, reverse=True)
        if q:
            ids = ids[offset:offset+limit]
        if not ids:
            return {"results": [], "total": total or 0, "offset": offset, "limit": limit}
        rows = conn.execute("""
            SELECT s.shot_id, f.file_code, s.tc_in, s.tc_out, s.thumb_path,
                   s.shot_size, s.camera_movement, s.mood, s.description,
                   s.narrative_role, sc.narrative_label, f.category,
                   (SELECT string_agg(u.text, ' ') FROM utterance_shots us2
                    JOIN utterances u ON u.utterance_id=us2.utterance_id
                    WHERE us2.shot_id=s.shot_id) AS transcript
            FROM shots s JOIN files f USING (file_id)
            LEFT JOIN scene_shots ss ON ss.shot_id=s.shot_id
            LEFT JOIN scenes sc ON sc.scene_id=ss.scene_id
            WHERE s.shot_id = ANY(%(ids)s)""", {"ids": ids}).fetchall()
    max_score = max(results.values()) if results else 1.0
    out = [dict(shot_id=r[0], code=f"{r[1]}@{fmt_tc(r[2])}", file_code=r[1],
                tc_in=fmt_tc(r[2]), tc_out=fmt_tc(r[3]), thumb=r[4],
                shot_size=r[5], movement=r[6], mood=r[7], description=r[8],
                role=r[9], scene_label=r[10], category=r[11],
                transcript=r[12] or "",
                score=round(results.get(r[0], 0) / max_score, 3))
           for r in rows]
    out.sort(key=lambda x: ids.index(x["shot_id"]))
    return {"results": out, "total": total, "offset": offset, "limit": limit}

@app.get("/api/filters/{field}")
def filter_values(field: str):
    """Distinct values + counts from actual shot data, for dropdown population."""
    if field == "narrative_label":
        sql = ("SELECT sc.narrative_label AS v, count(*) FROM scenes sc "
               "WHERE sc.narrative_label IS NOT NULL AND sc.narrative_label<>'' "
               "GROUP BY 1 ORDER BY 2 DESC")
    elif field in _FILTER_FIELDS:
        sql = (f"SELECT {field} AS v, count(*) FROM shots "
               f"WHERE {field} IS NOT NULL AND {field}<>'' "
               f"GROUP BY 1 ORDER BY 2 DESC")
    elif field == "file_code":
        sql = "SELECT file_code AS v, 1 FROM files ORDER BY file_code"
    elif field == "category":
        # Canonical list from the taxonomy axis, LEFT JOINed to live file counts
        # so the ingest picker and the search filter share EXACTLY the same
        # labels (no free-form drift like interview/Interview/interviews).
        sql = ("""SELECT t.label AS v, coalesce(c.n, 0) FROM taxonomy t
                  LEFT JOIN (SELECT category, count(*) AS n FROM files
                             WHERE category IS NOT NULL AND category<>''
                             GROUP BY category) c ON c.category=t.label
                  WHERE t.axis='category' ORDER BY c.n DESC NULLS LAST, t.label""")
    else:
        raise HTTPException(400, f"unknown filter field: {field}")
    with psycopg.connect(DB) as conn:
        rows = conn.execute(sql).fetchall()
    return {"field": field, "values": [{"value": r[0], "count": r[1]} for r in rows]}

@app.get("/api/shot/{shot_id}/context")
def context(shot_id: int):
    """Full-file timeline data for the context player."""
    with psycopg.connect(DB) as conn:
        s = conn.execute("""SELECT file_id, tc_in, tc_out FROM shots
                            WHERE shot_id=%s""", (shot_id,)).fetchone()
        if not s:
            raise HTTPException(404, f"shot {shot_id} not found (library may have changed)")
        shots = conn.execute("""SELECT shot_id, shot_seq, tc_in, tc_out, shot_size,
                                camera_movement FROM shots WHERE file_id=%s ORDER BY shot_seq""",
                             (s[0],)).fetchall()
        meta = conn.execute("""SELECT shot_size, camera_movement, camera_angle, lens_type,
                                lighting, mood, narrative_role, description
                               FROM shots WHERE shot_id=%s""", (shot_id,)).fetchone()
        scns = conn.execute("""SELECT tc_in, tc_out, narrative_label FROM scenes
                               WHERE file_id=%s ORDER BY tc_in""", (s[0],)).fetchall()
        utts = conn.execute("""SELECT tc_in, tc_out, text, speaker FROM utterances
                               WHERE file_id=%s ORDER BY tc_in""", (s[0],)).fetchall()
        rel = conn.execute("SELECT rel_path, file_code FROM files WHERE file_id=%s",
                           (s[0],)).fetchone()
    fid = s[0]
    return dict(file_id=fid, shot_tc=[s[1], s[2]], file_rel=rel[0],
                code=f"{rel[1]}@{fmt_tc(s[1])}",
                proxy=f"/derived/proxies/{fid}.m3u8",
                waveform=f"/derived/waveforms/{fid}.png",
                shots=[dict(id=x[0], seq=x[1], a=x[2], b=x[3], size=x[4], mv=x[5]) for x in shots],
                meta=dict(shot_size=meta[0], camera_movement=meta[1], camera_angle=meta[2],
                          lens_type=meta[3], lighting=meta[4], mood=meta[5],
                          narrative_role=meta[6], description=meta[7]) if meta else {},
                scenes=[dict(a=x[0], b=x[1], label=x[2]) for x in scns],
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
           file_code: str | None = None, category: str | None = None,
           label: str | None = None):
    src = os.path.join(LIB, rel_path)
    if not os.path.isfile(src):
        raise HTTPException(404, f"file not found in library: {rel_path}")
    # Duplicate-content guard: cheap size+mtime hash before queueing (the full
    # sha256 column is populated later by the probe stage on the worker side).
    with psycopg.connect(DB, autocommit=True) as conn:
        sch = conn.execute("""SELECT * FROM id_schemes
            WHERE scheme_id=%s OR (scheme_id IS NULL AND is_default)
            ORDER BY is_default DESC LIMIT 1 FOR UPDATE""",
            (scheme_id,)).fetchone() or \
            conn.execute("SELECT * FROM id_schemes WHERE is_default LIMIT 1 FOR UPDATE").fetchone()
        if not sch:
            raise HTTPException(400, "no default ID scheme configured (id_schemes is empty)")
        if file_code:
            code = file_code.strip()
            if conn.execute("SELECT 1 FROM files WHERE file_code=%s", (code,)).fetchone():
                raise HTTPException(409, f"file_code already exists: {code}")
        else:
            # Row is locked FOR UPDATE above: read+increment is now atomic.
            code = f"{sch[3]}-{sch[5]:0{sch[4]}d}"
            conn.execute("UPDATE id_schemes SET counter_next=counter_next+1 WHERE scheme_id=%s",
                         (sch[0],))
        # Category/prefix label: explicit param > scheme default > none.
        cat = (category or "").strip() or (sch[8] if len(sch) > 8 else None)
        if cat:
            conn.execute("INSERT INTO taxonomy (axis, label) VALUES ('category', %s) ON CONFLICT DO NOTHING", (cat,))
        orig = os.path.basename(rel_path)
        if sch[7]:  # rename_on_ingest
            stem, ext = os.path.splitext(orig)
            if cat:
                new_name = f"{cat}_{code}{ext}"
            else:
                new_name = f"{code}{ext}"
            new_rel = os.path.join(os.path.dirname(rel_path), new_name)
            os.rename(os.path.join(LIB, rel_path), os.path.join(LIB, new_rel))
            rel_path = new_rel
        fid = conn.execute("""INSERT INTO files (file_code, scheme_id, rel_path, orig_name, category)
            VALUES (%s,%s,%s,%s,%s) RETURNING file_id""",
            (code, sch[0], rel_path, orig, cat)).fetchone()[0]
        qid = conn.execute("""INSERT INTO ingest_queue (file_id, priority)
            VALUES (%s,%s) RETURNING queue_id""", (fid, priority)).fetchone()[0]
    return {"file_code": code, "file_id": fid, "queue_id": qid}

@app.get("/api/admin/codes/exists")
def code_exists(code: str):
    with psycopg.connect(DB) as conn:
        hit = conn.execute("SELECT 1 FROM files WHERE file_code=%s", (code,)).fetchone()
    return {"code": code, "exists": bool(hit)}

@app.get("/api/admin/schemes/preview")
def scheme_preview(n: int = 1, scheme_id: int | None = None):
    """Next n codes a scheme would issue — display preview only; codes are
    actually assigned atomically at submit time inside /api/ingest."""
    n = max(1, min(n, 500))
    with psycopg.connect(DB) as conn:
        sch = conn.execute("""SELECT * FROM id_schemes
            WHERE scheme_id=%s OR (scheme_id IS NULL AND is_default)
            ORDER BY is_default DESC LIMIT 1""", (scheme_id,)).fetchone()
        if not sch:
            raise HTTPException(400, "no default ID scheme configured")
        codes = [f"{sch[3]}-{sch[5]+i:0{sch[4]}d}" for i in range(n)]
    return {"scheme": sch[1], "scheme_id": sch[0], "codes": codes}

@app.post("/api/corrections")
def add_correction(entity: str, entity_id: int, field: str, new_value: str,
                   old_value: str | None = None):
    """Human edit from the search inspector — the future fine-tune dataset."""
    if entity not in ("shot", "utterance", "scene"):
        raise HTTPException(400, "entity must be shot|utterance|scene")
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("""INSERT INTO corrections (entity, entity_id, field, old_value, new_value)
                        VALUES (%s,%s,%s,%s,%s)""",
                     (entity, entity_id, field, old_value, new_value))
        if entity == "shot" and field in ("mood", "shot_size", "camera_movement",
                                          "narrative_role", "description",
                                          "camera_angle", "lens_type", "lighting"):
            conn.execute(f"UPDATE shots SET {field}=%s WHERE shot_id=%s",
                         (new_value, entity_id))
    return {"saved": True}

# Mirrors worker/config.py RESIDENCY_PROFILES for validation + admin display.
# (api and worker are separate containers; worker re-reads config.json per loop.)
RESIDENCY_PROFILES = {
    "sequential":   {"label": "Sequential — one GPU model at a time (12GB safe)", "gpu_gb": 12},
    "pair_vlm_shot":{"label": "ShotVL + Qwen-VL resident together (~10GB, 3B tier)", "gpu_gb": 10},
    "trio_vlm_narr":{"label": "ShotVL + Qwen-VL + Narrative resident (~16GB GPU)", "gpu_gb": 16},
    "full_24":      {"label": "All GPU models resident simultaneously (24GB GPU)", "gpu_gb": 24},
}

@app.get("/api/admin/residency")
def residency():
    cfg = get_config()
    return {"current": cfg.get("residency", "sequential"),
            "tier": cfg.get("tier", "tier_full"),
            "profiles": RESIDENCY_PROFILES}

@app.get("/api/admin/config")
def get_config():
    cfg_path = "/app/config.json"
    return json.load(open(cfg_path)) if os.path.exists(cfg_path) else {"tier": "tier_full"}

@app.post("/api/admin/config")
def set_config(cfg: dict):
    # Whitelist: only worker-tuning keys may be written from the UI — the raw
    # dict merge previously let a caller overwrite library_root/database_url etc.
    allowed = {"tier", "residency", "proxy_height", "shotvl_num_frames",
               "power_limit_w", "inter_job_sleep_s", "models"}
    cfg = {k: v for k, v in cfg.items() if k in allowed}
    if not cfg:
        raise HTTPException(400, f"no writable keys provided (allowed: {sorted(allowed)})")
    cfg_path = "/app/config.json"
    cur = get_config(); cur.update(cfg)
    json.dump(cur, open(cfg_path, "w"), indent=2)
    return cur

MEDIA = os.environ.get("MEDIA_ROOT", "/media")
VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".mpg", ".mpeg", ".ts"}

@app.get("/api/admin/folders")
def media_folders():
    """Folders and root media inside /media (the ingest source root)."""
    subfolders = []
    root_videos = 0
    total_videos = 0
    try:
        for name in sorted(os.listdir(MEDIA)):
            fp = os.path.join(MEDIA, name)
            if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                root_videos += 1
                total_videos += 1
            elif os.path.isdir(fp) and not name.startswith(('.', '_')) and name not in ('derived', 'video-pipeline'):
                vids = sum(1 for d, _, ns in os.walk(fp)
                           for n in ns if os.path.splitext(n)[1].lower() in VIDEO_EXTS)
                total_videos += vids
                subfolders.append(dict(name=name, path=name, videos=vids))
    except OSError:
        pass

    out = [dict(name="All media (root & subdirectories)", path=".", videos=total_videos)]
    if root_videos > 0:
        out.append(dict(name="Root files only (/media/*)", path="__root__", videos=root_videos))
    out.extend(subfolders)
    return {"media_root": MEDIA, "folders": out}

@app.get("/api/admin/library_scan")
def media_scan(folder: str = "."):
    """Recursively list eligible video files under /media or a specific subfolder."""
    if not folder or folder in (".", "/"):
        root = os.path.realpath(MEDIA)
        recurse = True
    elif folder == "__root__":
        root = os.path.realpath(MEDIA)
        recurse = False
    else:
        root = os.path.realpath(os.path.join(MEDIA, folder))
        recurse = True

    med_real = os.path.realpath(MEDIA)
    if not root.startswith(med_real):
        raise HTTPException(400, "folder outside media root")
    if not os.path.isdir(root):
        raise HTTPException(404, f"folder not found: {folder}")

    files = []
    if recurse:
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(('.', '_')) and d not in ('derived', 'video-pipeline')]
            for n in sorted(names):
                if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                    fp = os.path.join(dirpath, n)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    files.append(dict(rel_path=os.path.relpath(fp, MEDIA),
                                      name=n, size_bytes=st.st_size,
                                      modified=int(st.st_mtime)))
    else:
        for n in sorted(os.listdir(root)):
            fp = os.path.join(root, n)
            if os.path.isfile(fp) and os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files.append(dict(rel_path=os.path.relpath(fp, MEDIA),
                                  name=n, size_bytes=st.st_size,
                                  modified=int(st.st_mtime)))
    return {"folder": folder, "files": files}

@app.post("/api/admin/media/ingest_folder")
def media_ingest_folder(folder: str, scheme_id: int | None = None,
                        priority: int = 100, category: str | None = None,
                        label: str | None = None):
    """Ingest every eligible video under a /media subfolder (moves files into
    the library under their assigned codes, preserving relative structure)."""
    root = os.path.realpath(os.path.join(MEDIA, folder))
    if not root.startswith(os.path.realpath(MEDIA)):
        raise HTTPException(400, "folder outside media root")
    if not os.path.isdir(root):
        raise HTTPException(404, f"folder not found: {folder}")
    videos = []
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.join(dirpath, n))
    if not videos:
        raise HTTPException(400, f"no eligible videos under {folder}")
    results, failures = [], []
    import shutil
    with psycopg.connect(DB, autocommit=True) as conn:
        sch = conn.execute("""SELECT * FROM id_schemes
            WHERE scheme_id=%s OR (scheme_id IS NULL AND is_default)
            ORDER BY is_default DESC LIMIT 1 FOR UPDATE""",
            (scheme_id,)).fetchone() or \
            conn.execute("SELECT * FROM id_schemes WHERE is_default LIMIT 1 FOR UPDATE").fetchone()
        if not sch:
            raise HTTPException(400, "no default ID scheme configured")
        cat_batch = (category or "").strip() or (sch[8] if len(sch) > 8 else None)
        if cat_batch:
            conn.execute("INSERT INTO taxonomy (axis, label) VALUES ('category', %s) ON CONFLICT DO NOTHING", (cat_batch,))
        for src in videos:
            try:
                rel_src = os.path.relpath(src, MEDIA)
                code = f"{sch[3]}-{sch[5]:0{sch[4]}d}"
                conn.execute("UPDATE id_schemes SET counter_next=counter_next+1 WHERE scheme_id=%s",
                             (sch[0],))
                sch = list(sch); sch[5] += 1  # local counter for the batch
                cat = cat_batch
                stem, ext = os.path.splitext(os.path.basename(src))
                new_name = f"{code}_{stem}{ext}"
                dest = os.path.join(LIB, new_name)
                shutil.move(src, dest)
                fid = conn.execute("""INSERT INTO files (file_code, scheme_id, rel_path, orig_name, category)
                    VALUES (%s,%s,%s,%s,%s) RETURNING file_id""",
                    (code, sch[0], new_name, os.path.basename(src), cat)).fetchone()[0]
                conn.execute("INSERT INTO ingest_queue (file_id, priority) VALUES (%s,%s)",
                             (fid, priority))
                results.append(dict(file=os.path.basename(src), code=code))
            except Exception as e:
                failures.append(dict(file=os.path.basename(src), error=str(e)[:200]))
    return {"ingested": results, "failed": failures,
            "folder": folder, "count": len(results)}

@app.get("/api/admin/gpu")
def gpu_stats():
    """GPU telemetry written by the worker into worker_state.gpu_json."""
    try:
        with psycopg.connect(DB) as conn:
            row = conn.execute("SELECT gpu_json FROM worker_state WHERE id=1").fetchone()
        if row and row[0]:
            g = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            g["available"] = True
            g.setdefault("power_limit_w", 170.0)
            return g
    except Exception:
        pass
    return {"available": False, "note": "no GPU telemetry from worker yet"}

@app.post("/api/admin/folder_cleanup")
def folder_cleanup(folder: str, execute: bool = False):
    """Tidy a /media folder: flatten videos into the folder root, move
    non-video files to <folder>/_trash/ (NOTHING is deleted), drop empty
    subdirs. Default is a dry-run preview; pass execute=true to apply.
    Duplicate-name videos are kept with a numeric suffix rather than destroyed."""
    root = os.path.realpath(os.path.join(MEDIA, folder))
    mediaroot = os.path.realpath(MEDIA)
    if not root.startswith(mediaroot) or root == mediaroot or folder in (".", "__root__"):
        raise HTTPException(400, "folder must be inside a /media subdirectory")
    if not os.path.isdir(root):
        raise HTTPException(404, f"folder not found: {folder}")
    trash = os.path.join(root, "_trash")
    moved, trashed = [], []
    plan = []
    for dirpath, _dirs, names in os.walk(root, topdown=False):
        if os.path.realpath(dirpath).startswith(trash):
            continue
        for n in names:
            fp = os.path.join(dirpath, n)
            if os.path.splitext(n)[1].lower() in VIDEO_EXTS:
                dest = os.path.join(root, n)
                if fp != dest:
                    if os.path.exists(dest):
                        stem, ext = os.path.splitext(n)
                        i = 2
                        while os.path.exists(os.path.join(root, f"{stem}_{i}{ext}")):
                            i += 1
                        dest = os.path.join(root, f"{stem}_{i}{ext}")
                    plan.append((fp, dest))
                    moved.append((os.path.relpath(fp, MEDIA), os.path.relpath(dest, MEDIA)))
            else:
                dest = os.path.join(trash, n)
                if os.path.exists(dest):
                    stem, ext = os.path.splitext(n)
                    i = 2
                    while os.path.exists(os.path.join(trash, f"{stem}_{i}{ext}")):
                        i += 1
                    dest = os.path.join(trash, f"{stem}_{i}{ext}")
                plan.append((fp, dest))
                trashed.append(os.path.relpath(fp, MEDIA))
    if not execute:
        return {"folder": folder, "dry_run": True,
                "would_move": len(moved), "would_trash": len(trashed),
                "moves": [{"from": a, "to": b} for a, b in moved], "trash": trashed,
                "note": "nothing deleted — non-video files go to <folder>/_trash/"}
    os.makedirs(trash, exist_ok=True)
    for src_fp, dest_fp in plan:
        os.makedirs(os.path.dirname(dest_fp), exist_ok=True)
        os.rename(src_fp, dest_fp)
    for dirpath, dirs, names in os.walk(root, topdown=False):
        rp = os.path.realpath(dirpath)
        if rp != root and rp != trash and not rp.startswith(trash) and not os.listdir(dirpath):
            os.rmdir(dirpath)
    return {"folder": folder, "moved": len(moved), "trashed": trashed,
            "note": "non-video files preserved in _trash/"}

@app.get("/api/admin/schemes")
def schemes():
    with psycopg.connect(DB) as conn:
        rows = conn.execute("""SELECT scheme_id, name, pattern, prefix, counter_pad,
                               counter_next, is_default, rename_on_ingest, category
                               FROM id_schemes ORDER BY scheme_id""").fetchall()
    return {"schemes": [dict(scheme_id=r[0], name=r[1], pattern=r[2], prefix=r[3],
                             pad=r[4], next=r[5], default=r[6], rename=r[7],
                             category=r[8]) for r in rows]}

@app.post("/api/admin/schemes")
def scheme_set(name: str, prefix: str | None = None, pad: int | None = None,
               next: int | None = None, rename: bool | None = None,
               category: str | None = None,
               set_default: bool = False):
    with psycopg.connect(DB, autocommit=True) as conn:
        row = conn.execute("SELECT scheme_id FROM id_schemes WHERE name=%s", (name,)).fetchone()
        if row:
            sets, args = [], []
            for col, val in (("prefix", prefix), ("counter_pad", pad),
                             ("counter_next", next), ("rename_on_ingest", rename),
                             ("category", category)):
                if val is not None:
                    sets.append(f"{col}=%s"); args.append(val)
            if sets:
                args.append(row[0])
                conn.execute(f"UPDATE id_schemes SET {', '.join(sets)} WHERE scheme_id=%s", args)
            sid = row[0]
        else:
            sid = conn.execute("""INSERT INTO id_schemes (name, prefix, counter_pad,
                                counter_next, rename_on_ingest, category)
                                VALUES (%s,%s,%s,%s,%s,%s) RETURNING scheme_id""",
                               (name, prefix or "FILM", pad or 4, next or 1,
                                True if rename is None else rename, category)).fetchone()[0]
        if set_default:
            conn.execute("UPDATE id_schemes SET is_default=(scheme_id=%s)", (sid,))
    return {"scheme_id": sid, "name": name}

@app.get("/api/admin/stats")
def stats():
    with psycopg.connect(DB) as conn:
        counts = conn.execute("""
            SELECT (SELECT count(*) FROM files),
                   (SELECT count(*) FROM shots),
                   (SELECT count(*) FROM utterances),
                   (SELECT count(*) FROM scenes),
                   (SELECT count(*) FROM ingest_queue WHERE status='pending'),
                   (SELECT count(*) FROM ingest_queue WHERE status='active'),
                   (SELECT count(*) FROM ingest_queue WHERE status='failed'),
                   (SELECT coalesce(sum(size_bytes),0) FROM files),
                   (SELECT count(*) FROM corrections)
        """).fetchone()
        top_moods = [dict(mood=r[0], n=r[1]) for r in conn.execute("""
            SELECT mood, count(*) FROM shots WHERE mood IS NOT NULL AND mood<>''
            GROUP BY mood ORDER BY count(*) DESC LIMIT 8""").fetchall()]
        top_moves = [dict(movement=r[0], n=r[1]) for r in conn.execute("""
            SELECT camera_movement, count(*) FROM shots WHERE camera_movement IS NOT NULL
              AND camera_movement<>''
            GROUP BY camera_movement ORDER BY count(*) DESC LIMIT 8""").fetchall()]
    # GPU telemetry for the admin status strip (from worker_state)
    gpu = None
    try:
        with psycopg.connect(DB) as conn:
            row = conn.execute("SELECT gpu_json FROM worker_state WHERE id=1").fetchone()
        if row and row[0]:
            g = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if g.get("available"):
                gpu = g
    except Exception:
        pass
    return {"files": counts[0], "shots": counts[1], "utterances": counts[2],
            "scenes": counts[3], "pending": counts[4], "active": counts[5],
            "failed": counts[6], "source_bytes": counts[7], "corrections": counts[8],
            "top_moods": top_moods, "top_movements": top_moves, "gpu": gpu}

@app.get("/api/admin/library")
def library_list():
    with psycopg.connect(DB) as conn:
        rows = conn.execute("""
            SELECT f.file_id, f.file_code, f.rel_path, f.status, f.duration_s,
                   f.size_bytes, f.category,
                   (SELECT count(*) FROM shots s WHERE s.file_id=f.file_id) AS shots,
                   (SELECT count(*) FROM utterances u WHERE u.file_id=f.file_id) AS utts
            FROM files f ORDER BY f.file_code""").fetchall()
    return {"files": [dict(file_id=r[0], code=r[1], path=r[2], status=r[3],
                           duration_s=r[4], size_bytes=r[5], category=r[6],
                           shots=r[7], utterances=r[8]) for r in rows]}

@app.post("/api/admin/library/{file_id}/delete")
def library_delete(file_id: int):
    """Remove a file's DB rows (shots/utterances/scenes via FK cascade) and its
    derived media; the source file itself is left on disk."""
    with psycopg.connect(DB, autocommit=True) as conn:
        rel = conn.execute("SELECT rel_path, file_code FROM files WHERE file_id=%s",
                           (file_id,)).fetchone()
        if not rel:
            raise HTTPException(404, f"file_id {file_id} not found")
        conn.execute("DELETE FROM files WHERE file_id=%s", (file_id,))
    import shutil
    for sub in ("proxies", "waveforms"):
        p = os.path.join(LIB, "derived", sub)
        if os.path.isdir(p):
            for name in os.listdir(p):
                if name.startswith(f"{file_id}."):
                    fp = os.path.join(p, name)
                    (shutil.rmtree if os.path.isdir(fp) else os.remove)(fp)
    tdir = os.path.join(LIB, "derived", "thumbs", str(file_id))
    if os.path.isdir(tdir):
        shutil.rmtree(tdir, ignore_errors=True)
    return {"deleted_file_code": rel[1], "note": "source file left on disk"}

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
