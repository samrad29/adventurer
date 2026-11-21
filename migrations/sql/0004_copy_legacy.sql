-- Copy adventurers and logs into the phase 1 tables.
-- Legacy tables are not updated or dropped. A failed check rolls this
-- revision back and leaves the earlier revisions in place.

CREATE FUNCTION adventurer.legacy_text_is_date(value text)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF value IS NULL OR btrim(value) = '' THEN
        RETURN false;
    END IF;
    PERFORM value::date;
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END;
$fn$;

CREATE FUNCTION adventurer.legacy_text_is_timestamptz(value text)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = adventurer, pg_temp
AS $fn$
BEGIN
    IF value IS NULL OR btrim(value) = '' THEN
        RETURN false;
    END IF;
    PERFORM value::timestamptz;
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END;
$fn$;

DO $pre$
DECLARE
    bad_ids text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM adventurer.expeditions WHERE slug = 'everest'
    ) THEN
        RAISE EXCEPTION 'expedition everest is not seeded';
    END IF;

    SELECT string_agg(id::text, ', ' ORDER BY id) INTO bad_ids
    FROM adventurer.logs
    WHERE NOT adventurer.legacy_text_is_date(happened_on);
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'logs.happened_on is not a date for log ids ' || bad_ids;
    END IF;

    SELECT string_agg(id::text, ', ' ORDER BY id) INTO bad_ids
    FROM adventurer.logs
    WHERE NOT adventurer.legacy_text_is_timestamptz(created_at);
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'logs.created_at is not a timestamp for log ids ' || bad_ids;
    END IF;

    SELECT string_agg(id::text, ', ' ORDER BY id) INTO bad_ids
    FROM adventurer.adventurers
    WHERE NOT adventurer.legacy_text_is_timestamptz(created_at);
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'adventurers.created_at is not a timestamp for ids ' || bad_ids;
    END IF;

    SELECT string_agg(id::text, ', ' ORDER BY id) INTO bad_ids
    FROM adventurer.logs
    WHERE (elevation_ft::numeric * 0.3048)::numeric(12, 5) <= 0;
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'elevation rounds to zero meters for log ids ' || bad_ids;
    END IF;

    SELECT string_agg(id::text, ', ' ORDER BY id) INTO bad_ids
    FROM adventurer.logs
    WHERE distance_km IS NOT NULL
      AND (distance_km::numeric * 1000)::numeric(14, 3) <= 0;
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'distance rounds to zero meters for log ids ' || bad_ids;
    END IF;

    SELECT string_agg(DISTINCT challenge_slug, ', ' ORDER BY challenge_slug)
        INTO bad_ids
    FROM adventurer.logs AS legacy_log
    WHERE NOT EXISTS (
        SELECT 1
        FROM adventurer.expeditions AS expedition
        WHERE expedition.slug = legacy_log.challenge_slug
    );
    IF bad_ids IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'unmapped challenge_slug values: ' || bad_ids;
    END IF;
END
$pre$;

DROP FUNCTION adventurer.legacy_text_is_date(text);
DROP FUNCTION adventurer.legacy_text_is_timestamptz(text);

INSERT INTO adventurer.users (
    id, username, username_key, password_hash, created_at, updated_at
)
SELECT
    id,
    name,
    name_key,
    password_hash,
    created_at::timestamptz,
    created_at::timestamptz
FROM adventurer.adventurers;

INSERT INTO adventurer.user_profiles (
    user_id, display_name, unit_system, created_at, updated_at
)
SELECT
    id,
    name,
    'imperial',
    created_at::timestamptz,
    created_at::timestamptz
FROM adventurer.adventurers;

-- The live site has one implicit climb. Every existing account joins it,
-- including accounts that have not logged anything yet.
INSERT INTO adventurer.expedition_participations (
    user_id, expedition_id, status, joined_at, created_at, updated_at
)
SELECT
    account.id,
    expedition.id,
    'active',
    account.created_at::timestamptz,
    account.created_at::timestamptz,
    account.created_at::timestamptz
FROM adventurer.adventurers AS account
CROSS JOIN adventurer.expeditions AS expedition
WHERE expedition.slug = 'everest';

