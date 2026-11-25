CREATE TABLE adventurer.route_completions (
    event_key text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES adventurer.users (id),
    journey_id bigint UNIQUE REFERENCES adventurer.route_journeys (id) ON DELETE SET NULL,
    route_id bigint REFERENCES adventurer.routes (id) ON DELETE SET NULL,
    route_key bigint NOT NULL,
    route_slug text NOT NULL,
    route_title text NOT NULL,
    completed_at timestamptz NOT NULL,
    completion_number bigint NOT NULL CHECK (completion_number > 0),
    UNIQUE (user_id, route_key, completion_number)
);

INSERT INTO adventurer.route_completions (
    event_key, user_id, journey_id, route_id, route_key, route_slug, route_title,
    completed_at, completion_number
)
SELECT 'route_completed:' || journey.id, journey.user_id, journey.id, route.id, route.id,
       route.slug, route.name, journey.completed_at,
       row_number() OVER (PARTITION BY journey.user_id, route.id ORDER BY journey.completed_at, journey.id)
FROM adventurer.route_journeys AS journey
JOIN adventurer.routes AS route ON route.id = journey.route_id
WHERE journey.status = 'completed';

ALTER TABLE adventurer.achievement_definitions
    ADD COLUMN evaluator_key text,
    ADD COLUMN evaluator_version integer,
    ADD COLUMN evaluator_route_id bigint,
    ADD COLUMN issuance_rule text,
    ADD CONSTRAINT managed_achievement_configuration CHECK (
        (evaluator_key IS NULL AND evaluator_version IS NULL AND evaluator_route_id IS NULL AND issuance_rule IS NULL)
        OR (evaluator_key IS NOT NULL AND evaluator_version IS NOT NULL AND evaluator_route_id IS NOT NULL
            AND issuance_rule IS NOT NULL AND evaluator_key = 'route_completion' AND evaluator_version = 1
            AND source = 'route_completion' AND scope = 'route' AND NOT repeatable
            AND issuance_rule IN ('once_per_account', 'once_per_event', 'once_per_account_per_edition')
            AND (criteria = '{}' OR CASE
                WHEN jsonb_typeof(criteria -> 'completion_number') = 'number'
                THEN criteria - 'completion_number' = '{}'
                     AND (criteria ->> 'completion_number') ~ '^[1-9][0-9]*$'
                ELSE false END))
    );

CREATE INDEX managed_achievement_route_idx ON adventurer.achievement_definitions (evaluator_route_id, id)
    WHERE evaluator_key IS NOT NULL;

ALTER TABLE adventurer.earned_achievements
    ADD COLUMN issuance_rule text,
    ADD COLUMN deduplication_key text,
    ADD COLUMN completion_event_key text REFERENCES adventurer.route_completions (event_key),
    ADD COLUMN completion_number bigint,
    ADD COLUMN route_group_key bigint;

CREATE UNIQUE INDEX managed_earned_achievement_key
    ON adventurer.earned_achievements (user_id, definition_id, deduplication_key)
    WHERE deduplication_key IS NOT NULL;
CREATE INDEX earned_achievement_owner_order_idx
    ON adventurer.earned_achievements (user_id, issued_at DESC, id DESC);
CREATE INDEX earned_achievement_completion_idx
    ON adventurer.earned_achievements (completion_event_key);

CREATE TABLE adventurer.achievement_reveals (
    earned_achievement_id bigint PRIMARY KEY REFERENCES adventurer.earned_achievements (id) ON DELETE CASCADE,
    acknowledged_at timestamptz
);

CREATE FUNCTION adventurer.protect_managed_definition()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF NEW.evaluator_route_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.evaluator_route_id IS DISTINCT FROM OLD.evaluator_route_id) THEN
        PERFORM 1 FROM routes WHERE id = NEW.evaluator_route_id FOR KEY SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'managed achievement route does not exist' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF (NEW.evaluator_key, NEW.evaluator_version, NEW.evaluator_route_id, NEW.issuance_rule)
            IS DISTINCT FROM (OLD.evaluator_key, OLD.evaluator_version, OLD.evaluator_route_id, OLD.issuance_rule)
       AND (OLD.evaluator_key IS NOT NULL OR EXISTS (
           SELECT 1 FROM earned_achievements WHERE definition_id = OLD.id
       )) THEN
        RAISE EXCEPTION 'issued achievement definition configuration is immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.issuance_rule IN ('once_per_account', 'once_per_event')
       AND EXISTS (SELECT 1 FROM achievement_editions WHERE definition_id = OLD.id) THEN
        RAISE EXCEPTION 'unlimited achievement definitions cannot have editions' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER managed_definition_history BEFORE INSERT OR UPDATE ON adventurer.achievement_definitions
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_managed_definition();

CREATE FUNCTION adventurer.validate_managed_edition()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
DECLARE
    rule text;
BEGIN
    SELECT issuance_rule INTO rule FROM achievement_definitions WHERE id = NEW.definition_id FOR SHARE;
    IF rule IN ('once_per_account', 'once_per_event') THEN
        RAISE EXCEPTION 'unlimited achievement definitions cannot have editions' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER managed_edition_configuration BEFORE INSERT ON adventurer.achievement_editions
FOR EACH ROW EXECUTE FUNCTION adventurer.validate_managed_edition();

CREATE FUNCTION adventurer.prepare_managed_award()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
DECLARE
    definition adventurer.achievement_definitions;
    completion adventurer.route_completions;
BEGIN
    -- Run after the existing issuance trigger has taken the user and catalog locks.
    SELECT * INTO definition FROM achievement_definitions WHERE id = NEW.definition_id;
    NEW.issuance_rule := definition.issuance_rule;
    NEW.deduplication_key := NULL;
    NEW.completion_number := NULL;
    NEW.route_group_key := NEW.route_id;
    IF definition.issuance_rule IS NULL THEN
        IF NEW.completion_event_key IS NOT NULL THEN
            RAISE EXCEPTION 'foundation awards cannot claim a managed completion' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO completion FROM route_completions
        WHERE event_key = NEW.completion_event_key AND user_id = NEW.user_id;
    IF NOT FOUND OR NEW.event_key IS DISTINCT FROM completion.event_key
       OR definition.evaluator_route_id IS DISTINCT FROM completion.route_key
       OR (definition.criteria ->> 'completion_number' IS NOT NULL
           AND (definition.criteria ->> 'completion_number') <> completion.completion_number::text)
       OR ((definition.issuance_rule = 'once_per_account_per_edition') <> (NEW.edition_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'managed award does not match its completion or cohort' USING ERRCODE = '23514';
    END IF;
    NEW.deduplication_key := CASE definition.issuance_rule
        WHEN 'once_per_account' THEN 'account'
        WHEN 'once_per_event' THEN completion.event_key
        ELSE 'edition:' || NEW.edition_id END;
    NEW.completion_number := completion.completion_number;
    NEW.event_at := completion.completed_at;
    NEW.route_group_key := completion.route_key;
    NEW.context_snapshot := jsonb_build_object('route',
        jsonb_build_object('slug', completion.route_slug, 'name', completion.route_title));
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER earned_achievement_managed_snapshot BEFORE INSERT ON adventurer.earned_achievements
FOR EACH ROW EXECUTE FUNCTION adventurer.prepare_managed_award();

CREATE FUNCTION adventurer.protect_route_completion()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF journey_credit_deletion_allowed(OLD.user_id) IS NOT TRUE THEN
            RAISE EXCEPTION 'route completions require account deletion' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF (to_jsonb(OLD) - ARRAY['journey_id', 'route_id']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['journey_id', 'route_id'])
       OR (NEW.journey_id IS NOT NULL AND NEW.journey_id IS DISTINCT FROM OLD.journey_id)
       OR (NEW.route_id IS NOT NULL AND NEW.route_id IS DISTINCT FROM OLD.route_id) THEN
        RAISE EXCEPTION 'route completion history is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER route_completion_history BEFORE UPDATE OR DELETE ON adventurer.route_completions
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_route_completion();

CREATE FUNCTION adventurer.protect_achievement_reveal()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.earned_achievement_id IS DISTINCT FROM OLD.earned_achievement_id
       OR (OLD.acknowledged_at IS NOT NULL AND NEW.acknowledged_at IS DISTINCT FROM OLD.acknowledged_at) THEN
        RAISE EXCEPTION 'achievement reveal acknowledgment is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER achievement_reveal_history BEFORE UPDATE ON adventurer.achievement_reveals
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_achievement_reveal();

CREATE FUNCTION adventurer.protect_trail_identity_delete()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF journey_credit_deletion_allowed(OLD.user_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'trail identity requires account deletion' USING ERRCODE = '23514';
    END IF;
    RETURN OLD;
END;
$fn$;

CREATE TRIGGER trail_identity_delete_guard BEFORE DELETE ON adventurer.user_trail_identities
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_trail_identity_delete();

CREATE FUNCTION adventurer.protect_personal_history_truncate()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF (TG_TABLE_NAME = 'user_trail_identities' AND EXISTS (SELECT 1 FROM user_trail_identities))
       OR (TG_TABLE_NAME = 'route_completions' AND EXISTS (SELECT 1 FROM route_completions)) THEN
        RAISE EXCEPTION 'personal history cannot be truncated' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER trail_identity_no_truncate BEFORE TRUNCATE ON adventurer.user_trail_identities
FOR EACH STATEMENT EXECUTE FUNCTION adventurer.protect_personal_history_truncate();
CREATE TRIGGER route_completion_no_truncate BEFORE TRUNCATE ON adventurer.route_completions
FOR EACH STATEMENT EXECUTE FUNCTION adventurer.protect_personal_history_truncate();

CREATE FUNCTION adventurer.delete_account_completion_history()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    -- Delete while the marked user is still visible to the row guards.
    DELETE FROM user_trail_identities WHERE user_id = OLD.id;
    DELETE FROM route_completions WHERE user_id = OLD.id;
    RETURN OLD;
END;
$fn$;

CREATE TRIGGER delete_account_completion_history BEFORE DELETE ON adventurer.users
FOR EACH ROW EXECUTE FUNCTION adventurer.delete_account_completion_history();

ALTER TABLE adventurer.route_completions ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.achievement_reveals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON adventurer.route_completions, adventurer.achievement_reveals FROM PUBLIC;
DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON adventurer.route_completions, adventurer.achievement_reveals FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON adventurer.route_completions, adventurer.achievement_reveals FROM authenticated;
    END IF;
END;
$roles$;
