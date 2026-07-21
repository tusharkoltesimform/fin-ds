"""Phase 3 §3.1 step 4 (fact_payment).

fact_payment shares its balance-bearing parent with whichever of
fact_deposit_txn / fact_card_txn already posted against that account, so
this module runs after both and reads each eligible account's *current*
balance from the DB once (rather than trying to carry a Python-side mirror
across module boundaries), then mirrors further mutations locally for the
handful of payments it generates per account.

Simplification: payment dates are drawn from a recent window (last ~9
months) rather than spread across the account's whole history, since doing
otherwise would require reconstructing point-in-time balances -- the
current balance this module reads is only representative of "now," not of
any arbitrary past date. Noted in the Phase 3 implementation log.
"""
import datetime as dt
import random

from faker import Faker

from . import config, fake
from .txncall import FunctionCallBatcher

RAILS = ["upi"] * 5 + ["imps"] * 2 + ["neft"] * 2 + ["rtgs"] * 1 + ["nach"] * 1 + ["bbps"] * 1 + ["swift"]
BENEFICIARY_TYPES = ["bank_account", "upi", "biller", "international"]
BILLER_CATEGORIES = ["electricity", "mobile_postpaid", "broadband", "insurance_premium", "credit_card_bill"]
DELIBERATE_REJECT_PROB = 0.0002


def _eligible_accounts(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.account_id, 'deposit', da.current_balance, da.min_balance,
                   COALESCE(da.overdraft_limit,0), da.deposit_type
            FROM core.dim_account a JOIN core.deposit_account da ON da.account_id = a.account_id
            WHERE a.status = 'active'
        """)
        deposits = cur.fetchall()
        cur.execute("""
            SELECT a.account_id, 'card', cm.current_statement_balance, 0,
                   COALESCE(cm.credit_limit,0), NULL
            FROM core.dim_account a JOIN core.card_master cm ON cm.account_id = a.account_id
            WHERE a.status = 'active'
        """)
        cards = cur.fetchall()
    return deposits + cards


def _available(acc_type, balance, min_balance, limit_or_od, dep_type):
    if acc_type == "deposit":
        return (balance + limit_or_od) if dep_type == "overdraft" else (balance - min_balance)
    return limit_or_od - balance  # card: available credit


def generate_payments(conn, rng: random.Random, faker: Faker, target_total):
    accounts = _eligible_accounts(conn)
    if not accounts:
        return {"approved": 0, "rejected": 0, "total_calls": 0}

    avg_per_account = max(0.1, target_total / len(accounts))
    today = dt.date.today()
    window_start = dt.datetime.combine(today - dt.timedelta(days=270), dt.time())

    batcher = FunctionCallBatcher(conn, config.COMMIT_BATCH_SIZE)
    approved = 0
    rejected = 0

    sql = "SELECT core.post_payment(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"

    for account_id, acc_type, balance, min_balance, limit_or_od, dep_type in accounts:
        n_pay = int(rng.random() < (avg_per_account % 1)) + int(avg_per_account)
        if n_pay == 0:
            continue
        balance = float(balance)
        min_balance = float(min_balance or 0)
        limit_or_od = float(limit_or_od or 0)

        for _ in range(n_pay):
            available = _available(acc_type, balance, min_balance, limit_or_od, dep_type)
            deliberate_test = rng.random() < DELIBERATE_REJECT_PROB and available > 0
            if deliberate_test:
                amount = round(available + rng.uniform(50000, 300000), 2)
            elif available <= 100:
                continue  # not enough headroom left for a safe payment this round
            else:
                amount = round(rng.uniform(50, min(50000, available * 0.7)), 2)

            rail = rng.choice(RAILS)
            btype = rng.choice(BENEFICIARY_TYPES)
            payer_vpa = f"{faker.user_name()}@upi" if rail == "upi" else None
            payee_vpa = f"{faker.user_name()}@upi" if rail == "upi" else None
            biller_category = rng.choice(BILLER_CATEGORIES) if btype == "biller" else None
            payment_dt = window_start + dt.timedelta(
                days=rng.randint(0, 270), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
            key = fake.new_id("PIDEMP")

            result = batcher.call(sql, (
                account_id, amount, rail, faker.name(), faker.iban() if btype == "bank_account" else payee_vpa,
                btype, payer_vpa, payee_vpa, biller_category, payment_dt, key))

            if result is not None:
                approved += 1
                # core.post_payment DECREASES current_balance for a deposit source
                # but INCREASES current_statement_balance for a card source
                # (spending available credit) -- the mirror must match either way.
                balance = balance - amount if acc_type == "deposit" else balance + amount
            else:
                rejected += 1

    batcher.flush()
    return {"approved": approved, "rejected": rejected, "total_calls": batcher.total_calls}
