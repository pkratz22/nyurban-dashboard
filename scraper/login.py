"""HTTP session, retrying request helpers, and the site login."""

from __future__ import annotations

import os
import time

import requests

BASE_URL = "https://www.nyurban.com"
LOGIN_URL = f"{BASE_URL}/basketball/new-scoring-stats/"
TEAM_LIST_URL = f"{BASE_URL}/waiver-team-listing/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """
    Wrapper around session.get/post that retries on 429 (Too Many Requests)
    or transient 5xx errors using exponential backoff.
    """
    delays = [5, 15, 30, 60]  # seconds between attempts
    for attempt, delay in enumerate(delays + [None], start=1):
        resp = getattr(session, method)(url, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", delay or 60))
            print(f"  ⚠ 429 Too Many Requests — waiting {retry_after}s (attempt {attempt})")
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500 and delay is not None:
            print(f"  ⚠ {resp.status_code} server error — waiting {delay}s (attempt {attempt})")
            time.sleep(delay)
            continue
        return resp
    # Final attempt after all delays exhausted
    return getattr(session, method)(url, **kwargs)


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return _request(session, "get", url, **kwargs)


def post(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return _request(session, "post", url, **kwargs)


def login(session: requests.Session) -> bool:
    """Log in with NYURBAN_USERNAME / NYURBAN_PASSWORD from the environment."""
    print("Step 1: Logging in...")
    resp = post(
        session, LOGIN_URL,
        data={
            "ny_username": os.environ["NYURBAN_USERNAME"],
            "ny_password": os.environ["NYURBAN_PASSWORD"],
            "submit": "login",
        },
        allow_redirects=True,
    )
    if "logout" in resp.text.lower() or "welcome" in resp.text.lower():
        print("  ✓ Login successful")
        return True
    print("  ✗ Login failed")
    return False