-- Any other slug is enrolled only for accounts that actually logged it.
-- The slug must already match an expedition; the preflight rejected the rest.
INSERT INTO adventurer.expedition_participations (
    user_id, expedition_id, status, joined_at, created_at, updated_at
)
SELECT
    legacy_log.adventurer_id,
    expedition.id,
    'active',
    MIN(account.created_at::timestamptz),
    MIN(account.created_at::timestamptz),
    MIN(account.created_at::timestamptz)
FROM adventurer.logs AS legacy_log
JOIN adventurer.expeditions AS expedition
    ON expedition.slug = legacy_log.challenge_slug
JOIN adventurer.adventurers AS account
    ON account.id = legacy_log.adventurer_id
GROUP BY legacy_log.adventurer_id, expedition.id
ON CONFLICT (user_id, expedition_id) DO NOTHING;

INSERT INTO adventurer.activities (
    id,
    user_id,
    activity_type,
    activity_date,
    started_at,
    ended_at,
    duration_s,
    distance_m,
    elevation_gain_m,
    source,
    external_id,
    source_metadata,
    verification_status,
    note,
    created_at,
    updated_at
)
SELECT
    legacy_log.id,
    legacy_log.adventurer_id,
    'unspecified',
    legacy_log.happened_on::date,
    NULL,
    NULL,
    NULL,
    CASE
        WHEN legacy_log.distance_km IS NULL THEN NULL
        ELSE (legacy_log.distance_km::numeric * 1000)::numeric(14, 3)
    END,
    (legacy_log.elevation_ft::numeric * 0.3048)::numeric(12, 5),
    'manual',
    NULL,
    '{}'::jsonb,
    'not_required',
    legacy_log.note,
    legacy_log.created_at::timestamptz,
    legacy_log.created_at::timestamptz
FROM adventurer.logs AS legacy_log;

INSERT INTO adventurer.activity_credits (
    activity_id,
    participation_id,
    user_id,
    credited_elevation_m,
    created_at,
    updated_at
)
SELECT
    activity.id,
    participation.id,
    activity.user_id,
    activity.elevation_gain_m,
    activity.created_at,
    activity.created_at
FROM adventurer.activities AS activity
JOIN adventurer.logs AS legacy_log ON legacy_log.id = activity.id
JOIN adventurer.expeditions AS expedition
    ON expedition.slug = legacy_log.challenge_slug
JOIN adventurer.expedition_participations AS participation
    ON participation.user_id = activity.user_id
   AND participation.expedition_id = expedition.id;

INSERT INTO adventurer.activity_verification_events (
    activity_id,
    from_status,
    to_status,
    reason,
    actor_type,
    actor_user_id,
    created_at
)
SELECT
    id,
    NULL,
    'not_required',
    'Migrated manual log. Manual entries are not independently verified.',
    'system',
    NULL,
    created_at
FROM adventurer.activities;

DO $seq$
BEGIN
    PERFORM setval(
        pg_get_serial_sequence('adventurer.users', 'id'),
        GREATEST(COALESCE((SELECT MAX(id) FROM adventurer.users), 1), 1),
        EXISTS (SELECT 1 FROM adventurer.users)
    );
    PERFORM setval(
        pg_get_serial_sequence('adventurer.activities', 'id'),
        GREATEST(COALESCE((SELECT MAX(id) FROM adventurer.activities), 1), 1),
        EXISTS (SELECT 1 FROM adventurer.activities)
    );
END
$seq$;

