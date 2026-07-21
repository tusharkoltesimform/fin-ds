"""Phase 3 §3.1 step 5 (loan_delinquency) -- Lakehouse-native derived batch
table, no OLTP, bulk-loaded directly.

risk_stage / days_past_due are generated together from one DPD state machine
per loan per month, so the cross-column consistency Phase 1 flagged (e.g.
risk_stage=1 on a 120-DPD loan) can't arise here by construction -- Phase 5
re-checks this on the full backfill, this just doesn't manufacture the fault.
"""
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import fake

PROVISION_RATE = {1: 0.01, 2: 0.10, 3: 0.50}


def _stage_and_bucket(dpd: int):
    if dpd == 0:
        return 1, "0"
    if dpd <= 30:
        return 1, "1-30"
    if dpd <= 60:
        return 2, "31-60"
    if dpd <= 90:
        return 2, "61-90"
    return 3, "90+"


def generate_loan_delinquency(conn, rng: random.Random, loan_windows, emi_by_loan):
    rows = []
    for loan_id, window in loan_windows.items():
        start = window["start"]
        n_months = window["months"]
        emi_amount = emi_by_loan.get(loan_id, 0)
        dpd = 0
        for m in range(1, n_months + 1):
            snapshot_date = start + dt.timedelta(days=30 * m)
            roll = rng.random()
            if dpd > 0:
                # already delinquent: mostly climbs or clears
                if roll < 0.35:
                    dpd = 0
                else:
                    dpd += 30
            else:
                if roll < 0.06:
                    dpd = 30
            if window["status"] == "written_off" and m == n_months:
                dpd = max(dpd, 120)

            stage, bucket = _stage_and_bucket(dpd)
            overdue_amount = round(emi_amount * (dpd // 30), 2) if dpd > 0 else 0.0
            provision_amount = round(overdue_amount * PROVISION_RATE[stage], 2)

            rows.append((fake.new_id("DELQ"), loan_id, snapshot_date, dpd, bucket,
                         overdue_amount, stage, provision_amount))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO gold.loan_delinquency (snapshot_id, loan_id, snapshot_date, days_past_due,
                dpd_bucket, overdue_amount, risk_stage, provision_amount)
            VALUES %s
        """, rows)
    conn.commit()
    return {"rows": len(rows)}
