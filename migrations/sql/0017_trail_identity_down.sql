ALTER TABLE adventurer.expedition_updates DROP COLUMN name_capture_present,
    DROP COLUMN profile_name_snapshot, DROP COLUMN trail_name_snapshot;
ALTER TABLE adventurer.expedition_result_entries DROP COLUMN trail_name_snapshot, DROP COLUMN profile_name_visible;
ALTER TABLE adventurer.user_profiles DROP COLUMN trail_name_display;
DROP TABLE adventurer.user_trail_identities, adventurer.trail_identity_checkpoints, adventurer.trail_name_pool;
DROP FUNCTION adventurer.protect_trail_history();
DROP FUNCTION adventurer.validate_trail_pool();
DROP FUNCTION adventurer.lock_trail_editorial_config();
