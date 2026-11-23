-- Remove only the narrative catalog. Progress, journeys, expeditions, and
-- achievements stay. Rows in the narrative tables have no place in 0009,
-- so dropping the tables removes them.

DROP TABLE IF EXISTS adventurer.journey_story_views;
DROP TABLE IF EXISTS adventurer.route_media;
DROP TABLE IF EXISTS adventurer.story_discoveries;
DROP TABLE IF EXISTS adventurer.checkpoint_stories;

DROP TRIGGER IF EXISTS route_chapter_spans ON adventurer.route_checkpoints;
DROP TRIGGER IF EXISTS route_chapter_spans_chapters ON adventurer.route_chapters;
DROP TRIGGER IF EXISTS route_chapters_clear_checkpoints ON adventurer.route_chapters;

DROP FUNCTION IF EXISTS adventurer.enforce_route_chapter_order();
DROP FUNCTION IF EXISTS adventurer.enforce_route_chapter_spans();
DROP FUNCTION IF EXISTS adventurer.assert_route_chapter_spans(bigint);
DROP FUNCTION IF EXISTS adventurer.clear_chapter_checkpoints();

ALTER TABLE adventurer.route_checkpoints
    DROP CONSTRAINT IF EXISTS route_checkpoints_chapter_route_fkey;

ALTER TABLE adventurer.route_checkpoints
    DROP COLUMN IF EXISTS chapter_id;

DROP TABLE IF EXISTS adventurer.route_chapters;
