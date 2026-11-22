-- Elevation-only progress becomes a metric catalog.
--
-- Route targets and checkpoint thresholds move into requirement rows, then
-- the old elevation columns are dropped. Journey credits, expedition result
-- totals, and feed amounts from the single-metric model are discarded.
-- Accounts, routes, checkpoints, the Global Everest expedition, and activity
-- measurements are kept. Nothing is backfilled into the new credit model.
--
-- Route shape checks are constraint triggers, DEFERRABLE INITIALLY DEFERRED.
-- A route, its requirements, and its checkpoints can be written in one
-- transaction; the checks run at commit (or when SET CONSTRAINTS IMMEDIATE
-- is issued). Deleting a route cascades its requirements away before commit,
-- and the check then sees that the route is gone. Once any route_journeys
-- row exists, requirement and checkpoint-threshold changes are rejected
-- immediately, before the deferred check.

CREATE TABLE adventurer.progress_metrics (
    code text PRIMARY KEY,
    storage_unit text NOT NULL,
    display_unit text NOT NULL,
    CONSTRAINT progress_metrics_code_chk CHECK (code IN ('elevation', 'distance')),
    CONSTRAINT progress_metrics_units_chk CHECK (
        (code = 'elevation' AND storage_unit = 'm' AND display_unit = 'ft')
        OR (code = 'distance' AND storage_unit = 'm' AND display_unit = 'mi')
    )
);

INSERT INTO adventurer.progress_metrics (code, storage_unit, display_unit)
VALUES
    ('elevation', 'm', 'ft'),
    ('distance', 'm', 'mi');

CREATE TABLE adventurer.route_requirements (
    route_id bigint NOT NULL,
    metric_code text NOT NULL,
    target_amount numeric(16, 6) NOT NULL,
    CONSTRAINT route_requirements_pkey PRIMARY KEY (route_id, metric_code),
    CONSTRAINT route_requirements_route_id_fkey
        FOREIGN KEY (route_id) REFERENCES adventurer.routes (id) ON DELETE CASCADE,
    CONSTRAINT route_requirements_metric_code_fkey
        FOREIGN KEY (metric_code) REFERENCES adventurer.progress_metrics (code),
    CONSTRAINT route_requirements_target_chk CHECK (target_amount > 0)
);

INSERT INTO adventurer.route_requirements (route_id, metric_code, target_amount)
SELECT id, 'elevation', target_elevation_m
FROM adventurer.routes;

ALTER TABLE adventurer.route_checkpoints
    ADD CONSTRAINT route_checkpoints_id_route_key UNIQUE (id, route_id);

CREATE TABLE adventurer.checkpoint_requirements (
    checkpoint_id bigint NOT NULL,
    route_id bigint NOT NULL,
    metric_code text NOT NULL,
    threshold_amount numeric(16, 6) NOT NULL,
    CONSTRAINT checkpoint_requirements_pkey PRIMARY KEY (checkpoint_id, metric_code),
    CONSTRAINT checkpoint_requirements_checkpoint_route_fkey
        FOREIGN KEY (checkpoint_id, route_id)
        REFERENCES adventurer.route_checkpoints (id, route_id) ON DELETE CASCADE,
    CONSTRAINT checkpoint_requirements_route_metric_fkey
        FOREIGN KEY (route_id, metric_code)
        REFERENCES adventurer.route_requirements (route_id, metric_code) ON DELETE CASCADE,
    CONSTRAINT checkpoint_requirements_threshold_chk CHECK (threshold_amount > 0)
);

CREATE INDEX checkpoint_requirements_route_idx
    ON adventurer.checkpoint_requirements (route_id, metric_code);

INSERT INTO adventurer.checkpoint_requirements (
    checkpoint_id, route_id, metric_code, threshold_amount
)
SELECT id, route_id, 'elevation', elevation_m
FROM adventurer.route_checkpoints;

-- A published route has at least one requirement. Every checkpoint has at
-- least one requirement. A later checkpoint includes every earlier metric
-- at a threshold greater than or equal to the earlier one. The last
-- checkpoint requires every route metric at that metric's target.

