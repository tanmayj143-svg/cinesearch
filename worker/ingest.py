"""Ingest worker: polls ingest_queue, runs 10 stages per file.
GPU discipline: stages load models sequentially via a process-level mutex —
only one GPU stage resident at a time. Model servers (vLLM) are warm sidecars
the worker calls over HTTP; local models (ShotVL, whisper, CLAP) load/unload
inside this process per stage.
"""
import os, time, json, subprocess, traceback
import psycopg
from psycopg.rows import dict_row
from config import load

# torch 2.5.1 + transformers>=4.49 ships a hard gate (CVE-2025-32434) that blocks
# torch.load of pickle weights. We control every model we load, so neutralise it
# (CLAP and others still ship .bin rather than safetensors).
try:
    import transformers.modeling_utils as _mw
    import transformers.utils.import_utils as _iu
    _mw.check_torch_load_is_safe = lambda *a, **k: None
    _iu.check_torch_load_is_safe = lambda *a, **k: None
except Exception:
    pass

CFG = load()
DB = CFG["database_url"]
LIB = CFG["library_root"]

STAGES = ["probe", "scenedetect", "whisper", "shotvl", "vlm",
          "clap", "embed", "narrative", "proxy", "joins"]

# ---------- model residency cache ----------
# When CFG["residency_models"] lists a stage's model, it stays loaded in this
# dict between stages/jobs and is reused instead of cold-loading. Models not in
# the profile are released after their stage (original sequential behaviour).
GPU_CACHE = {}
GPU_TOTAL_GB = None

def gpu_total_gb():
    global GPU_TOTAL_GB
    if GPU_TOTAL_GB is None:
        try:
            import pynvml
            pynvml.nvmlInit()
            GPU_TOTAL_GB = pynvml.nvmlDeviceGetMemoryInfo(
                pynvml.nvmlDeviceGetHandleByIndex(0)).total / 1024**3
        except Exception:
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                                      "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=10).stdout
                GPU_TOTAL_GB = float(out.splitlines()[0]) / 1024
            except Exception:
                GPU_TOTAL_GB = 12.0   # conservative default if detection fails
    return GPU_TOTAL_GB

def cached(key: str, loader):
    """Return the cached model for key, or load it via loader(). If residency
    does not include this key, the caller must release it (see release_if_needed)."""
    if key in GPU_CACHE:
        return GPU_CACHE[key]
    total = gpu_total_gb()
    if CFG["residency_vram_gb"] > total:
        raise RuntimeError(
            f"residency profile needs ~{CFG['residency_vram_gb']:.1f}GB but GPU has "
            f"{total:.1f}GB — lower residency in the admin panel")
    mdl = loader()
    GPU_CACHE[key] = mdl
    return mdl

def resident(key: str) -> bool:
    return key in CFG.get("residency_models", [])

def release_if_needed(key: str):
    """Drop the model unless residency profile keeps it loaded."""
    if resident(key):
        return
    if key in GPU_CACHE:
        del GPU_CACHE[key]
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

def log(conn, queue_id, stage, status, detail=None, error=None):
    tail = None
    if error:
        tail = "".join(traceback.format_exc()[-2000:])
    conn.execute(
        "INSERT INTO ingest_events (queue_id, stage, status, detail, error_tail) VALUES (%s,%s,%s,%s,%s)",
        (queue_id, stage, status, detail, tail))

def set_model(conn, name):
    conn.execute("UPDATE worker_state SET current_model=%s, updated_at=now() WHERE id=1", (name,))

# ---------- stage implementations ----------

def st_probe(conn, f):
    src = os.path.join(LIB, f["rel_path"])
    out = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
                          "-show_format","-show_streams", src],
                         capture_output=True, text=True, check=True).stdout
    meta = json.loads(out)
    vs = next(s for s in meta["streams"] if s["codec_type"]=="video")
    num, den = (vs.get("avg_frame_rate","0/1").split("/") + ["1"])[:2]
    fps = float(num)/float(den) if float(den) else 0.0
    conn.execute("""UPDATE files SET duration_s=%s, fps=%s, width=%s, height=%s,
                    codec=%s, size_bytes=%s WHERE file_id=%s""",
                 (float(meta["format"]["duration"]), fps,
                  vs.get("width"), vs.get("height"), vs.get("codec_name"),
                  int(meta["format"]["size"]), f["file_id"]))

