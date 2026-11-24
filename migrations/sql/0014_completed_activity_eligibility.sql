-- A completed journey keeps the eligibility of every activity assigned to it.
-- Metadata and a type change that stays eligible, or stays ineligible, are allowed.

CREATE OR REPLACE FUNCTION adventurer.protect_completed_activity_eligibility()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    eligibility_changed boolean;
BEGIN
    IF current_setting('adventurer.account_deletion', true) = 'on' THEN
        RETURN NEW;
    END IF;
    IF NEW.activity_type IS NOT DISTINCT FROM OLD.activity_type THEN
        RETURN NEW;
    END IF;
    SELECT EXISTS (
        SELECT 1
        FROM activity_journey_assignments AS assignment
        JOIN route_journeys AS journey ON journey.id = assignment.journey_id
        WHERE assignment.activity_id = OLD.id
          AND journey.status = 'completed'
          AND (
              CASE
                  WHEN journey.activity_policy = 'unrestricted' THEN true
                  ELSE EXISTS (
                      SELECT 1
                      FROM journey_activity_types AS allowed
                      WHERE allowed.journey_id = journey.id
                        AND allowed.activity_type = OLD.activity_type
                  )
              END
          ) IS DISTINCT FROM (
              CASE
                  WHEN journey.activity_policy = 'unrestricted' THEN true
                  ELSE EXISTS (
                      SELECT 1
                      FROM journey_activity_types AS allowed
                      WHERE allowed.journey_id = journey.id
                        AND allowed.activity_type = NEW.activity_type
                  )
              END
          )
    ) INTO eligibility_changed;
    IF eligibility_changed THEN
        RAISE EXCEPTION USING MESSAGE = 'activity eligibility on a completed journey cannot change';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER activities_protect_completed_eligibility
BEFORE UPDATE OF activity_type ON adventurer.activities
FOR EACH ROW
EXECUTE FUNCTION adventurer.protect_completed_activity_eligibility();
