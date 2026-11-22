-- A privacy removal deletes the participation, so the next insert cannot
-- tell a first join from a rejoin. This row is that fact and nothing else.
-- Rejoin consumes it. The user and expedition foreign keys remove it when
-- either parent row goes away.

CREATE TABLE adventurer.expedition_privacy_removals (
    expedition_id bigint NOT NULL,
    user_id bigint NOT NULL,
    removed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT expedition_privacy_removals_pkey
        PRIMARY KEY (expedition_id, user_id),
    CONSTRAINT expedition_privacy_removals_expedition_id_fkey
        FOREIGN KEY (expedition_id)
        REFERENCES adventurer.expeditions (id) ON DELETE CASCADE,
    CONSTRAINT expedition_privacy_removals_user_id_fkey
        FOREIGN KEY (user_id)
        REFERENCES adventurer.users (id) ON DELETE CASCADE
);

CREATE INDEX expedition_privacy_removals_user_idx
    ON adventurer.expedition_privacy_removals (user_id);

ALTER TABLE adventurer.expedition_privacy_removals ENABLE ROW LEVEL SECURITY;

DO $lock$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE adventurer.expedition_privacy_removals FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE adventurer.expedition_privacy_removals FROM authenticated;
    END IF;
END
$lock$;
