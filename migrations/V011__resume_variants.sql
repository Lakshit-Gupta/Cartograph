-- V011__resume_variants.sql
-- Phase 2.2 — Resume A/B variant tracking.
--
-- The V002 reserved `resume_variants` placeholder is widened here with the
-- columns the apply pipeline needs: a per-tenant label, the relative path
-- to the variant's main .tex file, a source-kind discriminator (json |
-- latex), a soft-delete flag, and a logistic-regression-fit weight that
-- powers the UCB1 / probability-weighted variant picker.
--
-- Backward compat: V001 already declared `applications.resume_variant_id`
-- as a bare BIGINT with no FK (CLAUDE.md schema sketch listed it as
-- "FK added in Phase 2"). This migration introduces the FK + index. Both
-- ADDs are guarded by IF NOT EXISTS so re-running is a no-op.
--
-- Multi-tenant-ready: UNIQUE(user_id, label) so seed inserts are
-- idempotent across DEFAULT 1 owner and future tenants.

BEGIN;

-- 1. Widen the reserved placeholder.
ALTER TABLE resume_variants
    ADD COLUMN IF NOT EXISTS main_tex_path TEXT,
    ADD COLUMN IF NOT EXISTS active        BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS weight        REAL NOT NULL DEFAULT 1.0;

-- Tighten the placeholder rows that V002 declared as NULL-able.
-- ``user_id NOT NULL DEFAULT 1`` mirrors every other user-scoped table.
-- We populate any pre-existing NULL rows first so the SET NOT NULL
-- ALTER doesn't fail mid-migration.
UPDATE resume_variants SET user_id = 1 WHERE user_id IS NULL;
ALTER TABLE resume_variants
    ALTER COLUMN user_id SET DEFAULT 1,
    ALTER COLUMN user_id SET NOT NULL;

-- FK to users(id) — guard so re-runs don't trip ``constraint already exists``.
DO $$ BEGIN
    ALTER TABLE resume_variants
        ADD CONSTRAINT resume_variants_user_fk
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN duplicate_table  THEN NULL;
END $$;

-- Same idempotent guard for the UNIQUE(user_id, label) we rely on for
-- the ON CONFLICT seed insert below. ADD CONSTRAINT UNIQUE creates a
-- backing index of the same name; re-running can raise both
-- duplicate_object (constraint) AND duplicate_table (index). Catch both
-- so the migration is truly idempotent.
DO $$ BEGIN
    ALTER TABLE resume_variants
        ADD CONSTRAINT resume_variants_user_label_uniq UNIQUE (user_id, label);
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN duplicate_table  THEN NULL;
END $$;

-- 2. Wire applications.resume_variant_id into a real FK + index.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS resume_variant_id BIGINT;

DO $$ BEGIN
    ALTER TABLE applications
        ADD CONSTRAINT applications_resume_variant_fk
            FOREIGN KEY (resume_variant_id) REFERENCES resume_variants(id)
            ON DELETE SET NULL;
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN duplicate_table  THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS applications_variant_idx
    ON applications(resume_variant_id);

-- 3. Seed the v1 five variants. ``main_tex_path`` is relative to
-- ``config/profile/my_resume/``. ON CONFLICT keeps re-runs idempotent.
-- ``source_kind`` defaults to 'latex' (added in V007), so we don't set it
-- explicitly here.
INSERT INTO resume_variants (user_id, label, main_tex_path) VALUES
    (1, 'backend',      'variants/backend/main.tex'),
    (1, 'fullstack',    'variants/fullstack/main.tex'),
    (1, 'ml',           'variants/ml/main.tex'),
    (1, 'freelance',    'variants/freelance/main.tex'),
    (1, 'intern_india', 'variants/intern_india/main.tex')
ON CONFLICT (user_id, label) DO NOTHING;

INSERT INTO schema_migrations(version) VALUES ('V011') ON CONFLICT DO NOTHING;

COMMIT;
