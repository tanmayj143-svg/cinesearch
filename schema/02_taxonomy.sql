-- Narrative taxonomy: three orthogonal axes, as lookup tables (LLM prompt reads these)
CREATE TABLE taxonomy (
    tax_id serial PRIMARY KEY,
    axis   text NOT NULL,          -- scene_function | shot_role | sequence_type
    label  text NOT NULL,
    UNIQUE (axis, label)
);

INSERT INTO taxonomy (axis, label) VALUES
-- Axis 1: scene narrative function
('scene_function','opening_image'), ('scene_function','establishing_world'),
('scene_function','closing_image'), ('scene_function','hero_introduction'),
('scene_function','villain_introduction'), ('scene_function','ally_introduction'),
('scene_function','mentor_introduction'), ('scene_function','inciting_incident'),
('scene_function','midpoint_reversal'), ('scene_function','darkest_moment'),
('scene_function','twist_reveal'), ('scene_function','rising_tension'),
('scene_function','confrontation'), ('scene_function','final_confrontation'),
('scene_function','chase_pursuit'), ('scene_function','battle_setpiece'),
('scene_function','climax'), ('scene_function','falling_action'),
('scene_function','resolution_denouement'), ('scene_function','epilogue'),
('scene_function','flashback'), ('scene_function','dream_sequence'),
('scene_function','parallel_storyline'),
-- Axis 2: shot editorial role
('shot_role','establishing_shot'), ('shot_role','master_shot'), ('shot_role','coverage'),
('shot_role','insert_cutaway'), ('shot_role','reaction_shot'), ('shot_role','pov_shot'),
('shot_role','transition_shot'), ('shot_role','hero_shot'), ('shot_role','entrance_shot'),
('shot_role','exit_shot'), ('shot_role','detail_insert'),
-- Axis 3: sequence type
('sequence_type','dialogue_scene'), ('sequence_type','action_setpiece'),
('sequence_type','montage'), ('sequence_type','musical_sequence'),
('sequence_type','exposition'), ('sequence_type','suspense_build'),
('sequence_type','comedic_beat'), ('sequence_type','quiet_character_moment'),
('sequence_type','travel_passage'), ('sequence_type','training_sequence'),
('sequence_type','celebration'), ('sequence_type','grief_aftermath');