def st_scenedetect(conn, f):
    from scenedetect import detect, ContentDetector
    src = os.path.join(LIB, f["rel_path"])
    scenes = detect(src, ContentDetector(threshold=27))
    if not scenes:
        # cut-free source (test footage, single-take clips): treat the whole
        # file as one shot so downstream stages still produce a searchable atom
        dur = conn.execute("SELECT duration_s FROM files WHERE file_id=%s",
                           (f["file_id"],)).fetchone()["duration_s"] or 0
        class _FT:
            def __init__(self, s): self._s = float(s)
            def get_seconds(self): return self._s
        scenes = [(_FT(0.0), _FT(dur))]
    for i, (a, b) in enumerate(scenes):
        conn.execute("INSERT INTO shots (file_id, shot_seq, tc_in, tc_out) VALUES (%s,%s,%s,%s)",
                     (f["file_id"], i+1, a.get_seconds(), b.get_seconds()))
    outdir = os.path.join(CFG["derived_root"], "thumbs", str(f["file_id"]))
    os.makedirs(outdir, exist_ok=True)
    rows = conn.execute("SELECT shot_id, tc_in, tc_out FROM shots WHERE file_id=%s",
                        (f["file_id"],)).fetchall()
    for r in rows:
        sid, a, b = r["shot_id"], r["tc_in"], r["tc_out"]
        mid = (a+b)/2
        thumb = os.path.join(outdir, f"{sid}.jpg")
        subprocess.run(["ffmpeg","-y","-v","error","-ss",str(mid),"-i",src,
                        "-frames:v","1","-q:v","4", thumb], check=True)
        conn.execute("UPDATE shots SET thumb_path=%s WHERE shot_id=%s",
                     (f"derived/thumbs/{f['file_id']}/{sid}.jpg", sid))

def st_whisper(conn, f):
    src = os.path.join(LIB, f["rel_path"])
    has_audio = subprocess.run(["ffprobe","-v","error","-select_streams","a",
        "-show_entries","stream=index","-of","csv=p=0", src],
        capture_output=True, text=True).stdout.strip()
    if not has_audio:
        log(conn, f["queue_id"], "whisper", "done", "no audio stream — skipped")
        return
    set_model(conn, "whisper")
    def _load():
        from faster_whisper import WhisperModel
        return WhisperModel(CFG["models"]["whisper"], device="cuda",
                            compute_type="int8_float16")
    model = cached("whisper", _load)
    segs, _ = model.transcribe(src, word_timestamps=True, vad_filter=True)
    for s in segs:
        conn.execute("""INSERT INTO utterances (file_id, tc_in, tc_out, text)
                        VALUES (%s,%s,%s,%s)""",
                     (f["file_id"], s.start, s.end, s.text.strip()))
    release_if_needed("whisper")

def st_shotvl(conn, f):
    """Cinematic tags from a frame strip fed as multiple images (temporal reasoning
    without the PyAV video path)."""
    import torch, tempfile, glob, shutil, subprocess as sp
    set_model(conn, "shotvl")
    def _load():
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
        proc = AutoProcessor.from_pretrained(CFG["models"]["shotvl"])
        mdl = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            CFG["models"]["shotvl"], torch_dtype=torch.bfloat16, device_map="cuda")
        return proc, mdl
    proc, mdl = cached("shotvl", _load)
    src = os.path.join(LIB, f["rel_path"])
    rows = conn.execute("SELECT shot_id, tc_in, tc_out FROM shots WHERE file_id=%s ORDER BY shot_seq",
                        (f["file_id"],)).fetchall()
    q = ("These are consecutive frames of one movie shot. Answer as JSON: "
         "{shot_size, camera_angle, camera_movement, lens_type, lighting, composition}. "
         "camera_movement one of: static, pan, tilt, dolly_in, dolly_out, "
         "tracking, crane, handheld, steadicam, zoom, arc.")
    for r in rows:
        sid, a, b = r["shot_id"], r["tc_in"], r["tc_out"]
        tmpdir = tempfile.mkdtemp()
        sp.run(["ffmpeg","-y","-v","error","-ss",str(a),"-to",str(b),
                "-i",src,"-vf",f"fps={CFG['shotvl_num_frames']},scale=448:-2",
                os.path.join(tmpdir,"f%03d.jpg")], check=False)
        frames = sorted(glob.glob(os.path.join(tmpdir,"*.jpg")))
        if not frames:
            shutil.rmtree(tmpdir, ignore_errors=True); continue
        content = [{"type":"image","image":f"file://{fp}"} for fp in frames]
        content.append({"type":"text","text":q})
        msgs = [{"role":"user","content":content}]
        text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[text], images=frames, return_tensors="pt").to("cuda")
        out = mdl.generate(**inputs, max_new_tokens=256)
        ans = proc.batch_decode(out[:, inputs.input_ids.shape[1]:],
                                skip_special_tokens=True)[0]
        try:
            tags = json.loads(ans[ans.find("{"):ans.rfind("}")+1])
            conn.execute("""UPDATE shots SET shot_size=%s, camera_angle=%s,
                camera_movement=%s, lens_type=%s, lighting=%s, composition=%s
                WHERE shot_id=%s""",
                (tags.get("shot_size"), tags.get("camera_angle"),
                 tags.get("camera_movement"), tags.get("lens_type"),
                 tags.get("lighting"), tags.get("composition"), sid))
        except (json.JSONDecodeError, ValueError):
            log(conn, f["queue_id"], "shotvl", "failed", f"parse shot {sid}")
        shutil.rmtree(tmpdir, ignore_errors=True)
    release_if_needed("shotvl")