CREATE FUNCTION adventurer.assert_route_progress_shape(subject_route_id bigint)
RETURNS void
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    route_status text;
BEGIN
    SELECT status INTO route_status
    FROM routes
    WHERE id = subject_route_id;

    IF route_status IS NULL THEN
        RETURN;
    END IF;

    IF route_status = 'published' AND NOT EXISTS (
        SELECT 1
        FROM route_requirements
        WHERE route_id = subject_route_id
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'a published route needs at least one progress requirement';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM route_checkpoints WHERE route_id = subject_route_id
    ) THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM route_checkpoints AS checkpoint
        WHERE checkpoint.route_id = subject_route_id
          AND NOT EXISTS (
              SELECT 1
              FROM checkpoint_requirements AS requirement
              WHERE requirement.checkpoint_id = checkpoint.id
          )
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'a checkpoint needs at least one progress requirement';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM checkpoint_requirements AS requirement
        JOIN route_requirements AS route_requirement
            ON route_requirement.route_id = requirement.route_id
           AND route_requirement.metric_code = requirement.metric_code
        WHERE requirement.route_id = subject_route_id
          AND requirement.threshold_amount > route_requirement.target_amount
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'a checkpoint threshold cannot exceed the route target';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM route_checkpoints AS earlier
        JOIN route_checkpoints AS later
            ON later.route_id = earlier.route_id
           AND later.position > earlier.position
        JOIN checkpoint_requirements AS earlier_requirement
            ON earlier_requirement.checkpoint_id = earlier.id
        LEFT JOIN checkpoint_requirements AS later_requirement
            ON later_requirement.checkpoint_id = later.id
           AND later_requirement.metric_code = earlier_requirement.metric_code
        WHERE earlier.route_id = subject_route_id
          AND (
              later_requirement.metric_code IS NULL
              OR later_requirement.threshold_amount
                  < earlier_requirement.threshold_amount
          )
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'checkpoint requirements must stay monotonic';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM route_requirements AS route_requirement
        WHERE route_requirement.route_id = subject_route_id
          AND NOT EXISTS (
              SELECT 1
              FROM route_checkpoints AS checkpoint
              JOIN checkpoint_requirements AS requirement
                  ON requirement.checkpoint_id = checkpoint.id
                 AND requirement.metric_code = route_requirement.metric_code
              WHERE checkpoint.route_id = subject_route_id
                AND checkpoint.position = (
                    SELECT max(position)
                    FROM route_checkpoints AS last_checkpoint
                    WHERE last_checkpoint.route_id = subject_route_id
                )
                AND requirement.threshold_amount = route_requirement.target_amount
          )
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'the last checkpoint must require every route metric at its target';
    END IF;
END;
$fn$;

DO $check$
DECLARE
    subject_route_id bigint;
BEGIN
    FOR subject_route_id IN SELECT id FROM adventurer.routes LOOP
        PERFORM adventurer.assert_route_progress_shape(subject_route_id);
    END LOOP;
END
$check$;

ALTER TABLE adventurer.routes
    DROP CONSTRAINT routes_target_elevation_chk,
    DROP COLUMN target_elevation_m;

ALTER TABLE adventurer.route_checkpoints
    DROP CONSTRAINT route_checkpoints_route_elevation_key,
    DROP CONSTRAINT route_checkpoints_elevation_chk,
    DROP COLUMN elevation_m;

-- The completed-journey trigger names elevation_gain_m, so it has to be
-- dropped before that column can change type. It is recreated below for
-- both measurements.
DROP TRIGGER activities_protect_completed_journey ON adventurer.activities;

ALTER TABLE adventurer.activities
    DROP CONSTRAINT activities_elevation_chk,
    DROP CONSTRAINT activities_distance_chk,
    ALTER COLUMN elevation_gain_m TYPE numeric(16, 6),
    ALTER COLUMN elevation_gain_m DROP NOT NULL,
    ALTER COLUMN distance_m TYPE numeric(16, 6),
    ADD CONSTRAINT activities_elevation_chk
        CHECK (elevation_gain_m IS NULL OR elevation_gain_m >= 0),
    ADD CONSTRAINT activities_distance_chk
        CHECK (distance_m IS NULL OR distance_m >= 0);

-- Obsolete single-metric snapshots cannot be split into metric rows without
-- inventing them. The immutability triggers are disabled only for this
-- cutover and re-enabled in the same transaction. Journey credits are
-- removed by replacing the table, which does not fire their row trigger.

ALTER TABLE adventurer.expedition_updates
    DISABLE TRIGGER expedition_updates_immutable;
ALTER TABLE adventurer.expedition_result_entries
    DISABLE TRIGGER expedition_result_entries_immutable;