CREATE FUNCTION adventurer.assert_phase1_legacy_match()
RETURNS void
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    mismatch text;
BEGIN
    IF (SELECT COUNT(*) FROM adventurers) <> (SELECT COUNT(*) FROM users) THEN
        RAISE EXCEPTION 'user count mismatch';
    END IF;
    IF (SELECT COUNT(*) FROM user_profiles) <> (SELECT COUNT(*) FROM users) THEN
        RAISE EXCEPTION 'profile count does not match user count';
    END IF;
    IF (SELECT COUNT(*) FROM logs) <> (SELECT COUNT(*) FROM activities) THEN
        RAISE EXCEPTION 'activity count mismatch';
    END IF;
    IF (SELECT COUNT(*) FROM logs) <> (SELECT COUNT(*) FROM activity_credits) THEN
        RAISE EXCEPTION 'credit count does not match log count';
    END IF;

    IF EXISTS (
        SELECT id FROM adventurers
        EXCEPT
        SELECT id FROM users
    ) OR EXISTS (
        SELECT id FROM users
        EXCEPT
        SELECT id FROM adventurers
    ) THEN
        RAISE EXCEPTION 'user ids were not preserved';
    END IF;

    IF EXISTS (
        SELECT id FROM logs
        EXCEPT
        SELECT id FROM activities
    ) OR EXISTS (
        SELECT id FROM activities
        EXCEPT
        SELECT id FROM logs
    ) THEN
        RAISE EXCEPTION 'activity ids were not preserved';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        JOIN users AS account ON account.id = legacy_account.id
        WHERE account.username IS DISTINCT FROM legacy_account.name
           OR account.username_key IS DISTINCT FROM legacy_account.name_key
           OR account.password_hash IS DISTINCT FROM legacy_account.password_hash
           OR account.created_at IS DISTINCT FROM legacy_account.created_at::timestamptz
    ) THEN
        RAISE EXCEPTION 'user columns were not copied exactly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        JOIN user_profiles AS profile ON profile.user_id = legacy_account.id
        WHERE profile.display_name IS DISTINCT FROM legacy_account.name
           OR profile.unit_system IS DISTINCT FROM 'imperial'
    ) THEN
        RAISE EXCEPTION 'profiles were not copied from account names';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        WHERE activity.user_id IS DISTINCT FROM legacy_log.adventurer_id
           OR activity.activity_date IS DISTINCT FROM legacy_log.happened_on::date
           OR activity.note IS DISTINCT FROM legacy_log.note
           OR activity.created_at IS DISTINCT FROM legacy_log.created_at::timestamptz
           OR activity.source IS DISTINCT FROM 'manual'
           OR activity.activity_type IS DISTINCT FROM 'unspecified'
           OR activity.verification_status IS DISTINCT FROM 'not_required'
           OR activity.started_at IS NOT NULL
           OR activity.ended_at IS NOT NULL
           OR activity.duration_s IS NOT NULL
           OR activity.external_id IS NOT NULL
           OR activity.elevation_gain_m IS DISTINCT FROM
                (legacy_log.elevation_ft::numeric * 0.3048)::numeric(12, 5)
           OR activity.distance_m IS DISTINCT FROM (
                CASE
                    WHEN legacy_log.distance_km IS NULL THEN NULL
                    ELSE (legacy_log.distance_km::numeric * 1000)::numeric(14, 3)
                END
           )
    ) THEN
        RAISE EXCEPTION 'activity columns were not copied exactly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        WHERE (
            SELECT COUNT(*)
            FROM activity_credits AS credit
            WHERE credit.activity_id = legacy_log.id
        ) <> 1
    ) THEN
        RAISE EXCEPTION 'each legacy log must have exactly one credit';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        JOIN activity_credits AS credit ON credit.activity_id = activity.id
        WHERE credit.credited_elevation_m IS DISTINCT FROM activity.elevation_gain_m
           OR credit.user_id IS DISTINCT FROM activity.user_id
    ) THEN
        RAISE EXCEPTION 'a migrated credit does not copy the full activity elevation';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        WHERE abs(
            legacy_log.elevation_ft::numeric - (activity.elevation_gain_m / 0.3048)
        ) > 0.00002
    ) THEN
        RAISE EXCEPTION 'an activity elevation does not round-trip to its log';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        JOIN activities AS activity ON activity.id = legacy_log.id
        WHERE legacy_log.distance_km IS NULL AND activity.distance_m IS NOT NULL
           OR legacy_log.distance_km IS NOT NULL AND abs(
                legacy_log.distance_km::numeric - (activity.distance_m / 1000)
           ) > 0.000001
    ) THEN
        RAISE EXCEPTION 'an activity distance does not round-trip to its log';
    END IF;

    IF (
        SELECT COUNT(*)
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE expedition.slug = 'everest'
    ) <> (SELECT COUNT(*) FROM users) THEN
        RAISE EXCEPTION 'every account was not enrolled in everest';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM activity_verification_events AS event
        JOIN logs AS legacy_log ON legacy_log.id = event.activity_id
        WHERE event.from_status IS NOT NULL
           OR event.to_status IS DISTINCT FROM 'not_required'
           OR event.actor_type IS DISTINCT FROM 'system'
           OR event.actor_user_id IS NOT NULL
    ) OR EXISTS (
        SELECT 1
        FROM logs AS legacy_log
        WHERE (
            SELECT COUNT(*)
            FROM activity_verification_events AS event
            WHERE event.activity_id = legacy_log.id
        ) <> 1
    ) THEN
        RAISE EXCEPTION 'migrated verification events are not the manual opening event';
    END IF;

    WITH legacy AS (
        SELECT
            adventurer_id AS user_id,
            challenge_slug,
            COUNT(*) AS activity_count,
            SUM(elevation_ft::numeric) AS elevation_ft
        FROM logs
        GROUP BY adventurer_id, challenge_slug
    ),
    migrated AS (
        SELECT
            credit.user_id,
            expedition.slug AS challenge_slug,
            COUNT(*) AS activity_count,
            SUM(credit.credited_elevation_m) / 0.3048 AS elevation_ft
        FROM activity_credits AS credit
        JOIN expedition_participations AS participation
            ON participation.id = credit.participation_id
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        GROUP BY credit.user_id, expedition.slug
    )
    SELECT string_agg(
        'user ' || COALESCE(legacy.user_id, migrated.user_id)::text
            || ' challenge ' || COALESCE(legacy.challenge_slug, migrated.challenge_slug),
        ', '
    )
    INTO mismatch
    FROM legacy
    FULL OUTER JOIN migrated
        ON migrated.user_id = legacy.user_id
       AND migrated.challenge_slug = legacy.challenge_slug
    WHERE legacy.activity_count IS DISTINCT FROM migrated.activity_count
       OR abs(COALESCE(legacy.elevation_ft, 0) - COALESCE(migrated.elevation_ft, 0))
          > GREATEST(COALESCE(legacy.activity_count, migrated.activity_count), 1) * 0.00002;

    IF mismatch IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'elevation totals differ by user and challenge: ' || mismatch;
    END IF;

    WITH totals AS (
        SELECT
            account.id AS user_id,
            expedition.id AS expedition_id,
            COALESCE((
                SELECT SUM(legacy_log.elevation_ft::numeric)
                FROM logs AS legacy_log
                WHERE legacy_log.adventurer_id = account.id
                  AND legacy_log.challenge_slug = expedition.slug
            ), 0) AS legacy_ft,
            COALESCE((
                SELECT SUM(credit.credited_elevation_m)
                FROM activity_credits AS credit
                JOIN expedition_participations AS participation
                    ON participation.id = credit.participation_id
                WHERE participation.user_id = account.id
                  AND participation.expedition_id = expedition.id
            ), 0) AS new_m
        FROM adventurers AS account
        CROSS JOIN expeditions AS expedition
        WHERE expedition.slug = 'everest'
           OR EXISTS (
                SELECT 1
                FROM logs AS legacy_log
                WHERE legacy_log.adventurer_id = account.id
                  AND legacy_log.challenge_slug = expedition.slug
           )
    )
    SELECT string_agg(
        'user ' || totals.user_id::text || ' checkpoint ' || checkpoint.slug,
        ', '
    )
    INTO mismatch
    FROM totals
    JOIN expedition_checkpoints AS checkpoint
        ON checkpoint.expedition_id = totals.expedition_id
    WHERE (totals.legacy_ft >= (checkpoint.elevation_m / 0.3048))
        IS DISTINCT FROM (totals.new_m >= checkpoint.elevation_m);

    IF mismatch IS NOT NULL THEN
        RAISE EXCEPTION USING MESSAGE =
            'checkpoint unlocks differ: ' || mismatch;
    END IF;

    IF (SELECT COUNT(*) FROM users) > 0 THEN
        IF (SELECT last_value FROM users_id_seq) <> (SELECT MAX(id) FROM users)
           OR (SELECT is_called FROM users_id_seq) IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'users identity sequence was not set to the preserved max id';
        END IF;
    ELSIF (SELECT is_called FROM users_id_seq) THEN
        RAISE EXCEPTION 'users identity sequence must stay unused when there are no users';
    END IF;

    IF (SELECT COUNT(*) FROM activities) > 0 THEN
        IF (SELECT last_value FROM activities_id_seq) <> (SELECT MAX(id) FROM activities)
           OR (SELECT is_called FROM activities_id_seq) IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'activities identity sequence was not set to the preserved max id';
        END IF;
    ELSIF (SELECT is_called FROM activities_id_seq) THEN
        RAISE EXCEPTION 'activities identity sequence must stay unused when there are no activities';
    END IF;
