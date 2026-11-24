DROP TRIGGER IF EXISTS journey_credit_origins_protect ON adventurer.journey_credit_origins;

DROP FUNCTION IF EXISTS adventurer.protect_credit_origin();

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