DELETE FROM adventurer.expedition_updates;
DELETE FROM adventurer.expedition_result_entries;

ALTER TABLE adventurer.expedition_updates
    ENABLE TRIGGER expedition_updates_immutable;
ALTER TABLE adventurer.expedition_result_entries
    ENABLE TRIGGER expedition_result_entries_immutable;

DROP TABLE adventurer.journey_credits;

CREATE TABLE adventurer.activity_journey_assignments (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    activity_id bigint NOT NULL,
    journey_id bigint NOT NULL,
    user_id bigint NOT NULL,
    route_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT activity_journey_assignments_activity_key UNIQUE (activity_id),
    CONSTRAINT activity_journey_assignments_id_journey_route_key
        UNIQUE (id, journey_id, route_id),
    CONSTRAINT activity_journey_assignments_activity_user_fkey
        FOREIGN KEY (activity_id, user_id)
        REFERENCES adventurer.activities (id, user_id) ON DELETE CASCADE,
    CONSTRAINT activity_journey_assignments_journey_user_route_fkey
        FOREIGN KEY (journey_id, user_id, route_id)
        REFERENCES adventurer.route_journeys (id, user_id, route_id) ON DELETE CASCADE
);

CREATE INDEX activity_journey_assignments_journey_idx
    ON adventurer.activity_journey_assignments (journey_id);

CREATE TABLE adventurer.journey_credits (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    assignment_id bigint NOT NULL,
    journey_id bigint NOT NULL,
    route_id bigint NOT NULL,
    metric_code text NOT NULL,
    credited_amount numeric(16, 6) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT journey_credits_assignment_metric_key
        UNIQUE (assignment_id, metric_code),
    CONSTRAINT journey_credits_assignment_journey_route_fkey
        FOREIGN KEY (assignment_id, journey_id, route_id)
        REFERENCES adventurer.activity_journey_assignments (id, journey_id, route_id)
        ON DELETE CASCADE,
    CONSTRAINT journey_credits_route_metric_fkey
        FOREIGN KEY (route_id, metric_code)
        REFERENCES adventurer.route_requirements (route_id, metric_code),
    CONSTRAINT journey_credits_amount_chk CHECK (credited_amount > 0)
);

CREATE INDEX journey_credits_journey_metric_idx
    ON adventurer.journey_credits (journey_id, metric_code);

ALTER TABLE adventurer.expedition_result_entries
    DROP CONSTRAINT expedition_result_entries_total_chk,
    DROP CONSTRAINT expedition_result_entries_rank_chk,
    DROP COLUMN total_m,
    DROP COLUMN rank,
    ADD COLUMN final_rank integer,
    ADD CONSTRAINT expedition_result_entries_final_rank_chk
        CHECK (final_rank IS NULL OR final_rank >= 1);

CREATE TABLE adventurer.expedition_result_metrics (
    result_entry_id bigint NOT NULL,
    metric_code text NOT NULL,
    total_amount numeric(16, 6) NOT NULL,
    CONSTRAINT expedition_result_metrics_pkey
        PRIMARY KEY (result_entry_id, metric_code),
    CONSTRAINT expedition_result_metrics_result_entry_id_fkey
        FOREIGN KEY (result_entry_id)
        REFERENCES adventurer.expedition_result_entries (id) ON DELETE CASCADE,
    CONSTRAINT expedition_result_metrics_metric_code_fkey
        FOREIGN KEY (metric_code) REFERENCES adventurer.progress_metrics (code),
    CONSTRAINT expedition_result_metrics_total_chk CHECK (total_amount >= 0)
);

ALTER TABLE adventurer.expedition_updates
    DROP CONSTRAINT expedition_updates_elevation_chk,
    DROP COLUMN elevation_m;

CREATE TABLE adventurer.expedition_update_metrics (
    update_id bigint NOT NULL,
    metric_code text NOT NULL,
    credited_amount numeric(16, 6) NOT NULL,
    CONSTRAINT expedition_update_metrics_pkey PRIMARY KEY (update_id, metric_code),
    CONSTRAINT expedition_update_metrics_update_id_fkey
        FOREIGN KEY (update_id)
        REFERENCES adventurer.expedition_updates (id) ON DELETE CASCADE,
    CONSTRAINT expedition_update_metrics_metric_code_fkey
        FOREIGN KEY (metric_code) REFERENCES adventurer.progress_metrics (code),
    CONSTRAINT expedition_update_metrics_amount_chk CHECK (credited_amount > 0)
);