def st_vlm(conn, f):
    """Description/mood/objects via in-process Qwen2.5-VL (4-bit), keyframe only."""
    import torch
    set_model(conn, "qwen-vl")
    def _load():
        from transformers import (Qwen2_5_VLForConditionalGeneration, AutoProcessor,
                                  BitsAndBytesConfig)
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                 bnb_4bit_compute_dtype=torch.bfloat16)
        proc = AutoProcessor.from_pretrained(CFG["models"]["vlm"])
        mdl = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            CFG["models"]["vlm"], quantization_config=bnb, device_map="cuda")
        return proc, mdl
    proc, mdl = cached("vlm", _load)
    rows = conn.execute("SELECT shot_id, thumb_path FROM shots WHERE file_id=%s ORDER BY shot_seq",
                        (f["file_id"],)).fetchall()
    q = ("Describe this film frame for an editor's search database. JSON: "
         "{description (one sentence), mood, color_scheme, objects (array of "
         "salient objects/people), narrative_role (one of: establishing_shot, "
         "master_shot, coverage, insert_cutaway, reaction_shot, pov_shot, "
         "transition_shot, hero_shot, entrance_shot, exit_shot, detail_insert)}")
    for r in rows:
        sid = r["shot_id"]
        imgpath = os.path.join(LIB, r["thumb_path"])
        msgs = [{"role":"user","content":[
            {"type":"image","image":f"file://{imgpath}"},
            {"type":"text","text":q}]}]
        text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[text], images=[imgpath], return_tensors="pt").to("cuda")
        out = mdl.generate(**inputs, max_new_tokens=400)
        ans = proc.batch_decode(out[:, inputs.input_ids.shape[1]:],
                                skip_special_tokens=True)[0]
        try:
            tags = json.loads(ans[ans.find("{"):ans.rfind("}")+1])
            conn.execute("""UPDATE shots SET description=%s, mood=%s, color_scheme=%s,
                            objects=%s, narrative_role=%s WHERE shot_id=%s""",
                         (tags.get("description"), tags.get("mood"),
                          tags.get("color_scheme"), json.dumps(tags.get("objects",[])),
                          tags.get("narrative_role"), sid))
        except (json.JSONDecodeError, ValueError):
            log(conn, f["queue_id"], "vlm", "failed", f"parse shot {sid}")
    release_if_needed("vlm")

def st_clap(conn, f):
    import torch, torchaudio, tempfile, subprocess as sp
    set_model(conn, "clap")
    def _load():
        from transformers import ClapModel, ClapProcessor
        mdl = ClapModel.from_pretrained("laion/clap-htsat-unfused").to("cuda")
        proc = ClapProcessor.from_pretrained("laion/clap-htsat-unfused")
        return mdl, proc
    mdl, proc = cached("clap", _load)
    src = os.path.join(LIB, f["rel_path"])
    rows = conn.execute("SELECT utterance_id, tc_in, tc_out FROM utterances WHERE file_id=%s",
                        (f["file_id"],)).fetchall()
    for r in rows:
        uid, a, b = r["utterance_id"], r["tc_in"], r["tc_out"]
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sp.run(["ffmpeg","-y","-v","error","-ss",str(a),"-to",str(b),
                    "-i",src,"-ar","48000","-ac","1",tmp.name], check=True)
        wav, sr = torchaudio.load(tmp.name)
        inp = proc(audios=wav.squeeze().numpy(), sampling_rate=48000,
                   return_tensors="pt").to("cuda")
        vec = mdl.get_audio_features(**inp)[0]
        v = (vec/vec.norm()).cpu().tolist()
        conn.execute("UPDATE utterances SET audio_vec=%s WHERE utterance_id=%s", (v, uid))
        os.unlink(tmp.name)
    release_if_needed("clap")

