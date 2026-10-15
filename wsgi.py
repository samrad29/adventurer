"""Production entry point for Gunicorn on Render."""

from adventurer import create_app

app = create_app()
