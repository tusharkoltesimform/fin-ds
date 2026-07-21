"""Phase 2 §2.2 — daily incremental refresh job.

Built here, intended to run once daily (~21:00 IST) from here on,
independent of the one-time full_load.py build above. This script is not
registered with a scheduler in this repo (Windows Task Scheduler / cron is
an operational deploy step) -- see implemented/phase2-mf-api-ingestion.md
for how to wire it up.

For each scheme already in silver.dim_mf_scheme: refetch /mf/{code} (the API
has no "since" filter, it always returns full history) and keep only rows
with nav_date > the scheme's current MAX(nav_date) on file. A day with zero
new rows for a scheme (weekend/holiday) is an expected gap, not a failure.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

from . import api_client, bronze, config, db, silver, sync_core


def _max_nav_dates(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code, MAX(nav_date) FROM silver.fact_mf_nav GROUP BY scheme_code")
        return dict(cur.fetchall())


def _all_scheme_codes(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code FROM silver.dim_mf_scheme ORDER BY scheme_code")
        return [row[0] for row in cur.fetchall()]


def _refresh_one(scheme_code: str, watermark: date | None):
    with db.get_conn() as conn:
        try:
            payload = api_client.fetch_mf_scheme(scheme_code)
        except api_client.MFApiError as exc:
            return {"scheme_code": scheme_code, "status": "FAILED", "reason": str(exc)}
        bronze.land_mf_scheme(conn, scheme_code, payload)

    data_rows = payload.get("data") or []
    good_rows, quarantine = silver.cast_nav_rows(scheme_code, data_rows)
    new_rows = [r for r in good_rows if watermark is None or r["nav_date"] > watermark]

    status = "OK" if new_rows else "NO_NEW_ROWS"
    return {
        "scheme_code": scheme_code,
        "status": status,
        "nav_rows": new_rows,
        "quarantine": quarantine,
    }


def run() -> dict:
    started = time.time()
    with db.get_conn() as conn:
        scheme_codes = _all_scheme_codes(conn)
        watermarks = _max_nav_dates(conn)

    if not scheme_codes:
        raise RuntimeError(
            "silver.dim_mf_scheme is empty — run full_load.py first (Phase 2 §2.1 one-time load)."
        )

    results = []
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {
            pool.submit(_refresh_one, code, watermarks.get(code)): code for code in scheme_codes
        }
        for future in as_completed(futures):
            results.append(future.result())

    failed = [r for r in results if r["status"] == "FAILED"]
    no_new = [r for r in results if r["status"] == "NO_NEW_ROWS"]
    refreshed = [r for r in results if r["status"] == "OK"]

    all_nav_rows = [row for r in refreshed for row in r["nav_rows"]]
    all_quarantine = [entry for r in refreshed for entry in r["quarantine"]]

    with db.get_conn() as conn:
        n_nav = silver.upsert_fact_mf_nav(conn, all_nav_rows)
        n_quarantine = silver.insert_quarantine(conn, all_quarantine)
        n_synced = sync_core.sync_mf_nav_ref(conn, all_nav_rows)

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "schemes_checked": len(scheme_codes),
        "schemes_with_new_rows": len(refreshed),
        "schemes_no_new_rows": len(no_new),
        "schemes_failed": len(failed),
        "failed_schemes": [{"scheme_code": r["scheme_code"], "reason": r["reason"]} for r in failed],
        "nav_rows_upserted": n_nav,
        "quarantine_rows_written": n_quarantine,
        "core_mf_nav_ref_synced": n_synced,
        "elapsed_seconds": round(time.time() - started, 1),
    }

    config.LOG_DIR.mkdir(exist_ok=True)
    log_path = config.LOG_DIR / f"mf_daily_refresh_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    log_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Run log: {log_path}", file=sys.stderr)

    if failed:
        # §2.1: do not skip a failed scheme's refresh silently -- alert.
        print(f"ALERT: {len(failed)} scheme(s) FAILED today's refresh: "
              f"{[f['scheme_code'] for f in failed]}", file=sys.stderr)

    return summary


if __name__ == "__main__":
    run()
