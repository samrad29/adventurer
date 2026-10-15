import pytest

from adventurer import create_app


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(tmp_path / "test.db"),
            "DATABASE_URL": "",
            "INVITE_CODE": "",
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()
