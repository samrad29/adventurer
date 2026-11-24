DROP TRIGGER IF EXISTS journey_credits_require_origin ON adventurer.journey_credits;
DROP TRIGGER IF EXISTS journey_credits_record_origin ON adventurer.journey_credits;
DROP FUNCTION IF EXISTS adventurer.require_credit_origin();
DROP FUNCTION IF EXISTS adventurer.record_credit_origin();
DROP TABLE IF EXISTS adventurer.journey_credit_origins;

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

ALTER TABLE adventurer.activities
    DROP CONSTRAINT activities_type_chk;

ALTER TABLE adventurer.activities
    ADD CONSTRAINT activities_type_chk CHECK (
        activity_type IN (
            'run', 'hike', 'walk', 'stair_climb', 'other', 'unspecified'
        )
    );
