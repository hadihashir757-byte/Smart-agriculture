"""Loads the project's .env file into the environment, so `python3 api/agent.py` and gunicorn pick up the settings
without a shell `source .env`. Import this FIRST (before any module that reads settings at import time).

Real environment variables always win over the file (override=False), and ENV_FILE can point to another file.
Needs python-dotenv (in requirements.txt); without it nothing happens.
"""

import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ENV_PATH = os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def load(path=None):
    """Returns True if a .env file was read."""
    if load_dotenv is None:
        return False
    return bool(load_dotenv(path or ENV_PATH, override=False))


load()