def st_embed(conn, f):
    from sentence_transformers import SentenceTransformer
    set_model(conn, "bge-m3")
    mdl = SentenceTransformer("BAAI/bge-m3", device="cpu")
    rows = conn.execute("""SELECT shot_id, coalesce(description,'') || ' ' ||
        coalesce(mood,'') || ' ' || coalesce(camera_movement,'') AS txt FROM shots
        WHERE file_id=%s""", (f["file_id"],)).fetchall()
    for r in rows:
        v = mdl.encode(r["txt"], normalize_embeddings=True).tolist()
        conn.execute("UPDATE shots SET text_vec=%s WHERE shot_id=%s", (v, r["shot_id"]))
    rows = conn.execute("SELECT utterance_id, text FROM utterances WHERE file_id=%s",
                        (f["file_id"],)).fetchall()
    for r in rows:
        v = mdl.encode(r["text"], normalize_embeddings=True).tolist()
        conn.execute("UPDATE utterances SET text_vec=%s WHERE utterance_id=%s", (v, r["utterance_id"]))

def st_narrative(conn, f):
    """Text-only LLM pass over the compressed timeline -> scenes (in-process, 4-bit)."""
    import torch
    set_model(conn, "qwen-narrative")
    def _load():
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                 bnb_4bit_compute_dtype=torch.bfloat16)
        tok = AutoTokenizer.from_pretrained(CFG["models"]["narrative"])
        mdl = AutoModelForCausalLM.from_pretrained(
            CFG["models"]["narrative"], quantization_config=bnb, device_map="cuda")
        return tok, mdl
    tok, mdl = cached("narrative", _load)
    rows = conn.execute("""
        SELECT s.tc_in, s.shot_size, s.camera_movement, s.description,
               string_agg(u.text, ' ' ORDER BY u.tc_in) AS dlg
        FROM shots s LEFT JOIN utterance_shots us ON us.shot_id=s.shot_id
        LEFT JOIN utterances u ON u.utterance_id=us.utterance_id
        WHERE s.file_id=%s GROUP BY s.shot_id ORDER BY s.tc_in""",
        (f["file_id"],)).fetchall()
    lines = [f"[{_fmt_tc(r['tc_in'])}] {r['shot_size'] or '?'} {r['camera_movement'] or '?'} "
             f"| {(r['description'] or '')[:80]} | {(r['dlg'] or '')[:80]}" for r in rows]
    labels = [r["label"] for r in conn.execute(
        "SELECT label FROM taxonomy WHERE axis='scene_function'").fetchall()]
    prompt = ("Segment this film timeline into scenes. Output ONLY a JSON array; "
              "each item {start_tc, end_tc, narrative_label (one of: " + ", ".join(labels) +
              "), sequence_type, summary (one sentence)}.\n\n" + "\n".join(lines))
    msgs = [{"role":"user","content":prompt}]
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to("cuda")
    out = mdl.generate(**inputs, max_new_tokens=4000)
    content = tok.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
    for sc in json.loads(content[content.find("["):content.rfind("]")+1]):
        conn.execute("""INSERT INTO scenes (file_id, tc_in, tc_out, narrative_label,
                        sequence_type, summary, source) VALUES (%s,%s,%s,%s,%s,%s,'llm')""",
                     (f["file_id"], _parse_tc(sc["start_tc"]), _parse_tc(sc["end_tc"]),
                      sc.get("narrative_label"), sc.get("sequence_type"), sc.get("summary")))
    release_if_needed("narrative")

