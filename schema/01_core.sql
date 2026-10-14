-- CineSearch core schema
CREATE EXTENSION IF NOT EXISTS vector;

-- ID scheme registry (configured in admin UI)
CREATE TABLE id_schemes (
    scheme_id   serial PRIMARY KEY,
    name        text NOT NULL,
    pattern     text NOT NULL DEFAULT '{PREFIX}-{NUM}',   -- e.g. '{PREFIX}-{NUM}' -> FILM-0042
    prefix      text NOT NULL DEFAULT 'FILM',
    counter_pad int  NOT NULL DEFAULT 4,
    counter_next int NOT NULL DEFAULT 1,
    is_default  boolean NOT NULL DEFAULT false,
    rename_on_ingest boolean NOT NULL DEFAULT true        -- FILM-0042_original.mov
);

-- Source files (twin-library model: identical relative path exists on editor workstation)
CREATE TABLE files (
    file_id     bigserial PRIMARY KEY,
    file_code   text UNIQUE NOT NULL,                     -- FILM-0042 (definitive human ID)
    scheme_id   int REFERENCES id_schemes,
    rel_path    text NOT NULL,                            -- sources/FILM-0042_master.mov
    orig_name   text NOT NULL,
    duration_s  real, fps real, width int, height int, codec text,
    size_bytes  bigint,
    sha256      text,
    ingested_at timestamptz DEFAULT now(),
    status      text NOT NULL DEFAULT 'pending'           -- pending|processing|done|failed
);

-- Visual atom: shots (PySceneDetect boundaries)
CREATE TABLE shots (
    shot_id      bigserial PRIMARY KEY,
    file_id      bigint NOT NULL REFERENCES files ON DELETE CASCADE,
    shot_seq     int NOT NULL,                            -- order within file
    tc_in        real NOT NULL,                           -- seconds; display ID = file_code@tc
    tc_out       real NOT NULL,
    thumb_path   text,                                    -- derived/thumbs/<file>/<shot>.jpg
    -- ShotVL tags
    shot_size    text, camera_angle text, camera_movement text,
    lens_type    text, lighting text, composition text,
    -- Qwen2.5-VL description (keyframe pass)
    description  text, mood text, color_scheme text, objects jsonb,
    narrative_role text,                                  -- axis-2 editorial role
    text_vec     vector(1024),                            -- BGE-M3
    UNIQUE (file_id, shot_seq)
);

-- Audio atom: utterances (Whisper sentence segments)
CREATE TABLE utterances (
    utterance_id bigserial PRIMARY KEY,
    file_id      bigint NOT NULL REFERENCES files ON DELETE CASCADE,
    tc_in        real NOT NULL,
    tc_out       real NOT NULL,
    speaker      text,
    text         text NOT NULL,
    audio_vec    vector(512),                             -- CLAP
    text_vec     vector(1024)
);

-- Many-to-many time reconciliation (the canvas's mapping node)
CREATE TABLE utterance_shots (
    utterance_id bigint REFERENCES utterances ON DELETE CASCADE,
    shot_id      bigint REFERENCES shots ON DELETE CASCADE,
    overlap_s    real NOT NULL,
    PRIMARY KEY (utterance_id, shot_id)
);

-- Scenes: LLM narrative pass output (axis-1 function, axis-3 sequence type)
CREATE TABLE scenes (
    scene_id        bigserial PRIMARY KEY,
    file_id         bigint NOT NULL REFERENCES files ON DELETE CASCADE,
    tc_in real NOT NULL, tc_out real NOT NULL,
    narrative_label text,                                 -- final_confrontation, hero_introduction...
    sequence_type   text,                                 -- dialogue_scene, montage...
    summary         text,
    confidence      real,
    source          text NOT NULL DEFAULT 'llm'           -- heuristic|llm|human
);

CREATE TABLE scene_shots (
    scene_id bigint REFERENCES scenes ON DELETE CASCADE,
    shot_id  bigint REFERENCES shots ON DELETE CASCADE,
    PRIMARY KEY (scene_id, shot_id)
);

-- Human corrections = future fine-tune dataset
CREATE TABLE corrections (
    correction_id bigserial PRIMARY KEY,
    entity   text NOT NULL,                               -- shot|utterance|scene
    entity_id bigint NOT NULL,
    field    text NOT NULL,
    old_value text, new_value text,
    created_at timestamptz DEFAULT now()
);

-- Ingest queue + stage events (admin UI reads these)
CREATE TABLE ingest_queue (
    queue_id   bigserial PRIMARY KEY,
    file_id    bigint NOT NULL REFERENCES files ON DELETE CASCADE,
    priority   int NOT NULL DEFAULT 100,
    status     text NOT NULL DEFAULT 'pending',           -- pending|active|done|failed|paused
    submitted_at timestamptz DEFAULT now()
);

CREATE TABLE ingest_events (
    event_id  bigserial PRIMARY KEY,
    queue_id  bigint NOT NULL REFERENCES ingest_queue ON DELETE CASCADE,
    stage     text NOT NULL,   -- probe|scenedetect|whisper|shotvl|vlm|clap|embed|narrative|proxy|joins
    status    text NOT NULL,   -- started|done|failed
    detail    text,
    error_tail text,
    ts        timestamptz DEFAULT now()
);

CREATE TABLE worker_state (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    paused boolean NOT NULL DEFAULT false,
    current_model text,
    gpu_json jsonb,
    updated_at timestamptz DEFAULT now()
);
INSERT INTO worker_state (id) VALUES (1);

-- Search indexes
CREATE INDEX ON shots USING hnsw (text_vec vector_cosine_ops);
CREATE INDEX ON utterances USING hnsw (audio_vec vector_cosine_ops);
CREATE INDEX ON utterances USING hnsw (text_vec vector_cosine_ops);
CREATE INDEX ON shots (file_id, tc_in);
CREATE INDEX ON utterances (file_id, tc_in);
CREATE INDEX ON shots USING gin (to_tsvector('english', coalesce(description,'')));
CREATE INDEX ON utterances USING gin (to_tsvector('english', text));