CREATE FUNCTION adventurer.enforce_route_progress_shape()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    subject_route_id bigint;
    previous_route_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'routes' THEN
        subject_route_id := NEW.id;
        previous_route_id := NULL;
    ELSIF TG_OP = 'DELETE' THEN
        subject_route_id := OLD.route_id;
        previous_route_id := NULL;
    ELSIF TG_OP = 'UPDATE' THEN
        subject_route_id := NEW.route_id;
        previous_route_id := OLD.route_id;
    ELSE
        subject_route_id := NEW.route_id;
        previous_route_id := NULL;
    END IF;

    PERFORM assert_route_progress_shape(subject_route_id);
    IF previous_route_id IS NOT NULL
       AND previous_route_id IS DISTINCT FROM subject_route_id THEN
        PERFORM assert_route_progress_shape(previous_route_id);
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER route_progress_shape_routes
AFTER INSERT OR UPDATE OF status ON adventurer.routes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.enforce_route_progress_shape();

CREATE CONSTRAINT TRIGGER route_progress_shape_requirements
AFTER INSERT OR UPDATE OR DELETE ON adventurer.route_requirements
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.enforce_route_progress_shape();

CREATE CONSTRAINT TRIGGER route_progress_shape_checkpoints
AFTER INSERT OR UPDATE OR DELETE ON adventurer.route_checkpoints
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.enforce_route_progress_shape();

CREATE CONSTRAINT TRIGGER route_progress_shape_checkpoint_requirements
AFTER INSERT OR UPDATE OR DELETE ON adventurer.checkpoint_requirements
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.enforce_route_progress_shape();

