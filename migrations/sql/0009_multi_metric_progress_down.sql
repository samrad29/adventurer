-- Restore the elevation-only columns only when that is a pure reversal.
-- New credits, snapshots, mixed requirements, and measurements the old
-- checks cannot store are not rebuilt and not discarded here.

DO $refuse$
DECLARE
    totals_remain boolean := false;
BEGIN
    -- A database upgraded by the first version of this revision has no totals
    -- table. Dynamic SQL keeps that downgrade from failing at parse time.
    IF to_regclass('adventurer.journey_metric_totals') IS NOT NULL THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM adventurer.journey_metric_totals)'
            INTO totals_remain;
    END IF;

    IF EXISTS (SELECT 1 FROM adventurer.activity_journey_assignments)
        OR EXISTS (SELECT 1 FROM adventurer.journey_credits)
        OR totals_remain
        OR EXISTS (SELECT 1 FROM adventurer.expedition_result_metrics)
        OR EXISTS (SELECT 1 FROM adventurer.expedition_update_metrics)
        OR EXISTS (SELECT 1 FROM adventurer.expedition_result_entries)
        OR EXISTS (SELECT 1 FROM adventurer.expedition_updates)
        OR EXISTS (
            SELECT 1
            FROM adventurer.route_requirements
            WHERE metric_code <> 'elevation'
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.checkpoint_requirements
            WHERE metric_code <> 'elevation'
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.routes AS route
            WHERE NOT EXISTS (
                SELECT 1
                FROM adventurer.route_requirements AS requirement
                WHERE requirement.route_id = route.id
                  AND requirement.metric_code = 'elevation'
            )
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.route_checkpoints AS checkpoint
            WHERE NOT EXISTS (
                SELECT 1
                FROM adventurer.checkpoint_requirements AS requirement
                WHERE requirement.checkpoint_id = checkpoint.id
                  AND requirement.metric_code = 'elevation'
            )
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.route_requirements
            WHERE metric_code = 'elevation'
              AND target_amount IS DISTINCT FROM target_amount::numeric(12, 5)
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.checkpoint_requirements
            WHERE metric_code = 'elevation'
              AND threshold_amount IS DISTINCT FROM threshold_amount::numeric(12, 5)
        )
        OR EXISTS (
            SELECT 1
            FROM adventurer.activities
            WHERE elevation_gain_m IS NULL
               OR elevation_gain_m <= 0
               OR elevation_gain_m IS DISTINCT FROM elevation_gain_m::numeric(12, 5)
               OR (
                   distance_m IS NOT NULL
                   AND (
                       distance_m <= 0
                       OR distance_m IS DISTINCT FROM distance_m::numeric(14, 3)
                   )
               )
        )
    THEN
        RAISE EXCEPTION USING MESSAGE =
            'multi-metric progress cannot be downgraded once new measurements or credits exist';
    END IF;
END
$refuse$;

DROP TRIGGER IF EXISTS route_progress_shape_routes ON adventurer.routes;
DROP TRIGGER IF EXISTS route_progress_shape_requirements ON adventurer.route_requirements;
DROP TRIGGER IF EXISTS route_progress_shape_checkpoints ON adventurer.route_checkpoints;
DROP TRIGGER IF EXISTS route_progress_shape_checkpoint_requirements
    ON adventurer.checkpoint_requirements;
DROP TRIGGER IF EXISTS route_requirements_immutable ON adventurer.route_requirements;
DROP TRIGGER IF EXISTS checkpoint_requirements_immutable
    ON adventurer.checkpoint_requirements;
DROP TRIGGER IF EXISTS route_checkpoints_position_immutable ON adventurer.route_checkpoints;
DROP TRIGGER IF EXISTS activity_journey_assignments_protect
    ON adventurer.activity_journey_assignments;
DROP TRIGGER IF EXISTS journey_credits_protect ON adventurer.journey_credits;
DROP TRIGGER IF EXISTS journey_credits_within_target ON adventurer.journey_credits;
DROP TRIGGER IF EXISTS expedition_result_entries_open_metrics
    ON adventurer.expedition_result_entries;
