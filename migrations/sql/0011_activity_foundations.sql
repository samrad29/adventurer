-- Release 1 activity foundation. Every journey stays unrestricted.
-- Credits, totals, participations, and snapshots are not rewritten.
-- A credit origin copies journey_credits.created_at and then stays put.

ALTER TABLE adventurer.activities
    DROP CONSTRAINT activities_type_chk;

ALTER TABLE adventurer.activities
    ADD CONSTRAINT activities_type_chk CHECK (
        activity_type IN (
            'run', 'hike', 'walk', 'stair_climb', 'other', 'unspecified',
            'cycle', 'indoor_cycle'
        )
    );

CREATE TABLE adventurer.journey_credit_origins (
    assignment_id bigint NOT NULL,
    metric_code text NOT NULL,
    credited_at timestamptz NOT NULL,
    CONSTRAINT journey_credit_origins_pkey
        PRIMARY KEY (assignment_id, metric_code),
    CONSTRAINT journey_credit_origins_assignment_fkey
        FOREIGN KEY (assignment_id)
        REFERENCES adventurer.activity_journey_assignments (id) ON DELETE CASCADE
);

INSERT INTO adventurer.journey_credit_origins (
    assignment_id, metric_code, credited_at
)
SELECT assignment_id, metric_code, created_at
FROM adventurer.journey_credits;

CREATE OR REPLACE FUNCTION adventurer.record_credit_origin()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    INSERT INTO journey_credit_origins (assignment_id, metric_code, credited_at)
    VALUES (NEW.assignment_id, NEW.metric_code, NEW.created_at)
    ON CONFLICT (assignment_id, metric_code) DO NOTHING;
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER journey_credits_record_origin
AFTER INSERT ON adventurer.journey_credits
FOR EACH ROW
EXECUTE FUNCTION adventurer.record_credit_origin();

CREATE OR REPLACE FUNCTION adventurer.require_credit_origin()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM journey_credit_origins AS origin
        WHERE origin.assignment_id = NEW.assignment_id
          AND origin.metric_code = NEW.metric_code
    ) THEN
        RAISE EXCEPTION USING MESSAGE = 'a journey credit requires an origin';
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE CONSTRAINT TRIGGER journey_credits_require_origin
AFTER INSERT OR UPDATE ON adventurer.journey_credits
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION adventurer.require_credit_origin();

CREATE OR REPLACE FUNCTION adventurer.protect_completed_journey_activity()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM activity_journey_assignments AS assignment
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
            FROM activity_journey_assignments AS assignment
            JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            WHERE assignment.activity_id = OLD.id
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
            FROM activity_journey_assignments AS assignment
            JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            WHERE assignment.activity_id = OLD.id
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

ALTER TABLE adventurer.journey_credit_origins ENABLE ROW LEVEL SECURITY;

DO $lock$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE adventurer.journey_credit_origins FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE adventurer.journey_credit_origins FROM authenticated;
    END IF;
END;
$lock$;
