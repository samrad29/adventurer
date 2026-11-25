CREATE TABLE adventurer.achievement_definitions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug <> ''),
    name text NOT NULL CHECK (btrim(name) <> ''),
    description text NOT NULL,
    scope text NOT NULL CHECK (scope IN ('global', 'route', 'expedition', 'community')),
    source text NOT NULL CHECK (source IN (
        'route_completion', 'expedition_completion', 'personal_milestone',
        'community_award', 'administrative_grant'
    )),
    criteria jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(criteria) = 'object'),
    status text NOT NULL DEFAULT 'inactive' CHECK (status IN ('active', 'inactive', 'retired')),
    repeatable boolean NOT NULL DEFAULT false,
    presentation jsonb CHECK (jsonb_typeof(presentation) = 'object'),
    default_edition_size integer CHECK (default_edition_size > 0),
    created_by_user_id bigint REFERENCES adventurer.users (id) ON DELETE SET NULL,
    updated_by_user_id bigint REFERENCES adventurer.users (id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE adventurer.achievement_editions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    definition_id bigint NOT NULL REFERENCES adventurer.achievement_definitions (id),
    slug text NOT NULL CHECK (slug <> ''),
    size integer NOT NULL CHECK (size > 0),
    issued_count integer NOT NULL DEFAULT 0 CHECK (issued_count BETWEEN 0 AND size),
    available_from timestamptz,
    available_until timestamptz,
    eligible_from timestamptz,
    eligible_until timestamptz,
    UNIQUE (definition_id, slug),
    UNIQUE (definition_id, id),
    CHECK (available_from < available_until),
    CHECK (eligible_from < eligible_until)
);

CREATE TABLE adventurer.earned_achievements (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES adventurer.users (id),
    definition_id bigint NOT NULL REFERENCES adventurer.achievement_definitions (id),
    edition_id bigint,
    sequence_number integer,
    edition_size integer,
    issued_at timestamptz NOT NULL,
    event_at timestamptz NOT NULL,
    source text NOT NULL,
    event_key text NOT NULL CHECK (btrim(event_key) <> ''),
    repeat_key text NOT NULL DEFAULT '',
    route_id bigint REFERENCES adventurer.routes (id) ON DELETE SET NULL,
    expedition_id bigint REFERENCES adventurer.expeditions (id) ON DELETE SET NULL,
    activity_id bigint REFERENCES adventurer.activities (id) ON DELETE SET NULL,
    organization_id bigint REFERENCES adventurer.organizations (id) ON DELETE SET NULL,
    context_snapshot jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(context_snapshot) = 'object'),
    award_snapshot jsonb NOT NULL CHECK (jsonb_typeof(award_snapshot) = 'object'),
    FOREIGN KEY (definition_id, edition_id)
        REFERENCES adventurer.achievement_editions (definition_id, id),
    UNIQUE (edition_id, sequence_number),
    CHECK (
        (edition_id IS NULL AND sequence_number IS NULL AND edition_size IS NULL)
        OR (edition_id IS NOT NULL AND sequence_number IS NOT NULL AND edition_size IS NOT NULL
            AND sequence_number BETWEEN 1 AND edition_size)
    )
);

CREATE UNIQUE INDEX earned_achievements_unlimited_event_key
    ON adventurer.earned_achievements (user_id, definition_id, source, event_key, repeat_key)
    WHERE edition_id IS NULL;
CREATE UNIQUE INDEX earned_achievements_edition_event_key
    ON adventurer.earned_achievements (user_id, definition_id, edition_id, source, event_key, repeat_key)
    WHERE edition_id IS NOT NULL;
CREATE INDEX earned_achievements_definition_idx ON adventurer.earned_achievements (definition_id);
CREATE INDEX earned_achievements_route_idx ON adventurer.earned_achievements (route_id);
CREATE INDEX earned_achievements_expedition_idx ON adventurer.earned_achievements (expedition_id);
CREATE INDEX earned_achievements_activity_idx ON adventurer.earned_achievements (activity_id);
CREATE INDEX earned_achievements_organization_idx ON adventurer.earned_achievements (organization_id);