DROP TRIGGER IF EXISTS expedition_updates_open_metrics
    ON adventurer.expedition_updates;
DROP TRIGGER IF EXISTS expedition_result_entries_stamp_xid
    ON adventurer.expedition_result_entries;
DROP TRIGGER IF EXISTS expedition_updates_stamp_xid
    ON adventurer.expedition_updates;
DROP TRIGGER IF EXISTS activities_protect_completed_journey ON adventurer.activities;
DROP TRIGGER IF EXISTS expedition_result_metrics_immutable
    ON adventurer.expedition_result_metrics;
DROP TRIGGER IF EXISTS expedition_update_metrics_immutable
    ON adventurer.expedition_update_metrics;

DROP FUNCTION IF EXISTS adventurer.enforce_route_progress_shape();
DROP FUNCTION IF EXISTS adventurer.assert_route_progress_shape(bigint);
DROP FUNCTION IF EXISTS adventurer.reject_progress_change_after_journey();
DROP FUNCTION IF EXISTS adventurer.protect_journey_assignments();
DROP FUNCTION IF EXISTS adventurer.assert_credit_within_target();
DROP FUNCTION IF EXISTS adventurer.adjust_credit_total();
DROP FUNCTION IF EXISTS adventurer.apply_credit_delta(bigint, bigint, text, numeric);
DROP FUNCTION IF EXISTS adventurer.credit_key_before(bigint, text, bigint, text);
DROP FUNCTION IF EXISTS adventurer.note_open_snapshot();
DROP FUNCTION IF EXISTS adventurer.snapshot_metrics_are_open(text, bigint);
DROP FUNCTION IF EXISTS adventurer.stamp_snapshot_transaction();
DROP FUNCTION IF EXISTS adventurer.protect_result_metrics();
DROP FUNCTION IF EXISTS adventurer.protect_update_metrics();

ALTER TABLE adventurer.activities
    DROP CONSTRAINT activities_elevation_chk,
    DROP CONSTRAINT activities_distance_chk;

ALTER TABLE adventurer.activities
    ALTER COLUMN elevation_gain_m SET NOT NULL,
    ALTER COLUMN elevation_gain_m TYPE numeric(12, 5),
    ALTER COLUMN distance_m TYPE numeric(14, 3);

ALTER TABLE adventurer.activities
    ADD CONSTRAINT activities_elevation_chk CHECK (elevation_gain_m > 0),
    ADD CONSTRAINT activities_distance_chk CHECK (distance_m IS NULL OR distance_m > 0);

ALTER TABLE adventurer.routes
    ADD COLUMN target_elevation_m numeric(12, 5);

UPDATE adventurer.routes AS route
SET target_elevation_m = requirement.target_amount
FROM adventurer.route_requirements AS requirement
WHERE requirement.route_id = route.id
  AND requirement.metric_code = 'elevation';

ALTER TABLE adventurer.routes
    ALTER COLUMN target_elevation_m SET NOT NULL,
    ADD CONSTRAINT routes_target_elevation_chk CHECK (target_elevation_m > 0);

ALTER TABLE adventurer.route_checkpoints
    ADD COLUMN elevation_m numeric(12, 5);

UPDATE adventurer.route_checkpoints AS checkpoint
SET elevation_m = requirement.threshold_amount
FROM adventurer.checkpoint_requirements AS requirement
WHERE requirement.checkpoint_id = checkpoint.id
  AND requirement.metric_code = 'elevation';

ALTER TABLE adventurer.route_checkpoints
    ALTER COLUMN elevation_m SET NOT NULL;

DROP TABLE IF EXISTS adventurer.journey_metric_totals;
DROP TABLE adventurer.journey_credits;
DROP TABLE adventurer.activity_journey_assignments;
DROP TABLE adventurer.checkpoint_requirements;
DROP TABLE adventurer.route_requirements;
DROP TABLE adventurer.expedition_result_metrics;
DROP TABLE adventurer.expedition_update_metrics;
DROP TABLE adventurer.progress_metrics;

ALTER TABLE adventurer.route_checkpoints
    ADD CONSTRAINT route_checkpoints_elevation_chk CHECK (elevation_m > 0),
    ADD CONSTRAINT route_checkpoints_route_elevation_key UNIQUE (route_id, elevation_m),
    DROP CONSTRAINT route_checkpoints_id_route_key;

