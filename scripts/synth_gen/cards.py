"""Phase 3 §3.1 step 3 (card half) + step 4 (fact_card_txn).

card_master is bulk-loaded directly (pure dimension row) with
current_statement_balance = 0; every mutation afterwards goes through
core.post_card_txn. Note: the schema's balance-bearing pair
(credit_limit / current_statement_balance) is the only spending-capacity
model card_master has, and post_card_txn applies it uniformly regardless of
card_type -- so debit/prepaid cards are also given a credit_limit here,
standing in for "linked account ceiling" / "prepaid load amount"
respectively, since the schema doesn't carry a separate concept for them.
"""
import datetime as dt
import random

from faker import Faker
from psycopg2.extras import execute_values

from . import config, fake
from .txncall import FunctionCallBatcher

NETWORKS = ["visa", "mastercard", "rupay"]
ENTRY_MODES_PURCHASE = ["pos", "ecom", "contactless"]
MCC_CATEGORIES = [
    ("5411", "grocery"), ("5812", "dining"), ("5732", "electronics"), ("5941", "sporting_goods"),
    ("4111", "transport"), ("5999", "retail"), ("5541", "fuel"), ("4899", "utilities"),
    ("5311", "department_store"), ("7011", "travel"),
]
LIMIT_RANGE = {"credit": (50000, 500000), "debit": (100000, 2000000), "prepaid": (5000, 50000)}
DELIBERATE_REJECT_PROB = 0.0003


def _add_years(d: dt.date, years: int) -> dt.date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)  # Feb 29 -> Feb 28 in a non-leap target year


def create_cards(conn, rng: random.Random, faker: Faker, planned_accounts):
    rows = []
    for acc in planned_accounts:
        ctype = acc["subtype"]
        lo, hi = LIMIT_RANGE[ctype]
        credit_limit = round(rng.uniform(lo, hi), 2)
        issue_date = acc["open_date"]
        expiry_date = _add_years(issue_date, rng.choice([3, 4, 5]))
        payment_due_date = None
        if ctype == "credit":
            payment_due_date = dt.date.today().replace(day=1) + dt.timedelta(days=rng.randint(5, 20))

        # card_master.status is checked independently by post_card_txn (not
        # dim_account.status) -- inserted 'active' regardless of the account's
        # intended final state, same reasoning as spine.finalize_account_status.
        # finalize_card_status() applies the real final status after this
        # card's purchase history is done generating.
        rows.append((fake.new_id("CARD"), acc["account_id"], acc["customer_id"], ctype,
                     rng.choice(NETWORKS), fake.fake_card_token(rng), issue_date, expiry_date,
                     "active", credit_limit, 0, payment_due_date))
        acc["card_id"] = rows[-1][0]
        acc["credit_limit"] = credit_limit

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.card_master (card_id, account_id, customer_id, card_type, network,
                card_token, issue_date, expiry_date, status, credit_limit,
                current_statement_balance, payment_due_date)
            VALUES %s
        """, rows)
    conn.commit()


def _txn_dates(rng, open_date, end_date, n):
    if end_date <= open_date:
        end_date = open_date + dt.timedelta(days=1)
    span = (end_date - open_date).days
    base = dt.datetime.combine(open_date, dt.time())
    return sorted(
        base + dt.timedelta(days=rng.randint(0, span), hours=rng.randint(8, 22), minutes=rng.randint(0, 59))
        for _ in range(n)
    )


def generate_card_transactions(conn, rng: random.Random, faker: Faker, planned_accounts, target_total):
    today = dt.date.today()
    avg_per_account = max(1, target_total / max(1, len(planned_accounts)))
    batcher = FunctionCallBatcher(conn, config.COMMIT_BATCH_SIZE)
    approved = 0
    declined = 0

    sql = ("SELECT core.post_card_txn(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")

    for acc in planned_accounts:
        if "card_id" not in acc:
            continue
        end_date = acc["close_date"] or today
        n_txn = max(1, round(rng.gauss(avg_per_account, avg_per_account * 0.4)))
        dates = _txn_dates(rng, acc["open_date"], end_date, n_txn)

        stmt_balance = 0.0
        credit_limit = acc["credit_limit"]
        for txn_dt in dates:
            available = credit_limit - stmt_balance
            deliberate_test = rng.random() < DELIBERATE_REJECT_PROB and available > 0
            roll = rng.random()
            if deliberate_test:
                txn_type = "purchase"
                amount = round(available + rng.uniform(100000, 600000), 2)
            elif roll < 0.85:
                txn_type = "purchase"
                if available <= 50:
                    txn_type = "refund"
                    amount = round(rng.uniform(100, 10000), 2)
                else:
                    amount = round(rng.uniform(50, min(25000, available * 0.8)), 2)
            elif roll < 0.93:
                txn_type = "atm"
                if available <= 50:
                    txn_type = "refund"
                    amount = round(rng.uniform(100, 10000), 2)
                else:
                    amount = round(rng.uniform(50, min(15000, available * 0.8)), 2)
            elif roll < 0.98:
                txn_type = "refund"
                amount = round(rng.uniform(100, 10000), 2)
            else:
                txn_type = "reversal"
                amount = round(rng.uniform(100, 10000), 2)

            mcc, category = rng.choice(MCC_CATEGORIES)
            entry_mode = rng.choice(ENTRY_MODES_PURCHASE) if txn_type == "purchase" else "atm"
            interchange_fee = round(amount * 0.015, 2) if txn_type == "purchase" else None
            reward_points = int(amount // 100) if txn_type == "purchase" else None
            key = fake.new_id("CIDEMP")

            card_txn_id = batcher.call(sql, (
                acc["card_id"], amount, txn_type, faker.company(), mcc, category, faker.city(),
                entry_mode, interchange_fee, reward_points, txn_dt, key))

            if deliberate_test:
                declined += 1  # rejected by the function; mirror balance stays unaffected
            else:
                approved += 1
                if txn_type in ("purchase", "atm"):
                    stmt_balance += amount
                else:
                    stmt_balance -= amount

    batcher.flush()
    finalize_card_status(conn, planned_accounts)
    return {"approved": approved, "declined": declined, "total_calls": batcher.total_calls}


def finalize_card_status(conn, planned_accounts):
    """Mirrors spine.finalize_account_status, but for card_master.status --
    checked independently by post_card_txn. Cards are created 'active' so
    their purchase history can post; this applies each card's intended final
    status (blocked for a closed account, expired otherwise) once that
    history is done generating."""
    updates = []
    for acc in planned_accounts:
        if "card_id" not in acc or acc["status"] == "active":
            continue
        final_status = "blocked" if acc["status"] == "closed" else "expired"
        updates.append((acc["card_id"], final_status))
    if not updates:
        return
    with conn.cursor() as cur:
        execute_values(cur, """
            UPDATE core.card_master AS c SET status = v.status
            FROM (VALUES %s) AS v(card_id, status)
            WHERE c.card_id = v.card_id
        """, updates)
    conn.commit()
