DROP TABLE IF EXISTS adventurer.activity_verification_events;
DROP TABLE IF EXISTS adventurer.activity_credits;
DROP TABLE IF EXISTS adventurer.expedition_participations;
DROP TABLE IF EXISTS adventurer.activities;
DROP TABLE IF EXISTS adventurer.user_profiles;
DROP TABLE IF EXISTS adventurer.competition_seasons;
DROP TABLE IF EXISTS adventurer.expedition_checkpoints;
DROP TABLE IF EXISTS adventurer.expeditions;
DROP TABLE IF EXISTS adventurer.users;

DROP FUNCTION IF EXISTS adventurer.delete_activity_verification_history();
DROP FUNCTION IF EXISTS adventurer.reject_verification_event_mutation();
DROP FUNCTION IF EXISTS adventurer.reject_verification_event_truncate();
DROP FUNCTION IF EXISTS adventurer.assert_verification_event_follows_chain();
DROP FUNCTION IF EXISTS adventurer.assert_verification_status_matches_latest_event();
DROP FUNCTION IF EXISTS adventurer.set_updated_at();
DROP DOMAIN IF EXISTS adventurer.verification_status;
