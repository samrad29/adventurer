CREATE TABLE adventurer.trail_name_pool (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> ''),
    name_key text NOT NULL UNIQUE CHECK (btrim(name_key) <> ''),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE adventurer.trail_identity_checkpoints (
    checkpoint_id bigint PRIMARY KEY REFERENCES adventurer.route_checkpoints (id) ON DELETE CASCADE
);

ALTER TABLE adventurer.user_profiles ADD COLUMN trail_name_display text NOT NULL DEFAULT 'both'
    CHECK (trail_name_display IN ('both', 'trail_only', 'hidden'));

CREATE TABLE adventurer.user_trail_identities (
    user_id bigint PRIMARY KEY REFERENCES adventurer.users (id) ON DELETE CASCADE,
    original_pool_id bigint NOT NULL REFERENCES adventurer.trail_name_pool (id),
    current_pool_id bigint NOT NULL REFERENCES adventurer.trail_name_pool (id),
    name text NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    route_slug text NOT NULL,
    checkpoint_slug text NOT NULL,
    revealed_at timestamptz,
    rerolled_at timestamptz,
    reroll_request_key text,
    CHECK ((rerolled_at IS NULL AND reroll_request_key IS NULL AND current_pool_id = original_pool_id)
        OR (rerolled_at IS NOT NULL AND reroll_request_key IS NOT NULL AND current_pool_id <> original_pool_id)),
    CHECK (reroll_request_key IS NULL OR char_length(reroll_request_key) BETWEEN 1 AND 128)
);

ALTER TABLE adventurer.expedition_result_entries
    ADD COLUMN trail_name_snapshot text,
    ADD COLUMN profile_name_visible boolean NOT NULL DEFAULT true;

ALTER TABLE adventurer.expedition_updates
    ADD COLUMN name_capture_present boolean NOT NULL DEFAULT false,
    ADD COLUMN profile_name_snapshot text,
    ADD COLUMN trail_name_snapshot text;

CREATE FUNCTION adventurer.lock_trail_editorial_config()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    PERFORM pg_advisory_xact_lock(211701, 1);
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER trail_pool_config_lock BEFORE INSERT OR UPDATE OR DELETE ON adventurer.trail_name_pool
FOR EACH STATEMENT EXECUTE FUNCTION adventurer.lock_trail_editorial_config();
CREATE TRIGGER trail_checkpoint_config_lock BEFORE INSERT OR UPDATE OR DELETE ON adventurer.trail_identity_checkpoints
FOR EACH STATEMENT EXECUTE FUNCTION adventurer.lock_trail_editorial_config();

CREATE FUNCTION adventurer.validate_trail_pool()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF (EXISTS (SELECT 1 FROM trail_identity_checkpoints)
        OR EXISTS (SELECT 1 FROM user_trail_identities WHERE rerolled_at IS NULL))
       AND (SELECT count(*) FROM trail_name_pool WHERE active) < 2 THEN
        RAISE EXCEPTION 'trail name pool requires two active names'
            USING ERRCODE = '23514', CONSTRAINT = 'trail_name_pool_capacity';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER trail_pool_capacity AFTER INSERT OR UPDATE OR DELETE ON adventurer.trail_name_pool
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION adventurer.validate_trail_pool();
CREATE CONSTRAINT TRIGGER trail_checkpoint_pool_capacity AFTER INSERT OR UPDATE ON adventurer.trail_identity_checkpoints
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION adventurer.validate_trail_pool();

CREATE FUNCTION adventurer.protect_trail_history()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_TABLE_NAME = 'trail_name_pool' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'retire trail names instead of deleting them';
        END IF;
        IF (NEW.id, NEW.name, NEW.name_key) IS DISTINCT FROM (OLD.id, OLD.name, OLD.name_key) THEN
            RAISE EXCEPTION 'trail name pool identity is immutable';
        END IF;
    ELSE
        IF (NEW.user_id, NEW.original_pool_id, NEW.assigned_at, NEW.route_slug, NEW.checkpoint_slug)
                IS DISTINCT FROM
           (OLD.user_id, OLD.original_pool_id, OLD.assigned_at, OLD.route_slug, OLD.checkpoint_slug)
           OR (OLD.revealed_at IS NOT NULL AND NEW.revealed_at IS DISTINCT FROM OLD.revealed_at)
           OR ((OLD.rerolled_at IS NOT NULL OR NEW.rerolled_at IS NULL)
               AND (NEW.current_pool_id, NEW.name, NEW.rerolled_at, NEW.reroll_request_key)
                   IS DISTINCT FROM (OLD.current_pool_id, OLD.name, OLD.rerolled_at, OLD.reroll_request_key)) THEN
            RAISE EXCEPTION 'trail identity history is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trail_pool_history BEFORE UPDATE OR DELETE ON adventurer.trail_name_pool
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_trail_history();
CREATE TRIGGER trail_identity_history BEFORE UPDATE ON adventurer.user_trail_identities
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_trail_history();

ALTER TABLE adventurer.trail_name_pool ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.trail_identity_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.user_trail_identities ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON adventurer.trail_name_pool, adventurer.trail_identity_checkpoints,
    adventurer.user_trail_identities FROM PUBLIC;
REVOKE ALL ON SEQUENCE adventurer.trail_name_pool_id_seq FROM PUBLIC;

DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON adventurer.trail_name_pool, adventurer.trail_identity_checkpoints,
            adventurer.user_trail_identities FROM anon;
        REVOKE ALL ON SEQUENCE adventurer.trail_name_pool_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON adventurer.trail_name_pool, adventurer.trail_identity_checkpoints,
            adventurer.user_trail_identities FROM authenticated;
        REVOKE ALL ON SEQUENCE adventurer.trail_name_pool_id_seq FROM authenticated;
    END IF;
END;
$roles$;
