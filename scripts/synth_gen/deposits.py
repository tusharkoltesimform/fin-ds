"""Phase 3 §3.1 step 3 (deposit half) + step 4 (fact_deposit_txn).

deposit_account is bulk-loaded directly (§3.2: pure dimension row, no
concurrent-write risk) but always starts at current_balance = 0 -- the
balance-bearing field itself is never set to a nonzero value except through
core.post_deposit_txn, which is the only thing that ever UPDATEs it
afterwards. The account's real opening balance is therefore generated as an
ordinary first credit transaction, through the same function as every other
transaction on that account, not as a special case.
"""
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import config, fake
from .txncall import FunctionCallBatcher

CHANNELS = ["branch", "atm", "netbanking", "mobile", "upi"]
DELIBERATE_REJECT_PROB = 0.0002

INTEREST_RATE = {"savings": (3.0, 4.0), "checking": (0.0, 0.5), "term": (6.0, 7.5), "overdraft": (10.0, 14.0)}
MIN_BALANCE = {"savings": (1000, 10000), "checking": (0, 0), "term": (0, 0), "overdraft": (0, 0)}
OPENING_AMOUNT = {"savings": (5000, 500000), "checking": (10000, 1000000),
                   "term": (50000, 2000000), "overdraft": (0, 0)}


def create_deposit_accounts(conn, rng: random.Random, planned_accounts):
    rows = []
    for acc in planned_accounts:
        dtype = acc["subtype"]
        lo, hi = MIN_BALANCE[dtype]
        min_balance = rng.uniform(lo, hi) if hi > lo else lo
        rlo, rhi = INTEREST_RATE[dtype]
        interest_rate = round(rng.uniform(rlo, rhi), 3)
        term_months = None
        maturity_date = None
        overdraft_limit = None
        if dtype == "term":
            term_months = rng.choice([12, 24, 36, 60])
            maturity_date = acc["open_date"] + dt.timedelta(days=30 * term_months)
        if dtype == "overdraft":
            overdraft_limit = round(rng.uniform(10000, 200000), 2)

        min_balance = round(min_balance, 2)
        acc["min_balance"] = min_balance
        acc["overdraft_limit"] = overdraft_limit or 0.0

        rows.append((acc["account_id"], dtype, 0, interest_rate, min_balance,
                     term_months, maturity_date, overdraft_limit))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.deposit_account (account_id, deposit_type, current_balance,
                interest_rate, min_balance, term_months, maturity_date, overdraft_limit)
            VALUES %s
        """, rows)
    conn.commit()


def _txn_dates(rng, open_date, end_date, n):
    if end_date <= open_date:
        end_date = open_date + dt.timedelta(days=1)
    span = (end_date - open_date).days
    base = dt.datetime.combine(open_date, dt.time())
    # Sort the fully-formed datetimes (not just day offsets) so same-day entries
    # can't land out of order -- the running_balance trigger orders by the real
    # txn_datetime, and processing must match that exact chronological order.
    dates = sorted(
        base + dt.timedelta(days=rng.randint(0, span), hours=rng.randint(8, 20), minutes=rng.randint(0, 59))
        for _ in range(n)
    )
    # Two entries landing on the exact same minute would tie under the trigger's
    # strict "<" ordering (it would skip straight past both to find a prior row,
    # instead of chaining the second off the first) -- force strict monotonicity
    # so every row this account gets has a provably distinct, correctly-ordered
    # predecessor.
    for i in range(1, len(dates)):
        if dates[i] <= dates[i - 1]:
            dates[i] = dates[i - 1] + dt.timedelta(seconds=1)
    return dates


def generate_deposit_transactions(conn, rng: random.Random, planned_accounts, target_total):
    today = dt.date.today()
    avg_per_account = max(1, target_total / max(1, len(planned_accounts)))
    batcher = FunctionCallBatcher(conn, config.COMMIT_BATCH_SIZE)
    approved = 0
    deliberate_rejections = 0
    ledger = {}  # account_id -> sorted list of (txn_date, balance_after) for wealth-snapshot reconstruction

    sql = ("SELECT core.post_deposit_txn(%s,%s,%s,%s,%s,%s,%s)")

    for acc in planned_accounts:
        dtype = acc["subtype"]
        end_date = acc["close_date"] or today
        n_txn = max(1, round(rng.gauss(avg_per_account, avg_per_account * 0.4)))
        dates = _txn_dates(rng, acc["open_date"], end_date, n_txn)

        balance = 0.0
        opening_lo, opening_hi = OPENING_AMOUNT[dtype]

        for i, txn_dt in enumerate(dates):
            if i == 0 and dtype != "overdraft":
                amount = round(rng.uniform(opening_lo, opening_hi), 2)
                dr_cr = "credit"
            else:
                deliberate_test = rng.random() < DELIBERATE_REJECT_PROB
                headroom = (balance + acc["overdraft_limit"]) if dtype == "overdraft" \
                    else (balance - acc["min_balance"])
                if deliberate_test and headroom > 0:
                    dr_cr = "debit"
                    amount = round(headroom + rng.uniform(50000, 500000), 2)
                else:
                    deliberate_test = False
                    # Force a credit whenever headroom is too thin for a safe debit,
                    # so only the deliberate test above can ever exceed it.
                    dr_cr = "credit" if (rng.random() < 0.45 or headroom <= 100) else "debit"
                    if dr_cr == "credit":
                        amount = round(rng.uniform(500, 100000), 2)
                    else:
                        amount = round(rng.uniform(20, min(50000, headroom * 0.7)), 2)

            channel = rng.choice(CHANNELS)
            narration = f"{dr_cr.upper()} via {channel}"
            key = fake.new_id("DIDEMP")

            result = batcher.call(sql, (acc["account_id"], amount, dr_cr, channel, narration, txn_dt, key))
            if result is not None:
                approved += 1
                balance = balance - amount if dr_cr == "debit" else balance + amount
                ledger.setdefault(acc["account_id"], []).append((txn_dt.date(), balance))
            else:
                deliberate_rejections += 1

    batcher.flush()
    return {"approved": approved, "rejected": deliberate_rejections, "total_calls": batcher.total_calls,
            "ledger": ledger}
