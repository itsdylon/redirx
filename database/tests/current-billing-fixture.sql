-- Queue and mapping shapes before applied migrations; no fake billing RPCs.
ALTER TABLE migration_sessions ADD COLUMN locked_at timestamptz,
 ADD COLUMN locked_by text, ADD COLUMN lease_expires_at timestamptz,
 ADD COLUMN attempt_count integer DEFAULT 0, ADD COLUMN last_error text,
 ADD COLUMN current_stage integer, ADD COLUMN stage_name text, ADD COLUMN total_stages integer,
 ADD COLUMN is_preview boolean NOT NULL DEFAULT false;
CREATE TABLE url_mappings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),session_id uuid NOT NULL REFERENCES migration_sessions(id) ON DELETE CASCADE,
 old_url text NOT NULL,new_url text,confidence_score double precision,match_type text,needs_review boolean NOT NULL DEFAULT false,
 repaired_url text,repair_method text,repair_confidence double precision,repair_support integer,repair_evidence text);
CREATE TABLE webpage_embeddings (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),session_id uuid NOT NULL REFERENCES migration_sessions(id),url text,site_type text,embedding jsonb,extracted_text text,title text);
CREATE FUNCTION update_updated_at_column() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
ALTER TABLE auth.users ADD COLUMN email text, ADD COLUMN email_confirmed_at timestamptz;
