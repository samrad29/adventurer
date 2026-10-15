import os

import pytest

from adventurer import create_app
from adventurer.db import get_db

_TEST_NAMES = ("sam", "alex")


def _wipe(application) -> None:
    with application.app_context():
        database = get_db()
        database.execute(
            """
            DELETE FROM logs
            WHERE adventurer_id IN (
                SELECT id FROM adventurers WHERE name_key IN (%s, %s)
            )
            """,
            _TEST_NAMES,
        )
        database.execute(
            "DELETE FROM adventurers WHERE name_key IN (%s, %s)",
            _TEST_NAMES,
        )
        database.commit()


@pytest.fixture()
def app():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL is required")
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE_URL": url,
            "INVITE_CODE": "",
        }
    )
    _wipe(application)
    yield application
    _wipe(application)


@pytest.fixture()
def client(app):
    return app.test_client()