CREATE FUNCTION adventurer.protect_achievement_definition()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF (NEW.id, NEW.slug, NEW.source, NEW.repeatable)
            IS DISTINCT FROM (OLD.id, OLD.slug, OLD.source, OLD.repeatable)
       OR (OLD.status = 'retired' AND NEW.status <> 'retired') THEN
        RAISE EXCEPTION 'achievement definition identity or retirement cannot change'
            USING ERRCODE = '23514';
    END IF;
    NEW.updated_at := statement_timestamp();
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER achievement_definition_history
BEFORE UPDATE ON adventurer.achievement_definitions
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_achievement_definition();

CREATE FUNCTION adventurer.protect_achievement_edition()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.issued_count > 0 THEN
            RAISE EXCEPTION 'an issued achievement edition cannot be deleted' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        PERFORM 1 FROM achievement_definitions
            WHERE id = NEW.definition_id AND status <> 'retired' FOR SHARE;
        IF NOT FOUND OR NEW.issued_count <> 0 THEN
            RAISE EXCEPTION 'achievement edition cannot be created' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.definition_id, NEW.slug) IS DISTINCT FROM (OLD.id, OLD.definition_id, OLD.slug)
       OR (OLD.issued_count > 0 AND
           (NEW.size, NEW.available_from, NEW.available_until, NEW.eligible_from, NEW.eligible_until)
               IS DISTINCT FROM
           (OLD.size, OLD.available_from, OLD.available_until, OLD.eligible_from, OLD.eligible_until))
       OR (NEW.issued_count <> OLD.issued_count
           AND (pg_trigger_depth() = 1 OR NEW.issued_count <> OLD.issued_count + 1)) THEN
        RAISE EXCEPTION 'achievement edition history cannot change' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER achievement_edition_history
BEFORE INSERT OR UPDATE OR DELETE ON adventurer.achievement_editions
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_achievement_edition();

CREATE FUNCTION adventurer.protect_earned_achievement()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF journey_credit_deletion_allowed(OLD.user_id) IS NOT TRUE THEN
            RAISE EXCEPTION 'earned achievements can only be deleted in the account deletion batch'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    -- Referential actions may clear a live reference while the snapshot stays intact.
    IF (to_jsonb(NEW) - ARRAY['route_id', 'expedition_id', 'activity_id', 'organization_id'])
            IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['route_id', 'expedition_id', 'activity_id', 'organization_id'])
       OR (NEW.route_id IS NOT NULL AND NEW.route_id IS DISTINCT FROM OLD.route_id)
       OR (NEW.expedition_id IS NOT NULL AND NEW.expedition_id IS DISTINCT FROM OLD.expedition_id)
       OR (NEW.activity_id IS NOT NULL AND NEW.activity_id IS DISTINCT FROM OLD.activity_id)
       OR (NEW.organization_id IS NOT NULL AND NEW.organization_id IS DISTINCT FROM OLD.organization_id) THEN
        RAISE EXCEPTION 'earned achievement history cannot change' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER earned_achievement_history
BEFORE UPDATE OR DELETE ON adventurer.earned_achievements
FOR EACH ROW EXECUTE FUNCTION adventurer.protect_earned_achievement();

CREATE FUNCTION adventurer.prepare_earned_achievement()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
DECLARE
    definition adventurer.achievement_definitions;
    edition adventurer.achievement_editions;
    issue_time timestamptz;
