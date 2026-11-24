-- Release 2 activity eligibility. Existing expeditions and journeys stay unrestricted.
-- Credits, totals, feed rows, and snapshots are not rewritten.

ALTER TABLE adventurer.expeditions
    ADD COLUMN activity_policy text NOT NULL DEFAULT 'unrestricted',
    ADD COLUMN activity_policy_sealed boolean NOT NULL DEFAULT true;

ALTER TABLE adventurer.expeditions
    ADD CONSTRAINT expeditions_activity_policy_chk
    CHECK (activity_policy IN ('unrestricted', 'restricted'));

ALTER TABLE adventurer.route_journeys
    ADD COLUMN activity_policy text NOT NULL DEFAULT 'unrestricted',
    ADD COLUMN governing_expedition_id bigint;

ALTER TABLE adventurer.route_journeys
    ADD CONSTRAINT route_journeys_activity_policy_chk
    CHECK (activity_policy IN ('unrestricted', 'restricted'));

ALTER TABLE adventurer.route_journeys
    ADD CONSTRAINT route_journeys_governing_expedition_fkey
    FOREIGN KEY (governing_expedition_id)
    REFERENCES adventurer.expeditions (id) ON DELETE SET NULL;

CREATE INDEX route_journeys_governing_expedition_idx
    ON adventurer.route_journeys (governing_expedition_id);

CREATE TABLE adventurer.expedition_activity_types (
    expedition_id bigint NOT NULL,
    activity_type text NOT NULL,
    CONSTRAINT expedition_activity_types_pkey PRIMARY KEY (expedition_id, activity_type),
    CONSTRAINT expedition_activity_types_expedition_fkey
        FOREIGN KEY (expedition_id)
        REFERENCES adventurer.expeditions (id) ON DELETE CASCADE,
    CONSTRAINT expedition_activity_types_type_chk CHECK (
        activity_type IN (
            'run', 'hike', 'walk', 'stair_climb', 'other', 'unspecified',
            'cycle', 'indoor_cycle'
        )
    )
);

CREATE TABLE adventurer.journey_activity_types (
    journey_id bigint NOT NULL,
    activity_type text NOT NULL,
    CONSTRAINT journey_activity_types_pkey PRIMARY KEY (journey_id, activity_type),
    CONSTRAINT journey_activity_types_journey_fkey
        FOREIGN KEY (journey_id)
        REFERENCES adventurer.route_journeys (id) ON DELETE CASCADE,
    CONSTRAINT journey_activity_types_type_chk CHECK (
        activity_type IN (
            'run', 'hike', 'walk', 'stair_climb', 'other', 'unspecified',
            'cycle', 'indoor_cycle'
        )
    )
);

CREATE TABLE adventurer.journey_policy_choices (
    journey_id bigint PRIMARY KEY,
    reason text NOT NULL,
    former_expedition_id bigint,
    CONSTRAINT journey_policy_choices_reason_chk CHECK (reason IN ('left', 'closed')),
    CONSTRAINT journey_policy_choices_journey_fkey
        FOREIGN KEY (journey_id)
        REFERENCES adventurer.route_journeys (id) ON DELETE CASCADE,
    CONSTRAINT journey_policy_choices_expedition_fkey
        FOREIGN KEY (former_expedition_id)
        REFERENCES adventurer.expeditions (id) ON DELETE SET NULL
);

CREATE OR REPLACE FUNCTION adventurer.note_expedition_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    PERFORM set_config('adventurer.expedition_delete', OLD.id::text, true);
    RETURN OLD;
END;
$fn$;

CREATE TRIGGER expeditions_note_delete
BEFORE DELETE ON adventurer.expeditions
FOR EACH ROW
EXECUTE FUNCTION adventurer.note_expedition_delete();

CREATE OR REPLACE FUNCTION adventurer.protect_expedition_activity_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF OLD.activity_policy_sealed AND (
        NEW.activity_policy IS DISTINCT FROM OLD.activity_policy
        OR NEW.activity_policy_sealed IS DISTINCT FROM OLD.activity_policy_sealed
    ) THEN
        RAISE EXCEPTION USING MESSAGE = 'this expedition''s activity rules are fixed';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER expeditions_protect_activity_policy
