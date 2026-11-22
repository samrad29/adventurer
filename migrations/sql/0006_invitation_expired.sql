-- An expired pending invitation was still pending, so the partial unique
-- indexes blocked a replacement. Expired is its own status. The pending
-- indexes stay limited to status = 'pending'.

ALTER TABLE adventurer.expedition_invitations
    DROP CONSTRAINT expedition_invitations_status_chk;

ALTER TABLE adventurer.expedition_invitations
    ADD CONSTRAINT expedition_invitations_status_chk
    CHECK (status IN ('pending', 'accepted', 'declined', 'revoked', 'expired'));

UPDATE adventurer.expedition_invitations
SET status = 'expired'
WHERE status = 'pending'
  AND expires_at <= now();
