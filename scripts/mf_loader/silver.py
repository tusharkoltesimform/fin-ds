"""Bronze -> silver transform: cast, dedupe, quarantine, upsert (§2.2).

Quarantine, don't drop: a row with a non-numeric/negative nav or an
unparseable date is written to silver.dq_quarantine with the raw values and
a reason, never silently discarded.
"""
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

import psycopg2.extras


def _parse_date(raw: str):
    try:
        return datetime.strptime(raw, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def _parse_nav(raw: str):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None
    if value <= 0:
        return None
    return value


# mfapi.in has no structured plan/option fields; both are embedded in
# schemeName free text. Heuristic parse, not authoritative — good enough for
# a POC dimension attribute, not used in any constraint or join.
_DIRECT_RE = re.compile(r"\bDIRECT\b", re.IGNORECASE)
_REGULAR_RE = re.compile(r"\bREGULAR\b", re.IGNORECASE)
_GROWTH_RE = re.compile(r"\bGROWTH\b", re.IGNORECASE)
_DIVIDEND_RE = re.compile(r"\b(DIVIDEND|IDCW)\b", re.IGNORECASE)


def _infer_plan(scheme_name: str):
    if _DIRECT_RE.search(scheme_name):
        return "Direct"
    if _REGULAR_RE.search(scheme_name):
        return "Regular"
    return None


def _infer_option(scheme_name: str):
    if _GROWTH_RE.search(scheme_name):
        return "Growth"
    if _DIVIDEND_RE.search(scheme_name):
        return "Dividend"
    return None


def build_dim_row(meta: dict):
    """meta -> silver.dim_mf_scheme row dict, or None + quarantine reason if unusable."""
    scheme_code = meta.get("scheme_code")
    scheme_name = meta.get("scheme_name")
    fund_house = meta.get("fund_house")
    if not scheme_code or not scheme_name or not fund_house:
        return None, f"meta missing required field(s): scheme_code/scheme_name/fund_house in {meta!r}"

    isin = meta.get("isin_growth") or meta.get("isin_div_reinvestment")
    row = {
        "scheme_code": str(scheme_code),
        "scheme_name": scheme_name,
        "amc_name": fund_house,
        "scheme_category": meta.get("scheme_category"),
        "scheme_type": meta.get("scheme_type"),
        "plan": _infer_plan(scheme_name),
        "option": _infer_option(scheme_name),
        "isin": isin,
    }
    return row, None


def cast_nav_rows(scheme_code: str, data_rows: list[dict]):
    """Cast + in-batch dedupe a scheme's data[] into (good_rows, quarantine_rows).

    Cross-run idempotency (same nav_date loaded twice across separate loader
    runs) is handled by the ON CONFLICT DO NOTHING upsert in upsert_fact_mf_nav,
    not here. This function only resolves duplicates *within* one API payload.
    """
    good_by_date: dict = {}
    quarantined = []
    for raw_row in data_rows:
        raw_date = raw_row.get("date")
        raw_nav = raw_row.get("nav")
        nav_date = _parse_date(raw_date)
        if nav_date is None:
            quarantined.append(quarantine_entry(
                "silver.fact_mf_nav", raw_row,
                f"unparseable nav_date '{raw_date}' for scheme {scheme_code}",
            ))
            continue
        nav_value = _parse_nav(raw_nav)
        if nav_value is None:
            quarantined.append(quarantine_entry(
                "silver.fact_mf_nav", raw_row,
                f"invalid nav_value '{raw_nav}' for scheme {scheme_code} on {raw_date}",
            ))
            continue
        good_by_date[nav_date] = nav_value  # last-seen-in-batch wins

    good_rows = [
        {"scheme_code": scheme_code, "nav_date": d, "nav_value": v}
        for d, v in good_by_date.items()
    ]
    return good_rows, quarantined


def quarantine_entry(source_table: str, raw_row: dict, reason: str) -> dict:
    import json
    return {
        "quarantine_id": str(uuid.uuid4()),
        "source_table": source_table,
        "raw_payload": json.dumps(raw_row),
        "reason": reason,
    }


def anti_join_valid_scheme_codes(conn, scheme_codes: set[str]) -> set[str]:
    """Pre-merge anti-join (§2.2): which of these scheme_codes already exist
    in silver.dim_mf_scheme? Delta has no FK, so this check is what stands in
    for one here and in the eventual real Lakehouse."""
    if not scheme_codes:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scheme_code FROM silver.dim_mf_scheme WHERE scheme_code = ANY(%s)",
            (list(scheme_codes),),
        )
        return {row[0] for row in cur.fetchall()}


def upsert_dim_mf_scheme(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO silver.dim_mf_scheme
                (scheme_code, scheme_name, amc_name, scheme_category, scheme_type, plan, option, isin)
            VALUES %s
            ON CONFLICT (scheme_code) DO UPDATE SET
                scheme_name = EXCLUDED.scheme_name,
                amc_name = EXCLUDED.amc_name,
                scheme_category = EXCLUDED.scheme_category,
                scheme_type = EXCLUDED.scheme_type,
                plan = EXCLUDED.plan,
                option = EXCLUDED.option,
                isin = EXCLUDED.isin
            """,
            [
                (r["scheme_code"], r["scheme_name"], r["amc_name"], r["scheme_category"],
                 r["scheme_type"], r["plan"], r["option"], r["isin"])
                for r in rows
            ],
        )
    conn.commit()
    return len(rows)


def upsert_fact_mf_nav(conn, rows: list[dict]) -> int:
    """Idempotent insert on (scheme_code, nav_date) — §2.1 daily-refresh rule,
    applied uniformly here too: a rerun never creates a duplicate NAV row."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO silver.fact_mf_nav (nav_id, scheme_code, nav_date, nav_value)
            VALUES %s
            ON CONFLICT (scheme_code, nav_date) DO NOTHING
            """,
            [
                (f"{r['scheme_code']}:{r['nav_date'].isoformat()}", r["scheme_code"], r["nav_date"], r["nav_value"])
                for r in rows
            ],
        )
    conn.commit()
    return len(rows)


def insert_quarantine(conn, entries: list[dict]) -> int:
    if not entries:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO silver.dq_quarantine (quarantine_id, source_table, raw_payload, reason)
            VALUES %s
            """,
            [(e["quarantine_id"], e["source_table"], e["raw_payload"], e["reason"]) for e in entries],
        )
    conn.commit()
    return len(entries)