BEGIN
    PERFORM 1 FROM users WHERE id = NEW.user_id AND deletion_state IS NULL FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'achievement recipient is unavailable' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO definition FROM achievement_definitions WHERE id = NEW.definition_id FOR SHARE;
    IF NOT FOUND OR definition.status <> 'active' OR NEW.source IS DISTINCT FROM definition.source THEN
        RAISE EXCEPTION 'achievement definition is unavailable for this source' USING ERRCODE = '23514';
    END IF;
    IF NOT definition.repeatable AND NEW.repeat_key <> '' THEN
        RAISE EXCEPTION 'achievement definition does not allow repeat keys' USING ERRCODE = '23514';
    END IF;
    IF NEW.edition_id IS NOT NULL THEN
        SELECT * INTO edition FROM achievement_editions
            WHERE id = NEW.edition_id AND definition_id = NEW.definition_id FOR NO KEY UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'achievement edition does not belong to the definition' USING ERRCODE = '23514';
        END IF;
    END IF;
    issue_time := clock_timestamp();
    NEW.issued_at := issue_time;
    NEW.event_at := COALESCE(NEW.event_at, issue_time);
    NEW.sequence_number := NULL;
    NEW.edition_size := NULL;
    IF NEW.edition_id IS NOT NULL THEN
        IF issue_time < edition.available_from OR issue_time >= edition.available_until
           OR NEW.event_at < edition.eligible_from OR NEW.event_at >= edition.eligible_until THEN
            RAISE EXCEPTION 'achievement edition is outside its issuance window' USING ERRCODE = '23514';
        END IF;
        IF edition.issued_count = edition.size THEN
            RAISE EXCEPTION 'achievement edition is exhausted' USING ERRCODE = '23514';
        END IF;
        NEW.sequence_number := edition.issued_count + 1;
        NEW.edition_size := edition.size;
    END IF;
    NEW.award_snapshot := jsonb_build_object(
        'definition_slug', definition.slug, 'edition_slug', edition.slug,
        'name', definition.name, 'description', definition.description,
        'scope', definition.scope, 'presentation', definition.presentation
    );
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER earned_achievement_issuance
BEFORE INSERT ON adventurer.earned_achievements
FOR EACH ROW EXECUTE FUNCTION adventurer.prepare_earned_achievement();

CREATE FUNCTION adventurer.consume_achievement_edition_number()
RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
BEGIN
    UPDATE achievement_editions SET issued_count = NEW.sequence_number
        WHERE id = NEW.edition_id;
    RETURN NULL;
END;
$fn$;

CREATE TRIGGER earned_achievement_edition_number
AFTER INSERT ON adventurer.earned_achievements
FOR EACH ROW WHEN (NEW.edition_id IS NOT NULL)
EXECUTE FUNCTION adventurer.consume_achievement_edition_number();

ALTER TABLE adventurer.route_activity_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.achievement_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.achievement_editions ENABLE ROW LEVEL SECURITY;
ALTER TABLE adventurer.earned_achievements ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON adventurer.route_activity_recommendations, adventurer.achievement_definitions,
    adventurer.achievement_editions, adventurer.earned_achievements FROM PUBLIC;
REVOKE ALL ON SEQUENCE adventurer.achievement_definitions_id_seq, adventurer.achievement_editions_id_seq,
    adventurer.earned_achievements_id_seq FROM PUBLIC;

DO $roles$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON adventurer.route_activity_recommendations, adventurer.achievement_definitions,
            adventurer.achievement_editions, adventurer.earned_achievements FROM anon;
        REVOKE ALL ON SEQUENCE adventurer.achievement_definitions_id_seq, adventurer.achievement_editions_id_seq,
            adventurer.earned_achievements_id_seq FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON adventurer.route_activity_recommendations, adventurer.achievement_definitions,
            adventurer.achievement_editions, adventurer.earned_achievements FROM authenticated;
        REVOKE ALL ON SEQUENCE adventurer.achievement_definitions_id_seq, adventurer.achievement_editions_id_seq,
            adventurer.earned_achievements_id_seq FROM authenticated;
    END IF;
END;
$roles$;
