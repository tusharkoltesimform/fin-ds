"""Phase 3 §3.1 step 5 (credit_bureau_score) -- generated BEFORE loan_account
per Phase 1's build-order note, so each loan's cibil_at_application can
sample from a real prior pull for that customer rather than being invented
independently.

Lakehouse-native batch table (silver.credit_bureau_score), no concurrent-
write risk -- bulk-loaded directly.
"""
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import config, fake

BUREAUS = ["CIBIL", "Experian", "Equifax", "CRIF Highmark"]


def _band(score: int) -> str:
    if score >= 750:
        return "excellent"
    if score >= 650:
        return "good"
    if score >= 550:
        return "fair"
    return "poor"


def generate_credit_bureau_scores(conn, rng: random.Random, customer_ids, target_total):
    today = dt.date.today()
    history_start = today - dt.timedelta(days=365 * config.HISTORY_YEARS)

    avg_pulls = max(1.0, target_total / max(1, len(customer_ids)))
    by_customer = {}
    rows = []

    for cust_id in customer_ids:
        n_pulls = max(1, round(rng.gauss(avg_pulls, 0.6)))
        base_score = int(rng.gauss(720, 90))
        pull_dates = sorted(
            history_start + dt.timedelta(days=rng.randint(0, (today - history_start).days))
            for _ in range(n_pulls)
        )
        pulls = []
        for pull_date in pull_dates:
            score = max(300, min(900, base_score + rng.randint(-40, 40)))
            score_id = fake.new_id("SCORE")
            rows.append((score_id, cust_id, rng.choice(BUREAUS), score, _band(score), pull_date))
            pulls.append((pull_date, score))
        by_customer[cust_id] = pulls

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO silver.credit_bureau_score (score_id, customer_id, bureau, score, band, pull_date)
            VALUES %s
        """, rows)
    conn.commit()
    return by_customer
