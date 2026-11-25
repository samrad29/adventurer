DROP TABLE adventurer.earned_achievements;
DROP TABLE adventurer.achievement_editions;
DROP TABLE adventurer.achievement_definitions;
DROP FUNCTION adventurer.prepare_earned_achievement();
DROP FUNCTION adventurer.consume_achievement_edition_number();
DROP FUNCTION adventurer.protect_earned_achievement();
DROP FUNCTION adventurer.protect_achievement_edition();
DROP FUNCTION adventurer.protect_achievement_definition();

-- Keep the recommendation table secured when rolling back achievement support.