CREATE FUNCTION adventurer.reject_progress_change_after_journey()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    subject_route_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'route_checkpoints' THEN
        subject_route_id := OLD.route_id;
    ELSE
        subject_route_id := COALESCE(NEW.route_id, OLD.route_id);
    END IF;

    IF EXISTS (
        SELECT 1 FROM route_journeys WHERE route_id = subject_route_id
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'route requirements are immutable once a journey exists';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER route_requirements_immutable
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.route_requirements
FOR EACH ROW
EXECUTE FUNCTION adventurer.reject_progress_change_after_journey();

CREATE TRIGGER checkpoint_requirements_immutable
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.checkpoint_requirements
FOR EACH ROW
EXECUTE FUNCTION adventurer.reject_progress_change_after_journey();

CREATE TRIGGER route_checkpoints_position_immutable
BEFORE UPDATE OF position ON adventurer.route_checkpoints
FOR EACH ROW
EXECUTE FUNCTION adventurer.reject_progress_change_after_journey();

CREATE FUNCTION adventurer.protect_journey_assignments()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    journey_status text;
    subject_user_id bigint;
    subject_journey_id bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING MESSAGE = 'an activity assignment cannot be changed';
    END IF;

    IF TG_OP = 'DELETE' THEN
        subject_journey_id := OLD.journey_id;
        subject_user_id := OLD.user_id;
    ELSE
        subject_journey_id := NEW.journey_id;
        subject_user_id := NEW.user_id;
    END IF;

    SELECT status INTO journey_status
    FROM route_journeys
    WHERE id = subject_journey_id;

    IF journey_status = 'completed' THEN
        IF TG_OP = 'DELETE' AND journey_credit_deletion_allowed(subject_user_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION USING MESSAGE =
            'an assignment on a completed journey cannot be changed';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER activity_journey_assignments_protect
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.activity_journey_assignments
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_journey_assignments();

CREATE OR REPLACE FUNCTION adventurer.protect_journey_credits()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    journey_status text;
    subject_user_id bigint;
    activity_amount numeric(16, 6);
BEGIN
    SELECT journey.status, assignment.user_id
    INTO journey_status, subject_user_id
    FROM activity_journey_assignments AS assignment
    JOIN route_journeys AS journey ON journey.id = assignment.journey_id
    WHERE assignment.id = COALESCE(NEW.assignment_id, OLD.assignment_id);

    IF journey_status = 'completed' THEN
        IF TG_OP = 'DELETE' AND journey_credit_deletion_allowed(subject_user_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION USING MESSAGE =
            'credits on a completed journey cannot be changed';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    SELECT
        CASE NEW.metric_code
            WHEN 'elevation' THEN activity.elevation_gain_m
            WHEN 'distance' THEN activity.distance_m
            ELSE NULL
        END
    INTO activity_amount
    FROM activities AS activity
    JOIN activity_journey_assignments AS assignment
        ON assignment.activity_id = activity.id
    WHERE assignment.id = NEW.assignment_id;

    IF activity_amount IS NULL OR NEW.credited_amount > activity_amount THEN
        RAISE EXCEPTION USING MESSAGE =
            'credited amount cannot exceed the activity measurement';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER journey_credits_protect
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.journey_credits
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_journey_credits();

CREATE FUNCTION adventurer.assert_credit_within_target()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    credited_total numeric(16, 6);
    target_amount numeric(16, 6);
BEGIN
    SELECT route_requirement.target_amount INTO target_amount
    FROM route_requirements AS route_requirement
    WHERE route_requirement.route_id = NEW.route_id
      AND route_requirement.metric_code = NEW.metric_code;

    SELECT COALESCE(SUM(credit.credited_amount), 0) INTO credited_total
    FROM journey_credits AS credit
    WHERE credit.journey_id = NEW.journey_id
      AND credit.metric_code = NEW.metric_code;

    IF credited_total > target_amount THEN
        RAISE EXCEPTION USING MESSAGE = 'credit exceeds the route target';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_credits_within_target
AFTER INSERT OR UPDATE ON adventurer.journey_credits
DEFERRABLE INITIALLY IMMEDIATE
FOR EACH ROW
EXECUTE FUNCTION adventurer.assert_credit_within_target();

CREATE OR REPLACE FUNCTION adventurer.protect_completed_journey_activity()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            WHERE assignment.activity_id = OLD.id
              AND journey.status = 'completed'
        ) AND NOT journey_credit_deletion_allowed(OLD.user_id) THEN
            RAISE EXCEPTION USING MESSAGE =
                'an activity credited to a completed journey cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;

    IF NEW.elevation_gain_m IS DISTINCT FROM OLD.elevation_gain_m THEN
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            WHERE assignment.activity_id = OLD.id
              AND credit.metric_code = 'elevation'
              AND journey.status = 'completed'
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'elevation credited to a completed journey cannot change';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.activity_id = OLD.id
              AND credit.metric_code = 'elevation'
              AND (
                  NEW.elevation_gain_m IS NULL
                  OR credit.credited_amount > NEW.elevation_gain_m
              )
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'activity elevation cannot fall below the credited amount';
        END IF;
    END IF;

    IF NEW.distance_m IS DISTINCT FROM OLD.distance_m THEN
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            WHERE assignment.activity_id = OLD.id
              AND credit.metric_code = 'distance'
              AND journey.status = 'completed'
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'distance credited to a completed journey cannot change';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.activity_id = OLD.id
              AND credit.metric_code = 'distance'
              AND (
                  NEW.distance_m IS NULL
                  OR credit.credited_amount > NEW.distance_m
              )
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'activity distance cannot fall below the credited amount';
        END IF;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER activities_protect_completed_journey
BEFORE UPDATE OF elevation_gain_m, distance_m OR DELETE ON adventurer.activities
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_journey_activity();

CREATE FUNCTION adventurer.protect_result_metrics()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    subject_user_id bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    END IF;

    -- Updates are never allowed. A cascade from the parent runs after that
    -- parent's delete trigger has already allowed the deletion, and the
    -- parent row is no longer visible.
    IF TG_OP = 'DELETE' THEN
        SELECT entry.user_id INTO subject_user_id
        FROM expedition_result_entries AS entry
        WHERE entry.id = OLD.result_entry_id;

        IF NOT FOUND OR expedition_history_delete_allowed(subject_user_id) THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION USING MESSAGE = 'expedition result metrics are immutable';
END;
$fn$;

CREATE TRIGGER expedition_result_metrics_immutable
BEFORE UPDATE OR DELETE ON adventurer.expedition_result_metrics
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_result_metrics();

CREATE FUNCTION adventurer.protect_update_metrics()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    subject_user_id bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT feed.user_id INTO subject_user_id
        FROM expedition_updates AS feed
        WHERE feed.id = OLD.update_id;

        IF NOT FOUND OR expedition_history_delete_allowed(subject_user_id) THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION USING MESSAGE = 'expedition update metrics are immutable';
END;
$fn$;

CREATE TRIGGER expedition_update_metrics_immutable
BEFORE UPDATE OR DELETE ON adventurer.expedition_update_metrics
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_update_metrics();

-- The legacy validator still rejects a backfill. Credits no longer carry
-- activity_id, so the proof follows the assignment.

CREATE OR REPLACE FUNCTION adventurer.assert_phase1_legacy_match()
RETURNS void
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    mismatch text;
BEGIN
    IF to_regclass('adventurer.activity_credits') IS NOT NULL THEN
        RAISE EXCEPTION 'activity_credits is still present';
    END IF;
    IF (SELECT COUNT(*) FROM adventurers) <> (SELECT COUNT(*) FROM users) THEN
        RAISE EXCEPTION 'user count mismatch';
    END IF;
    IF (SELECT COUNT(*) FROM user_profiles) <> (SELECT COUNT(*) FROM users) THEN
        RAISE EXCEPTION 'profile count does not match user count';
    END IF;
    IF (SELECT COUNT(*) FROM logs) <> (SELECT COUNT(*) FROM activities) THEN
        RAISE EXCEPTION 'activity count mismatch';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activity_journey_assignments AS assignment
            ON assignment.activity_id = legacy_log.id
    ) THEN
        RAISE EXCEPTION 'legacy activities were backfilled into journey credits';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM expeditions
        WHERE slug = 'everest'
          AND name = 'Global Everest Challenge'
          AND visibility = 'public'
          AND status = 'active'
          AND owner_user_id IS NULL
          AND kind = 'global'
          AND organization_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Global Everest identity was not preserved';
    END IF;

    IF EXISTS (
        SELECT id FROM adventurers
        EXCEPT
        SELECT id FROM users
    ) OR EXISTS (
        SELECT id FROM users
        EXCEPT
        SELECT id FROM adventurers
    ) THEN
        RAISE EXCEPTION 'user ids were not preserved';
    END IF;

    IF EXISTS (
        SELECT id FROM logs
        EXCEPT
        SELECT id FROM activities
    ) OR EXISTS (
        SELECT id FROM activities
        EXCEPT
        SELECT id FROM logs
    ) THEN
        RAISE EXCEPTION 'activity ids were not preserved';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        JOIN users AS account ON account.id = legacy_account.id
        WHERE account.username IS DISTINCT FROM legacy_account.name
           OR account.username_key IS DISTINCT FROM legacy_account.name_key
           OR account.password_hash IS DISTINCT FROM legacy_account.password_hash
           OR account.created_at IS DISTINCT FROM legacy_account.created_at::timestamptz
    ) THEN
        RAISE EXCEPTION 'user columns were not copied exactly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        JOIN user_profiles AS profile ON profile.user_id = legacy_account.id
        WHERE profile.display_name IS DISTINCT FROM legacy_account.name
           OR profile.unit_system IS DISTINCT FROM 'imperial'
    ) THEN
        RAISE EXCEPTION 'profiles were not copied from account names';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        WHERE activity.user_id IS DISTINCT FROM legacy_log.adventurer_id
           OR activity.activity_date IS DISTINCT FROM legacy_log.happened_on::date
           OR activity.note IS DISTINCT FROM legacy_log.note
           OR activity.created_at IS DISTINCT FROM legacy_log.created_at::timestamptz
           OR activity.source IS DISTINCT FROM 'manual'
           OR activity.activity_type IS DISTINCT FROM 'unspecified'
           OR activity.verification_status IS DISTINCT FROM 'not_required'
           OR activity.started_at IS NOT NULL
           OR activity.ended_at IS NOT NULL
           OR activity.duration_s IS NOT NULL
           OR activity.external_id IS NOT NULL
           OR activity.elevation_gain_m IS DISTINCT FROM
                (legacy_log.elevation_ft::numeric * 0.3048)::numeric(12, 5)
           OR activity.distance_m IS DISTINCT FROM (
                CASE
                    WHEN legacy_log.distance_km IS NULL THEN NULL
                    ELSE (legacy_log.distance_km::numeric * 1000)::numeric(14, 3)
                END
           )
    ) THEN
        RAISE EXCEPTION 'activity columns were not copied exactly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        WHERE abs(
            legacy_log.elevation_ft::numeric - (activity.elevation_gain_m / 0.3048)
        ) > 0.00002
    ) THEN
        RAISE EXCEPTION 'an activity elevation does not round-trip to its log';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM activity_verification_events AS event
        JOIN logs AS legacy_log ON legacy_log.id = event.activity_id
        WHERE event.from_status IS NOT NULL
           OR event.to_status IS DISTINCT FROM 'not_required'
           OR event.actor_type IS DISTINCT FROM 'system'
           OR event.actor_user_id IS NOT NULL
    ) OR EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        WHERE (
            SELECT COUNT(*)
            FROM activity_verification_events AS event
            WHERE event.activity_id = legacy_log.id
        ) <> 1
    ) THEN
        RAISE EXCEPTION 'migrated verification events are not the manual opening event';
    END IF;

    WITH legacy AS (
        SELECT
            adventurer_id AS user_id,
            COUNT(*) AS activity_count,
            SUM(elevation_ft::numeric) AS elevation_ft
        FROM logs
        GROUP BY adventurer_id
    ),
    migrated AS (
        SELECT
            activity.user_id,
            COUNT(*) AS activity_count,
            SUM(activity.elevation_gain_m) / 0.3048 AS elevation_ft
        FROM activities AS activity
        GROUP BY activity.user_id
    )
    SELECT string_agg(
        'user ' || COALESCE(legacy.user_id, migrated.user_id)::text,
        ', '
    )
    INTO mismatch
    FROM legacy
    FULL OUTER JOIN migrated ON migrated.user_id = legacy.user_id
    WHERE legacy.activity_count IS DISTINCT FROM migrated.activity_count
       OR abs(COALESCE(legacy.elevation_ft, 0) - COALESCE(migrated.elevation_ft, 0))
          > GREATEST(COALESCE(legacy.activity_count, migrated.activity_count), 1) * 0.00002;

    IF mismatch IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'activity totals differ by user: ' || mismatch;
    END IF;

    IF (SELECT COUNT(*) FROM users) > 0 THEN
        IF (SELECT last_value FROM users_id_seq) <> (SELECT MAX(id) FROM users)
           OR (SELECT is_called FROM users_id_seq) IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'users identity sequence was not set to the preserved max id';
        END IF;
    ELSIF (SELECT is_called FROM users_id_seq) THEN
        RAISE EXCEPTION 'users identity sequence must stay unused when there are no users';
    END IF;

    IF (SELECT COUNT(*) FROM activities) > 0 THEN
        IF (SELECT last_value FROM activities_id_seq) <> (SELECT MAX(id) FROM activities)
           OR (SELECT is_called FROM activities_id_seq) IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'activities identity sequence was not set to the preserved max id';
        END IF;
    ELSIF (SELECT is_called FROM activities_id_seq) THEN
        RAISE EXCEPTION 'activities identity sequence must stay unused when there are no activities';
    END IF;