BEFORE UPDATE ON adventurer.expeditions
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_expedition_activity_policy();

CREATE OR REPLACE FUNCTION adventurer.protect_expedition_activity_types()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    parent_id bigint;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    parent_id := COALESCE(NEW.expedition_id, OLD.expedition_id);
    IF current_setting('adventurer.expedition_delete', true) = parent_id::text THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM expeditions
        WHERE id = parent_id
          AND activity_policy_sealed
    ) THEN
        RAISE EXCEPTION USING MESSAGE = 'this expedition''s activity rules are fixed';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER expedition_activity_types_protect
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.expedition_activity_types
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_expedition_activity_types();

CREATE OR REPLACE FUNCTION adventurer.expedition_activity_policy_nonempty()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    parent_id bigint;
    policy text;
    sealed boolean;
    type_count integer;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'expeditions' THEN
        parent_id := COALESCE(NEW.id, OLD.id);
    ELSE
        parent_id := COALESCE(NEW.expedition_id, OLD.expedition_id);
    END IF;
    SELECT activity_policy, activity_policy_sealed
    INTO policy, sealed
    FROM expeditions
    WHERE id = parent_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT COUNT(*) INTO type_count
    FROM expedition_activity_types
    WHERE expedition_id = parent_id;
    IF policy = 'restricted' THEN
        IF NOT sealed OR type_count < 1 THEN
            RAISE EXCEPTION USING MESSAGE = 'a restricted expedition needs at least one activity type';
        END IF;
    ELSIF type_count > 0 THEN
        RAISE EXCEPTION USING MESSAGE = 'an unrestricted expedition cannot list activity types';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER expedition_activity_policy_nonempty
AFTER INSERT OR UPDATE OR DELETE ON adventurer.expeditions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.expedition_activity_policy_nonempty();

CREATE CONSTRAINT TRIGGER expedition_activity_types_nonempty
AFTER INSERT OR UPDATE OR DELETE ON adventurer.expedition_activity_types
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.expedition_activity_policy_nonempty();

CREATE OR REPLACE FUNCTION adventurer.journey_activity_policy_nonempty()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    parent_id bigint;
    policy text;
    type_count integer;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'route_journeys' THEN
        parent_id := COALESCE(NEW.id, OLD.id);
    ELSE
        parent_id := COALESCE(NEW.journey_id, OLD.journey_id);
    END IF;
    SELECT activity_policy INTO policy
    FROM route_journeys
    WHERE id = parent_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT COUNT(*) INTO type_count
    FROM journey_activity_types
    WHERE journey_id = parent_id;
    IF policy = 'restricted' THEN
        IF type_count < 1 THEN
            RAISE EXCEPTION USING MESSAGE = 'a restricted journey needs at least one activity type';
        END IF;
    ELSIF type_count > 0 THEN
        RAISE EXCEPTION USING MESSAGE = 'an unrestricted journey cannot list activity types';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_activity_policy_nonempty
AFTER INSERT OR UPDATE OR DELETE ON adventurer.route_journeys
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_activity_policy_nonempty();

CREATE CONSTRAINT TRIGGER journey_activity_types_nonempty
AFTER INSERT OR UPDATE OR DELETE ON adventurer.journey_activity_types
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_activity_policy_nonempty();

CREATE OR REPLACE FUNCTION adventurer.protect_completed_journey_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    parent_id bigint;
    parent_status text;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'route_journeys' THEN
        IF OLD.status = 'completed' AND NEW.activity_policy IS DISTINCT FROM OLD.activity_policy THEN
            RAISE EXCEPTION USING MESSAGE = 'a completed journey cannot change activity rules';
        END IF;
        IF OLD.status = 'completed'
           AND NEW.governing_expedition_id IS DISTINCT FROM OLD.governing_expedition_id THEN
            IF NEW.governing_expedition_id IS NULL
               AND current_setting('adventurer.expedition_delete', true) = OLD.governing_expedition_id::text THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION USING MESSAGE = 'a completed journey cannot change activity rules';
        END IF;
        RETURN NEW;
    END IF;
    parent_id := COALESCE(NEW.journey_id, OLD.journey_id);
    SELECT status INTO parent_status FROM route_journeys WHERE id = parent_id;
    IF parent_status = 'completed' THEN
        RAISE EXCEPTION USING MESSAGE = 'a completed journey cannot change activity rules';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER route_journeys_protect_completed_policy
