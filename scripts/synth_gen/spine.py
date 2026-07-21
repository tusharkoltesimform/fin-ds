"""Phase 3 §3.1 step 1 -- the spine: dim_customer -> dim_account.

Both are pure dimension tables with no concurrent-write risk (§3.2), bulk-
loaded directly. dim_account rows are created here, tagged with the subtype
attributes (deposit_type / card_type / loan_type) their subtype table will
need, so the later per-domain generators don't have to re-derive them and
risk drifting from what's actually in dim_account.
"""
import datetime as dt
import random

from faker import Faker
from psycopg2.extras import execute_values

from . import config, fake

DEPOSIT_TYPES = ["savings", "savings", "savings", "checking", "checking", "term", "overdraft"]
CARD_TYPES = ["credit", "credit", "debit", "debit", "debit", "prepaid"]
LOAN_TYPES = ["personal", "personal", "auto", "mortgage", "sme"]

DEPOSIT_PRODUCT_NAME = {
    "savings": "Savings Account", "checking": "Current Account",
    "term": "Fixed Deposit", "overdraft": "Overdraft Account",
}
CARD_PRODUCT_NAME = {
    "credit": "Credit Card", "debit": "Debit Card", "prepaid": "Prepaid Card",
}
LOAN_PRODUCT_NAME = {
    "personal": "Personal Loan", "auto": "Auto Loan",
    "mortgage": "Home Loan", "sme": "SME Business Loan",
}

KYC_STATUS = ["verified"] * 8 + ["pending"] * 1 + ["failed"] * 1
RISK_CATEGORY = ["low"] * 6 + ["medium"] * 3 + ["high"] * 1
SEGMENT = ["retail"] * 7 + ["hni"] * 2 + ["sme"] * 1
INCOME_BAND = ["<5L", "5-10L", "10-25L", "25-50L", "50L+"]
CUSTOMER_STATUS = ["active"] * 9 + ["dormant"] * 1


def _random_date(rng: random.Random, start: dt.date, end: dt.date) -> dt.date:
    span = (end - start).days
    if span <= 0:
        return start
    return start + dt.timedelta(days=rng.randint(0, span))


def assign_customers(rng: random.Random, customer_ids, count: int):
    """Returns a list of `count` customer_ids to attach `count` rows to.

    Whenever count >= len(customer_ids), every customer is guaranteed at
    least one row (shuffled full pass first, then random for the remainder)
    instead of leaving coverage to chance -- important at small scale
    (SYNTH_MICRO) where a handful of rng.choice() calls could otherwise skip
    a customer entirely. At full scale this only changes behavior for the
    two subtype counts that already exceed N_CUSTOMERS (deposit, card); for
    counts below N_CUSTOMERS (loan, MF folio) it's identical to plain
    rng.choice sampling, preserving "not every customer has a loan" as
    intended there.
    """
    if count >= len(customer_ids):
        assigned = list(customer_ids)
        rng.shuffle(assigned)
        assigned += [rng.choice(customer_ids) for _ in range(count - len(customer_ids))]
        rng.shuffle(assigned)
        return assigned
    return [rng.choice(customer_ids) for _ in range(count)]


