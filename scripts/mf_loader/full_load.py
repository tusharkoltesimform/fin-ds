"""Phase 2 §2.1 — one-time full-history load.

1. GET /mf -> candidate scheme universe; land bronze.
2. Curate SCHEME_LIMIT schemes (fixed-seed random sample of the universe, so
   the pick is reproducible and not biased toward old delisted schemes at
   the low end of scheme_code).
3. GET /mf/{code} per scheme (threaded) -> land bronze, cast to silver rows.
4. Write silver.dim_mf_scheme, silver.fact_mf_nav, silver.dq_quarantine.
5. Sync core.mf_nav_ref.
6. Any scheme that failed all retries is logged and excluded, not silently
   missing (§2.3) -> logs/mf_full_load_<ts>.json.
"""
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import api_client, bronze, config, db, silver, sync_core


def _curate_scheme_codes(mf_list: list[dict]) -> list[str]:
    candidates = [
        str(row["schemeCode"])
        for row in mf_list
        if row.get("schemeCode") and row.get("schemeName")
    ]
    rng = random.Random(config.RANDOM_SEED)
    rng.shuffle(candidates)
    return sorted(candidates[: config.SCHEME_LIMIT], key=int)


def _fetch_and_transform_one(scheme_code: str):
    """Runs in a worker thread: own DB connection for bronze landing, pure
    transform for silver (no silver writes here — those are batched by the
    caller after all workers finish, so upserts + anti-join run once)."""
    with db.get_conn() as conn:
        try:
            payload = api_client.fetch_mf_scheme(scheme_code)
        except api_client.MFApiError as exc:
            return {"scheme_code": scheme_code, "status": "FAILED", "reason": str(exc)}

        bronze.land_mf_scheme(conn, scheme_code, payload)

    meta = payload.get("meta") or {}
    data_rows = payload.get("data") or []

    dim_row, dim_reason = silver.build_dim_row(meta)
    nav_rows, nav_quarantine = silver.cast_nav_rows(scheme_code, data_rows)

    result = {
        "scheme_code": scheme_code,
        "status": "OK",
        "dim_row": dim_row,
        "nav_rows": nav_rows,
        "quarantine": list(nav_quarantine),
    }
    if dim_reason:
        result["quarantine"].append(silver.quarantine_entry(
            "silver.dim_mf_scheme", meta, dim_reason,
        ))
    return result


def run() -> dict:
    started = time.time()
    with db.get_conn() as conn:
        mf_list = api_client.fetch_mf_list()
        bronze.land_mf_list(conn, mf_list)

    scheme_codes = _curate_scheme_codes(mf_list)
    print(f"Universe: {len(mf_list)} schemes. Curated {len(scheme_codes)} for this load "
          f"(seed={config.RANDOM_SEED}).", file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_and_transform_one, code): code for code in scheme_codes}
        done = 0
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if done % 50 == 0 or done == len(scheme_codes):
                print(f"  fetched {done}/{len(scheme_codes)}", file=sys.stderr)

    failed = [r for r in results if r["status"] == "FAILED"]
    succeeded = [r for r in results if r["status"] == "OK"]

    dim_rows = [r["dim_row"] for r in succeeded if r["dim_row"] is not None]
    all_nav_rows = [row for r in succeeded for row in r["nav_rows"]]
    all_quarantine = [entry for r in succeeded for entry in r["quarantine"]]

    with db.get_conn() as conn:
        n_dim = silver.upsert_dim_mf_scheme(conn, dim_rows)

        valid_codes = silver.anti_join_valid_scheme_codes(
            conn, {row["scheme_code"] for row in all_nav_rows}
        )
        orphan_nav_rows = [row for row in all_nav_rows if row["scheme_code"] not in valid_codes]
        clean_nav_rows = [row for row in all_nav_rows if row["scheme_code"] in valid_codes]
        for row in orphan_nav_rows:
            all_quarantine.append(silver.quarantine_entry(
                "silver.fact_mf_nav", row,
                f"scheme_code {row['scheme_code']} not present in silver.dim_mf_scheme (anti-join)",
            ))

        n_nav = silver.upsert_fact_mf_nav(conn, clean_nav_rows)
        n_quarantine = silver.insert_quarantine(conn, all_quarantine)
        n_synced = sync_core.sync_mf_nav_ref(conn, clean_nav_rows)

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "universe_size": len(mf_list),
        "curated_schemes": len(scheme_codes),
        "schemes_succeeded": len(succeeded),
        "schemes_failed": len(failed),
        "failed_schemes": [{"scheme_code": r["scheme_code"], "reason": r["reason"]} for r in failed],
        "dim_rows_upserted": n_dim,
        "nav_rows_attempted": len(all_nav_rows),
        "nav_rows_orphaned_anti_join": len(orphan_nav_rows),
        "nav_rows_upserted": n_nav,
        "quarantine_rows_written": n_quarantine,
        "core_mf_nav_ref_synced": n_synced,
        "elapsed_seconds": round(time.time() - started, 1),
    }

    config.LOG_DIR.mkdir(exist_ok=True)
    log_path = config.LOG_DIR / f"mf_full_load_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    log_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Run log: {log_path}", file=sys.stderr)
    return summary


if __name__ == "__main__":
    run()
