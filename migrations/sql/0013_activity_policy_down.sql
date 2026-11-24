DROP TRIGGER IF EXISTS journey_policy_choice_consistent_journey ON adventurer.route_journeys;
DROP TRIGGER IF EXISTS journey_policy_choice_consistent ON adventurer.journey_policy_choices;
DROP TRIGGER IF EXISTS journey_governor_consistent_expeditions ON adventurer.expeditions;
DROP TRIGGER IF EXISTS journey_governor_consistent_participations ON adventurer.expedition_participations;
DROP TRIGGER IF EXISTS journey_governor_consistent_types ON adventurer.journey_activity_types;
DROP TRIGGER IF EXISTS journey_governor_consistent ON adventurer.route_journeys;
DROP TRIGGER IF EXISTS journey_policy_choices_protect_completed ON adventurer.journey_policy_choices;
DROP TRIGGER IF EXISTS journey_activity_types_protect_completed ON adventurer.journey_activity_types;
DROP TRIGGER IF EXISTS route_journeys_protect_completed_policy ON adventurer.route_journeys;
DROP TRIGGER IF EXISTS journey_activity_types_nonempty ON adventurer.journey_activity_types;
DROP TRIGGER IF EXISTS journey_activity_policy_nonempty ON adventurer.route_journeys;
DROP TRIGGER IF EXISTS expedition_activity_types_nonempty ON adventurer.expedition_activity_types;
DROP TRIGGER IF EXISTS expedition_activity_policy_nonempty ON adventurer.expeditions;
DROP TRIGGER IF EXISTS expedition_activity_types_protect ON adventurer.expedition_activity_types;
DROP TRIGGER IF EXISTS expeditions_protect_activity_policy ON adventurer.expeditions;
DROP TRIGGER IF EXISTS expeditions_note_delete ON adventurer.expeditions;

DROP FUNCTION IF EXISTS adventurer.journey_policy_choice_consistent();
DROP FUNCTION IF EXISTS adventurer.journey_governor_consistent_expedition();
DROP FUNCTION IF EXISTS adventurer.journey_governor_consistent_one(bigint);
DROP FUNCTION IF EXISTS adventurer.journey_governor_consistent();
DROP FUNCTION IF EXISTS adventurer.protect_completed_journey_policy();
DROP FUNCTION IF EXISTS adventurer.journey_activity_policy_nonempty();
DROP FUNCTION IF EXISTS adventurer.expedition_activity_policy_nonempty();
DROP FUNCTION IF EXISTS adventurer.protect_expedition_activity_types();
DROP FUNCTION IF EXISTS adventurer.protect_expedition_activity_policy();
DROP FUNCTION IF EXISTS adventurer.note_expedition_delete();

DROP TABLE IF EXISTS adventurer.journey_policy_choices;
DROP TABLE IF EXISTS adventurer.journey_activity_types;
DROP TABLE IF EXISTS adventurer.expedition_activity_types;

ALTER TABLE adventurer.route_journeys
    DROP CONSTRAINT IF EXISTS route_journeys_governing_expedition_fkey;
ALTER TABLE adventurer.route_journeys
    DROP CONSTRAINT IF EXISTS route_journeys_activity_policy_chk;
DROP INDEX IF EXISTS adventurer.route_journeys_governing_expedition_idx;
ALTER TABLE adventurer.route_journeys
    DROP COLUMN IF EXISTS governing_expedition_id;
ALTER TABLE adventurer.route_journeys
    DROP COLUMN IF EXISTS activity_policy;

ALTER TABLE adventurer.expeditions
    DROP CONSTRAINT IF EXISTS expeditions_activity_policy_chk;
ALTER TABLE adventurer.expeditions
    DROP COLUMN IF EXISTS activity_policy_sealed;
ALTER TABLE adventurer.expeditions
    DROP COLUMN IF EXISTS activity_policy;