END;
$fn$;

ALTER TABLE adventurer.progress_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.route_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.checkpoint_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.activity_journey_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.journey_credits ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.expedition_result_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.expedition_update_metrics ENABLE ROW LEVEL SECURITY;

DO $lock$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE adventurer.progress_metrics FROM anon;
        REVOKE ALL ON TABLE adventurer.route_requirements FROM anon;
        REVOKE ALL ON TABLE adventurer.checkpoint_requirements FROM anon;
        REVOKE ALL ON TABLE adventurer.activity_journey_assignments FROM anon;
        REVOKE ALL ON TABLE adventurer.journey_credits FROM anon;
        REVOKE ALL ON TABLE adventurer.expedition_result_metrics FROM anon;
        REVOKE ALL ON TABLE adventurer.expedition_update_metrics FROM anon;
        REVOKE ALL ON SEQUENCE adventurer.activity_journey_assignments_id_seq FROM anon;
        REVOKE ALL ON SEQUENCE adventurer.journey_credits_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE adventurer.progress_metrics FROM authenticated;
        REVOKE ALL ON TABLE adventurer.route_requirements FROM authenticated;
        REVOKE ALL ON TABLE adventurer.checkpoint_requirements FROM authenticated;
        REVOKE ALL ON TABLE adventurer.activity_journey_assignments FROM authenticated;
        REVOKE ALL ON TABLE adventurer.journey_credits FROM authenticated;
        REVOKE ALL ON TABLE adventurer.expedition_result_metrics FROM authenticated;
        REVOKE ALL ON TABLE adventurer.expedition_update_metrics FROM authenticated;
        REVOKE ALL ON SEQUENCE adventurer.activity_journey_assignments_id_seq FROM authenticated;
        REVOKE ALL ON SEQUENCE adventurer.journey_credits_id_seq FROM authenticated;
    END IF;
END
$lock$;
