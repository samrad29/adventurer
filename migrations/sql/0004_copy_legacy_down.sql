DROP FUNCTION IF EXISTS adventurer.assert_phase1_legacy_match();
DROP FUNCTION IF EXISTS adventurer.reverse_sync_new_rows_to_legacy();

-- Deletes the copied accounts. Activities, profiles, participations, credits,
-- and verification events follow the foreign keys. The activity delete
-- trigger is the supported way to remove verification history.
DELETE FROM adventurer.users;
