"""ASGI entry point: ``uvicorn gateway.main:app``.

Kept separate from ``gateway/app.py`` so importing the factory (as tests do)
does not build an app from environment variables as a side effect.
"""

from gateway.app import create_app

app = create_app()
