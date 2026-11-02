"""API-key authentication shared by every route.

Secure by default:
  * API_KEY must be set in the environment. There is NO built-in default key.
    (For local development only, set DEV_INSECURE_KEY=1 to use "dev-secret-change-me".)
  * The key is read from the  X-API-Key  header only. Putting a key in the URL
    (?api_key=...) is OFF by default because URLs end up in server logs and
    browser history. If an HTTP client (e.g. some MacroDroid configurations)
    really cannot send headers, set ALLOW_QUERY_API_KEY=true to turn it back on.
  * Keys are compared in constant time.
"""

import hmac
import os
from functools import wraps

from flask import jsonify, request


def _load_key():
    key = os.environ.get("API_KEY", "").strip()
    if key:
        return key
    if os.environ.get("DEV_INSECURE_KEY") == "1":
        print("!! DEV_INSECURE_KEY=1: using an insecure development API key. Never do this in production. !!")
        return "dev-secret-change-me"
    raise RuntimeError(
        "API_KEY is not set. Generate one with:  openssl rand -hex 32  "
        "and put it in your environment / .env file. "
        "(For local development only you can set DEV_INSECURE_KEY=1.)"
    )


API_KEY = _load_key()
ALLOW_QUERY_API_KEY = os.environ.get("ALLOW_QUERY_API_KEY", "false").lower() == "true"


def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-API-Key")
        if not provided and ALLOW_QUERY_API_KEY:
            provided = request.args.get("api_key")
        if not provided or not hmac.compare_digest(provided.encode(), API_KEY.encode()):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper
