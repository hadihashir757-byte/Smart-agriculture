"""
Pushes a demo-mode change to MacroDroid's webhook trigger, so the phone's
photo-capture speed reacts instantly instead of waiting for its next poll.

This is deliberately "fire and forget": if MacroDroid's webhook service is
unreachable, times out, or the URLs aren't configured, this just logs it and
moves on - it never raises, and it never blocks the caller for long. The push
is the fast path, not the only path - MacroDroid's own fallback poll
(GET /demo-mode-status) is what actually guarantees correctness if this call
is ever lost, so this function failing silently is an acceptable trade-off,
not a hidden bug.
"""

import os

import requests

WEBHOOK_TIMEOUT_SEC = 5


def notify_macrodroid(enabled):
    url = (os.environ.get("MACRODROID_DEMO_ON_WEBHOOK_URL") if enabled
           else os.environ.get("MACRODROID_DEMO_OFF_WEBHOOK_URL"))

    if not url:
        print(f"[macrodroid] no webhook URL configured for demo_mode={enabled} - skipping push "
              f"(the fallback poll will still pick this up)")
        return

    try:
        requests.get(url, timeout=WEBHOOK_TIMEOUT_SEC)
        print(f"[macrodroid] pushed demo_mode={enabled}")
    except Exception as e:
        print(f"[macrodroid] push failed for demo_mode={enabled}, falling back to poll: {e}")