CREATE TABLE adventurer.journey_credits (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    activity_id bigint NOT NULL,
    journey_id bigint NOT NULL,
    user_id bigint NOT NULL,
    credited_elevation_m numeric(12, 5) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT journey_credits_activity_key UNIQUE (activity_id),
    CONSTRAINT journey_credits_activity_user_fkey
        FOREIGN KEY (activity_id, user_id)
        REFERENCES adventurer.activities (id, user_id) ON DELETE CASCADE,
    CONSTRAINT journey_credits_journey_user_fkey
        FOREIGN KEY (journey_id, user_id)
        REFERENCES adventurer.route_journeys (id, user_id) ON DELETE CASCADE,
    CONSTRAINT journey_credits_elevation_chk CHECK (credited_elevation_m > 0)
);

CREATE INDEX journey_credits_journey_idx
    ON adventurer.journey_credits (journey_id);

CREATE OR REPLACE FUNCTION adventurer.protect_journey_credits()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    journey_status text;
    activity_elevation numeric(12, 5);
    subject_journey_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        subject_journey_id := OLD.journey_id;
    ELSE
        subject_journey_id := NEW.journey_id;
    END IF;

    SELECT status INTO journey_status
    FROM route_journeys
    WHERE id = subject_journey_id;

    IF journey_status = 'completed' THEN
        IF TG_OP = 'DELETE' AND journey_credit_deletion_allowed(OLD.user_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION USING MESSAGE =
            'credits on a completed journey cannot be changed';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;

    SELECT elevation_gain_m INTO activity_elevation
    FROM activities
    WHERE id = NEW.activity_id;

    IF activity_elevation IS NULL OR NEW.credited_elevation_m > activity_elevation THEN
        RAISE EXCEPTION USING MESSAGE =
            'credited elevation cannot exceed the activity elevation';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER journey_credits_protect
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.journey_credits
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_journey_credits();

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
            JOIN route_journeys AS journey ON journey.id = credit.journey_id
            WHERE credit.activity_id = OLD.id
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
            JOIN route_journeys AS journey ON journey.id = credit.journey_id
            WHERE credit.activity_id = OLD.id
              AND journey.status = 'completed'
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'elevation credited to a completed journey cannot change';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM journey_credits AS credit
            WHERE credit.activity_id = OLD.id
              AND credit.credited_elevation_m > NEW.elevation_gain_m
        ) THEN
            RAISE EXCEPTION USING MESSAGE =
                'activity elevation cannot fall below the credited amount';
        END IF;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER activities_protect_completed_journey
BEFORE UPDATE OF elevation_gain_m OR DELETE ON adventurer.activities
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_journey_activity();

ALTER TABLE adventurer.expedition_result_entries
    DROP CONSTRAINT expedition_result_entries_final_rank_chk,
    DROP COLUMN final_rank,
    DROP COLUMN IF EXISTS creating_xid,
    ADD COLUMN total_m numeric(12, 5) NOT NULL,
    ADD COLUMN rank integer NOT NULL,
    ADD CONSTRAINT expedition_result_entries_total_chk CHECK (total_m >= 0),
    ADD CONSTRAINT expedition_result_entries_rank_chk CHECK (rank >= 1);

ALTER TABLE adventurer.expedition_updates
    DROP COLUMN IF EXISTS creating_xid,
    ADD COLUMN elevation_m numeric(12, 5) NOT NULL,
    ADD CONSTRAINT expedition_updates_elevation_chk CHECK (elevation_m >= 0);

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
        JOIN journey_credits AS credit ON credit.activity_id = legacy_log.id
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

ALTER TABLE adventurer.journey_credits ENABLE ROW LEVEL SECURITY;

DO $lock$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE adventurer.journey_credits FROM anon;
        REVOKE ALL ON SEQUENCE adventurer.journey_credits_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE adventurer.journey_credits FROM authenticated;
        REVOKE ALL ON SEQUENCE adventurer.journey_credits_id_seq FROM authenticated;
    END IF;
END
$lock$;
