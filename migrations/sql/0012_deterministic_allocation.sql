-- Release 1B origin integrity. Credits, totals, and snapshots are not rewritten.
-- A missing origin takes the assignment time. An existing origin stays put.
-- An origin cannot change or disappear while its assignment remains.

CREATE OR REPLACE FUNCTION adventurer.record_credit_origin()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    assigned_at timestamptz;
BEGIN
    SELECT created_at INTO assigned_at
    FROM activity_journey_assignments
    WHERE id = NEW.assignment_id;
    IF assigned_at IS NULL THEN
        RAISE EXCEPTION USING MESSAGE = 'a journey credit requires an assignment';
    END IF;
    INSERT INTO journey_credit_origins (assignment_id, metric_code, credited_at)
    VALUES (NEW.assignment_id, NEW.metric_code, assigned_at)
    ON CONFLICT (assignment_id, metric_code) DO NOTHING;
    RETURN NULL;
END;
$fn$;

CREATE OR REPLACE FUNCTION adventurer.protect_credit_origin()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION USING MESSAGE = 'a credit origin cannot be changed';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM activity_journey_assignments
        WHERE id = OLD.assignment_id
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'a credit origin cannot be deleted while its assignment remains';
    END IF;
    RETURN OLD;
END;
$fn$;

DROP TRIGGER IF EXISTS journey_credit_origins_protect ON adventurer.journey_credit_origins;

CREATE TRIGGER journey_credit_origins_protect
BEFORE UPDATE OR DELETE ON adventurer.journey_credit_origins
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_credit_origin();