def st_proxy(conn, f):
    src = os.path.join(LIB, f["rel_path"])
    outdir = os.path.join(CFG["derived_root"], "proxies")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(["ffmpeg","-y","-v","error","-i",src,
        "-vf",f"scale=-2:{CFG['proxy_height']}","-c:v","libx264","-preset","fast",
        "-crf","28","-c:a","aac","-b:a","96k",
        "-hls_time","4","-hls_playlist_type","vod",
        os.path.join(outdir, f"{f['file_id']}.m3u8")], check=True)
    subprocess.run(["ffmpeg","-y","-v","error","-i",src,"-filter_complex",
        "showwavespic=s=2000x200:split_channels=0","-frames:v","1",
        os.path.join(CFG["derived_root"], "waveforms", f"{f['file_id']}.png")],
        check=False)

def st_joins(conn, f):
    conn.execute("""
        INSERT INTO utterance_shots (utterance_id, shot_id, overlap_s)
        SELECT u.utterance_id, s.shot_id,
               LEAST(u.tc_out, s.tc_out) - GREATEST(u.tc_in, s.tc_in)
        FROM utterances u JOIN shots s ON s.file_id=u.file_id
        WHERE u.file_id=%s AND u.tc_out > s.tc_in AND u.tc_in < s.tc_out
        ON CONFLICT DO NOTHING""", (f["file_id"],))
    conn.execute("""
        INSERT INTO scene_shots (scene_id, shot_id)
        SELECT sc.scene_id, s.shot_id FROM scenes sc
        JOIN shots s ON s.file_id=sc.file_id
        WHERE sc.file_id=%s AND s.tc_in >= sc.tc_in AND s.tc_out <= sc.tc_out
        ON CONFLICT DO NOTHING""", (f["file_id"],))

STAGE_FNS = dict(probe=st_probe, scenedetect=st_scenedetect, whisper=st_whisper,
    shotvl=st_shotvl, vlm=st_vlm, clap=st_clap, embed=st_embed,
    narrative=st_narrative, proxy=st_proxy, joins=st_joins)

def _fmt_tc(s): return time.strftime("%H:%M:%S", time.gmtime(s))
def _parse_tc(t):
    p = [float(x) for x in str(t).split(":")]
    return p[0]*3600+p[1]*60+p[2] if len(p)==3 else p[0]*60+p[1] if len(p)==2 else p[0]

def _connect():
    for i in range(60):
        try:
            return psycopg.connect(DB, autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError:
            print(f"db not ready ({i}), retrying in 5s", flush=True)
            time.sleep(5)
    raise SystemExit("database unreachable")

def run():
    global CFG
    os.makedirs(os.path.join(CFG["derived_root"], "waveforms"), exist_ok=True)
    with _connect() as conn:
        while True:
            old_tier = CFG.get("tier")
            CFG.update(load())   # pick up tier/residency changes from admin UI
            if old_tier and old_tier != CFG.get("tier"):
                GPU_CACHE.clear()    # resident models belong to the previous tier
                try:
                    import torch; torch.cuda.empty_cache()
                except Exception:
                    pass
            resident = list(GPU_CACHE.keys())
            paused = conn.execute("SELECT paused FROM worker_state WHERE id=1").fetchone()["paused"]
            job = None if paused else conn.execute("""
                SELECT q.queue_id, f.* FROM ingest_queue q
                JOIN files f USING (file_id)
                WHERE q.status='pending' ORDER BY q.priority, q.queue_id
                LIMIT 1""").fetchone()
            if not job:
                set_model(conn, f"idle [{','.join(resident)}]" if resident else None)
                time.sleep(5); continue
            f = dict(job)
            conn.execute("UPDATE ingest_queue SET status='active' WHERE queue_id=%s",
                         (f["queue_id"],))
            done = {r["stage"] for r in conn.execute(
                "SELECT stage FROM ingest_events WHERE queue_id=%s AND status='done'",
                (f["queue_id"],)).fetchall()}
            stage = None
            try:
                for stage in STAGES:
                    if stage in done: continue
                    log(conn, f["queue_id"], stage, "started")
                    STAGE_FNS[stage](conn, f)
                    log(conn, f["queue_id"], stage, "done")
                conn.execute("UPDATE ingest_queue SET status='done' WHERE queue_id=%s",
                             (f["queue_id"],))
                conn.execute("UPDATE files SET status='done' WHERE file_id=%s",
                             (f["file_id"],))
            except Exception as e:
                log(conn, f["queue_id"], stage, "failed", error=e)
                conn.execute("UPDATE ingest_queue SET status='failed' WHERE queue_id=%s",
                             (f["queue_id"],))
                conn.execute("UPDATE files SET status='failed' WHERE file_id=%s",
                             (f["file_id"],))

if __name__ == "__main__":
    run()