BEFORE UPDATE ON adventurer.route_journeys
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_journey_policy();

CREATE TRIGGER journey_activity_types_protect_completed
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.journey_activity_types
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_journey_policy();

CREATE TRIGGER journey_policy_choices_protect_completed
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.journey_policy_choices
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_journey_policy();

CREATE OR REPLACE FUNCTION adventurer.journey_governor_consistent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    checked bigint;
    journey record;
    expected bigint;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'route_journeys' THEN
        checked := COALESCE(NEW.id, OLD.id);
    ELSIF TG_TABLE_NAME = 'journey_activity_types' THEN
        checked := COALESCE(NEW.journey_id, OLD.journey_id);
    ELSIF TG_TABLE_NAME = 'expedition_participations' THEN
        checked := COALESCE(NEW.journey_id, OLD.journey_id);
    ELSE
        checked := NULL;
    END IF;
    IF checked IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT id, user_id, route_id, status, activity_policy, governing_expedition_id
    INTO journey
    FROM route_journeys
    WHERE id = checked;
    IF NOT FOUND OR journey.status = 'completed' THEN
        RETURN NULL;
    END IF;
    IF journey.status NOT IN ('active', 'paused', 'abandoned') THEN
        RETURN NULL;
    END IF;
    SELECT expedition.id
    INTO expected
    FROM expedition_participations AS participation
    JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
    WHERE participation.journey_id = journey.id
      AND participation.user_id = journey.user_id
      AND participation.status = 'active'
      AND expedition.status = 'active'
      AND expedition.route_id = journey.route_id
      AND expedition.activity_policy = 'restricted'
      AND journey.activity_policy = 'restricted'
      AND NOT EXISTS (
          SELECT activity_type
          FROM expedition_activity_types
          WHERE expedition_id = expedition.id
          EXCEPT
          SELECT activity_type
          FROM journey_activity_types
          WHERE journey_id = journey.id
      )
      AND NOT EXISTS (
          SELECT activity_type
          FROM journey_activity_types
          WHERE journey_id = journey.id
          EXCEPT
          SELECT activity_type
          FROM expedition_activity_types
          WHERE expedition_id = expedition.id
      )
    ORDER BY expedition.id
    LIMIT 1;
    IF journey.activity_policy = 'unrestricted' THEN
        IF journey.governing_expedition_id IS NOT NULL THEN
            RAISE EXCEPTION USING MESSAGE = 'an unrestricted journey cannot have a governing expedition';
        END IF;
        RETURN NULL;
    END IF;
    IF journey.governing_expedition_id IS DISTINCT FROM expected THEN
        RAISE EXCEPTION USING MESSAGE = 'the governing expedition does not match the journey policy';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_governor_consistent
AFTER INSERT OR UPDATE OR DELETE ON adventurer.route_journeys
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_governor_consistent();

CREATE CONSTRAINT TRIGGER journey_governor_consistent_types
AFTER INSERT OR UPDATE OR DELETE ON adventurer.journey_activity_types
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_governor_consistent();

CREATE CONSTRAINT TRIGGER journey_governor_consistent_participations
AFTER INSERT OR UPDATE OR DELETE ON adventurer.expedition_participations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_governor_consistent();

CREATE OR REPLACE FUNCTION adventurer.journey_governor_consistent_expedition()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    checked bigint;
    journey_id bigint;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NULL;
    END IF;
    checked := COALESCE(NEW.id, OLD.id);
    FOR journey_id IN
        SELECT id
        FROM route_journeys
        WHERE governing_expedition_id = checked
        UNION
        SELECT participation.journey_id
        FROM expedition_participations AS participation
        WHERE participation.expedition_id = checked
    LOOP
        PERFORM adventurer.journey_governor_consistent_one(journey_id);
    END LOOP;
    RETURN NULL;
END;
$fn$;

CREATE OR REPLACE FUNCTION adventurer.journey_governor_consistent_one(checked bigint)
RETURNS void
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    journey record;
    expected bigint;
