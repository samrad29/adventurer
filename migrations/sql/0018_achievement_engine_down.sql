DROP TRIGGER delete_account_completion_history ON adventurer.users;
DROP TRIGGER trail_identity_no_truncate ON adventurer.user_trail_identities;
DROP TRIGGER trail_identity_delete_guard ON adventurer.user_trail_identities;
DROP TRIGGER earned_achievement_managed_snapshot ON adventurer.earned_achievements;
DROP TRIGGER managed_edition_configuration ON adventurer.achievement_editions;
DROP TRIGGER managed_definition_history ON adventurer.achievement_definitions;
DROP TABLE adventurer.achievement_reveals;
DROP INDEX adventurer.earned_achievement_owner_order_idx;
ALTER TABLE adventurer.earned_achievements
    DROP COLUMN issuance_rule, DROP COLUMN deduplication_key, DROP COLUMN completion_event_key,
    DROP COLUMN completion_number, DROP COLUMN route_group_key;
ALTER TABLE adventurer.achievement_definitions
    DROP COLUMN evaluator_key, DROP COLUMN evaluator_version, DROP COLUMN evaluator_route_id, DROP COLUMN issuance_rule;
DROP TABLE adventurer.route_completions;
DROP FUNCTION adventurer.delete_account_completion_history();
DROP FUNCTION adventurer.protect_personal_history_truncate();
DROP FUNCTION adventurer.protect_trail_identity_delete();
DROP FUNCTION adventurer.protect_route_completion();
DROP FUNCTION IF EXISTS adventurer.protect_achievement_reveal();
DROP FUNCTION adventurer.prepare_managed_award();
DROP FUNCTION adventurer.validate_managed_edition();
DROP FUNCTION adventurer.protect_managed_definition();
