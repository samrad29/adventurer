"""Resolve the managed event time before checking edition eligibility."""

from alembic import op

revision = "0019_managed_award_event_time"
down_revision = "0018_achievement_engine"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION adventurer.resolve_managed_award_event_time()
        RETURNS trigger LANGUAGE plpgsql SET search_path = adventurer, pg_temp AS $fn$
        BEGIN
            SELECT completed_at INTO NEW.event_at
            FROM route_completions
            WHERE event_key = NEW.completion_event_key AND user_id = NEW.user_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'managed award does not match its completion or cohort'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $fn$
    """)
    # PostgreSQL runs equivalent triggers by name. Resolve the event before
    # earned_achievement_issuance checks the edition's eligibility window.
    op.execute("""
        CREATE TRIGGER earned_achievement_event_time BEFORE INSERT ON adventurer.earned_achievements
        FOR EACH ROW WHEN (NEW.completion_event_key IS NOT NULL)
        EXECUTE FUNCTION adventurer.resolve_managed_award_event_time()
    """)


def downgrade():
    op.execute("DROP TRIGGER earned_achievement_event_time ON adventurer.earned_achievements")
    op.execute("DROP FUNCTION adventurer.resolve_managed_award_event_time()")