END;
$fn$;

CREATE FUNCTION adventurer.reverse_sync_new_rows_to_legacy()
RETURNS void
LANGUAGE plpgsql
SET search_path = adventurer, pg_temp
AS $fn$
DECLARE
    blocked_users bigint;
    blocked_activities bigint;
BEGIN
    SELECT COUNT(*) INTO blocked_users
    FROM users AS account
    WHERE NOT EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        WHERE legacy_account.id = account.id
    )
    AND NOT EXISTS (
        SELECT 1
        FROM user_profiles AS profile
        WHERE profile.user_id = account.id
          AND profile.display_name = account.username
          AND profile.unit_system = 'imperial'
    );

    IF blocked_users > 0 THEN
        RAISE EXCEPTION
            'reverse sync refused: a new account has no imperial profile under the same name';
    END IF;

    SELECT COUNT(*) INTO blocked_activities
    FROM activities AS activity
    WHERE NOT EXISTS (
        SELECT 1 FROM logs AS legacy_log WHERE legacy_log.id = activity.id
    )
    AND NOT (
        activity.source = 'manual'
        AND activity.activity_type = 'unspecified'
        AND activity.verification_status = 'not_required'
        AND activity.started_at IS NULL
        AND activity.ended_at IS NULL
        AND activity.duration_s IS NULL
        AND activity.external_id IS NULL
        AND activity.source_metadata = '{}'::jsonb
        AND (
            SELECT COUNT(*)
            FROM activity_credits AS credit
            WHERE credit.activity_id = activity.id
        ) = 1
        AND (
            SELECT credit.credited_elevation_m
            FROM activity_credits AS credit
            WHERE credit.activity_id = activity.id
        ) = activity.elevation_gain_m
        AND (
            SELECT expedition.slug
            FROM activity_credits AS credit
            JOIN expedition_participations AS participation
                ON participation.id = credit.participation_id
            JOIN expeditions AS expedition
                ON expedition.id = participation.expedition_id
            WHERE credit.activity_id = activity.id
        ) IS NOT NULL
    );

    IF blocked_activities > 0 THEN
        RAISE EXCEPTION
            'reverse sync refused: a new activity has data the legacy logs table cannot store';
    END IF;

    INSERT INTO adventurers (id, name, name_key, password_hash, created_at)
    SELECT
        account.id,
        account.username,
        account.username_key,
        account.password_hash,
        to_char(account.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
            || '+00:00'
    FROM users AS account
    WHERE NOT EXISTS (
        SELECT 1
        FROM adventurers AS legacy_account
        WHERE legacy_account.id = account.id
    );

    INSERT INTO logs (
        id,
        adventurer_id,
        challenge_slug,
        elevation_ft,
        distance_km,
        happened_on,
        note,
        created_at
    )
    SELECT
        activity.id,
        activity.user_id,
        expedition.slug,
        (activity.elevation_gain_m / 0.3048)::double precision,
        CASE
            WHEN activity.distance_m IS NULL THEN NULL
            ELSE (activity.distance_m / 1000)::double precision
        END,
        activity.activity_date::text,
        activity.note,
        to_char(activity.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
            || '+00:00'
    FROM activities AS activity
    JOIN activity_credits AS credit ON credit.activity_id = activity.id
    JOIN expedition_participations AS participation
        ON participation.id = credit.participation_id
    JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
    WHERE NOT EXISTS (
        SELECT 1 FROM logs AS legacy_log WHERE legacy_log.id = activity.id
    );

    PERFORM setval(
        pg_get_serial_sequence('adventurer.adventurers', 'id'),
        GREATEST(COALESCE((SELECT MAX(id) FROM adventurers), 1), 1),
        EXISTS (SELECT 1 FROM adventurers)
    );
    PERFORM setval(
        pg_get_serial_sequence('adventurer.logs', 'id'),
        GREATEST(COALESCE((SELECT MAX(id) FROM logs), 1), 1),
        EXISTS (SELECT 1 FROM logs)
    );
END;
$fn$;

DO $check$
BEGIN
    PERFORM adventurer.assert_phase1_legacy_match();
END
$check$;
