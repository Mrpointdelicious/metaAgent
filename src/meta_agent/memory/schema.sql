-- Patient preferences only. Existing LangGraph Store/Checkpoint tables are untouched.
CREATE TABLE IF NOT EXISTS public.patient_preferences (
    owner_key text NOT NULL CHECK (owner_key ~ '^[0-9a-f]{64}$'),
    memory_id uuid NOT NULL,
    preference_key text NOT NULL,
    preference_value text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner_key, memory_id),
    UNIQUE (owner_key, preference_key),
    CONSTRAINT patient_preferences_allowed_pair CHECK (
        (preference_key = 'language' AND preference_value IN ('zh-CN', 'en')) OR
        (preference_key = 'answer_length' AND preference_value IN ('short', 'standard', 'detailed')) OR
        (preference_key = 'answer_format' AND preference_value IN ('paragraphs', 'bullets', 'steps')) OR
        (preference_key = 'tone' AND preference_value IN ('neutral', 'warm'))
    )
);