def generate_customers(conn, rng: random.Random, faker: Faker, n: int):
    today = dt.date.today()
    history_start = today - dt.timedelta(days=365 * config.HISTORY_YEARS)
    ancient_start = today - dt.timedelta(days=365 * 40)  # DOB range

    rows = []
    ids = []
    for _ in range(n):
        cust_id = fake.new_id("CUST")
        ids.append(cust_id)
        city, state, ifsc = rng.choice(config.INDIAN_CITIES)
        rows.append((
            cust_id,
            "sme" if rng.random() < 0.1 else "individual",
            faker.name(),
            fake.fake_pan(rng),
            fake.fake_aadhaar_token(rng),
            _random_date(rng, ancient_start, today - dt.timedelta(days=365 * 18)),
            rng.choice(KYC_STATUS),
            rng.choice(RISK_CATEGORY),
            rng.choice(SEGMENT),
            rng.choice(INCOME_BAND),
            city, state, ifsc,
            faker.name() if rng.random() < 0.6 else None,
            _random_date(rng, history_start, today),
            rng.choice(CUSTOMER_STATUS),
        ))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.dim_customer (customer_id, customer_type, full_name, pan, aadhaar_token,
                date_of_birth, kyc_status, risk_category, segment, income_band, home_branch_city,
                home_branch_state, home_branch_ifsc, relationship_manager, customer_since, status)
            VALUES %s
        """, rows)
    conn.commit()
    return ids


def _account_row(rng, cust_id, acc_type, subtype, today, history_start):
    acc_id = fake.new_id("ACC")
    open_date = _random_date(rng, history_start, today - dt.timedelta(days=30))
    if acc_type == "loan":
        status_pool = ["active"] * 8 + ["closed"] * 1 + ["written_off"] * 1
    else:
        status_pool = ["active"] * 9 + ["closed"] * 1
    status = rng.choice(status_pool)
    close_date = None
    if status in ("closed", "written_off"):
        close_date = _random_date(rng, open_date + dt.timedelta(days=30), today)

    product_name = {"deposit": DEPOSIT_PRODUCT_NAME, "card": CARD_PRODUCT_NAME,
                    "loan": LOAN_PRODUCT_NAME}[acc_type][subtype]

    return acc_id, open_date, status, close_date, product_name


def generate_accounts(conn, rng: random.Random, customer_ids):
    today = dt.date.today()
    history_start = today - dt.timedelta(days=365 * config.HISTORY_YEARS)

    plan = [
        ("deposit", DEPOSIT_TYPES, config.N_DEPOSIT_ACCOUNTS),
        ("card", CARD_TYPES, config.N_CARD_ACCOUNTS),
        ("loan", LOAN_TYPES, config.N_LOAN_ACCOUNTS),
    ]

    result = {"deposit": [], "card": [], "loan": []}
    rows = []
    for acc_type, subtype_pool, count in plan:
        for cust_id in assign_customers(rng, customer_ids, count):
            subtype = rng.choice(subtype_pool)
            acc_id, open_date, status, close_date, product_name = _account_row(
                rng, cust_id, acc_type, subtype, today, history_start)
            # Every account is inserted 'active' regardless of its intended final
            # lifecycle state: core.post_deposit_txn / post_card_txn / post_loan_txn
            # all reject a write against a non-active account (§1.2), so the
            # historical backfill can only be posted while the account is still
            # active. Its subtype generator (deposits.py/cards.py/loans.py) applies
            # the real final status/close_date as a direct lifecycle UPDATE once
            # that account's transaction history is done generating -- `status`/
            # `close_date` here (and in the returned dict) are that *intended*
            # final state, not what gets bulk-inserted.
            rows.append((acc_id, cust_id, acc_type, product_name, "INR", open_date, "active", None))
            result[acc_type].append({
                "account_id": acc_id, "customer_id": cust_id, "subtype": subtype,
                "open_date": open_date, "status": status, "close_date": close_date,
            })

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.dim_account (account_id, customer_id, account_type, product_name,
                currency, open_date, status, close_date)
            VALUES %s
        """, rows)
    conn.commit()
    return result


def finalize_account_status(conn, planned_accounts):
    """Applies each account's intended final status/close_date (planned by
    generate_accounts, held in acc["status"]/acc["close_date"]) as a direct
    lifecycle UPDATE, once that account's subtype generator has finished
    posting its historical transactions while dim_account was still 'active'.
    A no-op for accounts whose intended final state is already 'active'.
    """
    updates = [(acc["account_id"], acc["status"], acc["close_date"])
               for acc in planned_accounts if acc["status"] != "active"]
    if not updates:
        return
    with conn.cursor() as cur:
        execute_values(cur, """
            UPDATE core.dim_account AS d SET status = v.status, close_date = v.close_date
            FROM (VALUES %s) AS v(account_id, status, close_date)
            WHERE d.account_id = v.account_id
        """, updates)
    conn.commit()
