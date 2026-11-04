"""Fetches a still photo from the phone camera app (LensCast, IP Webcam ...).

Settings (environment variables):
  CAMERA_BASE_URL       e.g. http://192.168.1.50:8080   (what the camera app shows; no trailing slash needed)
  CAMERA_SNAPSHOT_PATH  default /snapshot
  CAMERA_USER / CAMERA_PASSWORD   if you set a password in the app (HTTP Basic)
  CAMERA_API_TOKEN      alternative to a password (sent as X-Api-Token)
"""

import os

import requests

BASE = (os.environ.get("CAMERA_BASE_URL") or "").rstrip("/")
PATH = os.environ.get("CAMERA_SNAPSHOT_PATH", "/snapshot")
USER = os.environ.get("CAMERA_USER")
PASSWORD = os.environ.get("CAMERA_PASSWORD")
TOKEN = os.environ.get("CAMERA_API_TOKEN")
TIMEOUT = 15
MAX_BYTES = 15 * 1024 * 1024


def configured():
    return bool(BASE)


def fetch_snapshot():
    """Returns the JPEG bytes. Raises RuntimeError with a plain message when it fails."""
    if not configured():
        raise RuntimeError("CAMERA_BASE_URL is not set")
    kwargs = {"timeout": TIMEOUT}
    if USER and PASSWORD:
        kwargs["auth"] = (USER, PASSWORD)
    if TOKEN:
        kwargs["headers"] = {"X-Api-Token": TOKEN}
    try:
        r = requests.get(BASE + (PATH if PATH.startswith("/") else "/" + PATH), **kwargs)
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach the camera ({e.__class__.__name__}) - is the phone on and on the same Wi-Fi?")
    if r.status_code in (401, 403):
        raise RuntimeError("the camera refused the login - check CAMERA_USER / CAMERA_PASSWORD")
    if r.status_code != 200:
        raise RuntimeError(f"the camera answered HTTP {r.status_code}")
    data = r.content
    if not data or len(data) > MAX_BYTES:
        raise RuntimeError("the camera returned an empty or oversized picture")
    ctype = r.headers.get("Content-Type", "")
    if "image" not in ctype and not data[:2] == b"\xff\xd8":
        raise RuntimeError(f"the camera did not return a picture (Content-Type: {ctype or 'none'})")
    return data