BEGIN
    SELECT id, user_id, route_id, status, activity_policy, governing_expedition_id
    INTO journey
    FROM route_journeys
    WHERE id = checked;
    IF NOT FOUND OR journey.status = 'completed' THEN
        RETURN;
    END IF;
    IF journey.status NOT IN ('active', 'paused', 'abandoned') THEN
        RETURN;
    END IF;
    SELECT expedition.id
    INTO expected
    FROM expedition_participations AS participation
    JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
    WHERE participation.journey_id = journey.id
      AND participation.user_id = journey.user_id
      AND participation.status = 'active'
      AND expedition.status = 'active'
      AND expedition.route_id = journey.route_id
      AND expedition.activity_policy = 'restricted'
      AND journey.activity_policy = 'restricted'
      AND NOT EXISTS (
          SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
          EXCEPT
          SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
      )
      AND NOT EXISTS (
          SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
          EXCEPT
          SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
      )
    ORDER BY expedition.id
    LIMIT 1;
    IF journey.activity_policy = 'unrestricted' THEN
        IF journey.governing_expedition_id IS NOT NULL THEN
            RAISE EXCEPTION USING MESSAGE = 'an unrestricted journey cannot have a governing expedition';
        END IF;
        RETURN;
    END IF;
    IF journey.governing_expedition_id IS DISTINCT FROM expected THEN
        RAISE EXCEPTION USING MESSAGE = 'the governing expedition does not match the journey policy';
    END IF;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_governor_consistent_expeditions
AFTER UPDATE OR DELETE ON adventurer.expeditions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_governor_consistent_expedition();

CREATE OR REPLACE FUNCTION adventurer.journey_policy_choice_consistent()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    checked bigint;
    journey record;
    expected bigint;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'route_journeys' THEN
        checked := COALESCE(NEW.id, OLD.id);
    ELSE
        checked := COALESCE(NEW.journey_id, OLD.journey_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM journey_policy_choices WHERE journey_id = checked
    ) THEN
        RETURN NULL;
    END IF;
    SELECT id, user_id, route_id, status, activity_policy, governing_expedition_id
    INTO journey
    FROM route_journeys
    WHERE id = checked;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF journey.status = 'completed' OR journey.activity_policy <> 'restricted' OR journey.governing_expedition_id IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE = 'that activity-rule choice is not allowed';
    END IF;
    IF journey.status NOT IN ('active', 'paused', 'abandoned') THEN
        RAISE EXCEPTION USING MESSAGE = 'that activity-rule choice is not allowed';
    END IF;
    SELECT expedition.id
    INTO expected
    FROM expedition_participations AS participation
    JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
    WHERE participation.journey_id = journey.id
      AND participation.user_id = journey.user_id
      AND participation.status = 'active'
      AND expedition.status = 'active'
      AND expedition.route_id = journey.route_id
      AND expedition.activity_policy = 'restricted'
      AND NOT EXISTS (
          SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
          EXCEPT
          SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
      )
      AND NOT EXISTS (
          SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
          EXCEPT
          SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
      )
    LIMIT 1;
    IF expected IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE = 'that activity-rule choice is not allowed';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_policy_choice_consistent
AFTER INSERT OR UPDATE OR DELETE ON adventurer.journey_policy_choices
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_policy_choice_consistent();

CREATE CONSTRAINT TRIGGER journey_policy_choice_consistent_journey
AFTER UPDATE ON adventurer.route_journeys
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.journey_policy_choice_consistent();

ALTER TABLE adventurer.expedition_activity_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.journey_activity_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.journey_policy_choices ENABLE ROW LEVEL SECURITY;

DO $lock$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE adventurer.expedition_activity_types FROM anon;
        REVOKE ALL ON TABLE adventurer.journey_activity_types FROM anon;
        REVOKE ALL ON TABLE adventurer.journey_policy_choices FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE adventurer.expedition_activity_types FROM authenticated;
        REVOKE ALL ON TABLE adventurer.journey_activity_types FROM authenticated;
        REVOKE ALL ON TABLE adventurer.journey_policy_choices FROM authenticated;
    END IF;
END;
$lock$;
