"""Thin client for api.mfapi.in — §2.1.

Retries 3x with exponential backoff (30s / 2m / 8m) on failure, per spec.
No "since" parameter exists on this API: every call returns full history,
so callers filter client-side (daily_refresh.py does this).
"""
import time

import requests

from . import config


class MFApiError(Exception):
    """Raised when a call fails after all retries are exhausted."""


def _get_json(path: str):
    url = f"{config.MFAPI_BASE_URL}{path}"
    last_exc = None
    for attempt, backoff in enumerate([0] + config.RETRY_BACKOFF_SECONDS):
        if backoff:
            time.sleep(backoff)
        try:
            resp = requests.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
    raise MFApiError(f"GET {path} failed after {len(config.RETRY_BACKOFF_SECONDS)} retries: {last_exc}")


def fetch_mf_list():
    """GET /mf -> [{schemeCode, schemeName, isinGrowth, isinDivReinvestment}, ...]"""
    return _get_json("/mf")


def fetch_mf_scheme(scheme_code):
    """GET /mf/{code} -> {meta: {...}, data: [{date, nav}, ...], status}"""
    return _get_json(f"/mf/{scheme_code}")
