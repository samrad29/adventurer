-- Restore the previous status check. Expired rows go back to pending only
-- when that cannot collide with the one-pending indexes. A collision means
-- this is no longer a mechanical reverse. Restore from a backup taken
-- before 0006_invitation_expired. See docs/phase3-schema.md.

DO $guard$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM adventurer.expedition_invitations AS expired_row
        JOIN adventurer.expedition_invitations AS pending_row
            ON pending_row.expedition_id = expired_row.expedition_id
           AND pending_row.status = 'pending'
           AND pending_row.invited_user_id IS NOT DISTINCT FROM expired_row.invited_user_id
        WHERE expired_row.status = 'expired'
    ) OR EXISTS (
        SELECT 1
        FROM adventurer.expedition_invitations
        WHERE status = 'expired'
        GROUP BY expedition_id, invited_user_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION USING MESSAGE =
            'Refusing downgrade: expired invitations cannot return to pending without breaking the one-pending rule. Restore from a backup.';
    END IF;
END
$guard$;

UPDATE adventurer.expedition_invitations
SET status = 'pending'
WHERE status = 'expired';

ALTER TABLE adventurer.expedition_invitations
    DROP CONSTRAINT expedition_invitations_status_chk;

ALTER TABLE adventurer.expedition_invitations
    ADD CONSTRAINT expedition_invitations_status_chk
    CHECK (status IN ('pending', 'accepted', 'declined', 'revoked'));
