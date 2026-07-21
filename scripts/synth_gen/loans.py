"""Phase 3 §3.1 step 5 (loan_account, then fact_loan_txn).

loan_account is bulk-loaded directly with outstanding_principal = principal
(the loan's full balance owed at disbursal -- money movement only ever
*reduces* this field afterwards, via core.post_loan_txn's EMI/prepayment
path, so there's no "increase from 0" case for the function to model).
cibil_at_application samples from that customer's most recent real
credit_bureau_score pull before application_date (Phase 1's build-order
note), not invented independently.
"""
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import config, fake
from .txncall import FunctionCallBatcher

LOAN_PARAMS = {
    "personal": {"principal": (50_000, 1_000_000), "tenure": [12, 24, 36, 48, 60], "rate": (10.0, 16.0)},
    "auto":     {"principal": (200_000, 1_500_000), "tenure": [12, 24, 36, 48, 60, 84], "rate": (8.0, 12.0)},
    "mortgage": {"principal": (1_000_000, 10_000_000), "tenure": [60, 120, 180, 240], "rate": (7.0, 9.5)},
    "sme":      {"principal": (500_000, 5_000_000), "tenure": [12, 24, 36, 48, 60, 84], "rate": (9.0, 14.0)},
}
DELIBERATE_REJECT_PROB = 0.0005


def _emi(principal, annual_rate, tenure_months):
    r = annual_rate / 1200
    if r == 0:
        return round(principal / tenure_months, 2)
    factor = (1 + r) ** tenure_months
    return round(principal * r * factor / (factor - 1), 2)


def _cibil_for(pulls, application_date, rng):
    prior = [s for d, s in pulls if d <= application_date]
    if prior:
        return prior[-1]
    if pulls:
        return pulls[0][1]
    return rng.randint(600, 800)


def create_loan_accounts(conn, rng: random.Random, planned_accounts, bureau_by_customer):
    rows = []
    for acc in planned_accounts:
        ltype = acc["subtype"]
        params = LOAN_PARAMS[ltype]
        principal = round(rng.uniform(*params["principal"]), 2)
        tenure_months = rng.choice(params["tenure"])
        interest_rate = round(rng.uniform(*params["rate"]), 3)
        emi_amount = _emi(principal, interest_rate, tenure_months)
        application_date = acc["open_date"] - dt.timedelta(days=rng.randint(5, 30))
        disbursal_date = acc["open_date"]
        pulls = bureau_by_customer.get(acc["customer_id"], [])
        cibil = _cibil_for(pulls, application_date, rng)

        collection_status = None
        if acc["status"] == "written_off":
            collection_status = "in_collections"
        elif acc["status"] == "active" and rng.random() < 0.05:
            collection_status = rng.choice(["in_collections", "promise_to_pay"])

        loan_id = fake.new_id("LOAN")
        acc["loan_id"] = loan_id
        acc["principal"] = principal
        acc["tenure_months"] = tenure_months
        acc["interest_rate"] = interest_rate
        acc["emi_amount"] = emi_amount
        acc["disbursal_date"] = disbursal_date

        # outstanding_principal always starts at the full principal -- a written_off
        # loan reaches its partial-paydown state naturally, by the EMI loop below
        # stopping early (missed payments), not by seeding a smaller number here
        # that the mirror in generate_loan_transactions wouldn't know about.
        #
        # status is inserted as 'active' regardless of the loan's intended final
        # state (acc["status"], preserved for generate_loan_transactions/
        # loan_delinquency to consume) -- post_loan_txn rejects ANY posting
        # against a non-active loan (§1.2), so disbursal/EMI history can only be
        # posted while the loan is still active. The real final status is applied
        # as a direct lifecycle UPDATE after that history finishes generating,
        # the same way dim_account.status/close_date are set directly rather
        # than through a transaction function.
        rows.append((loan_id, acc["account_id"], acc["customer_id"], ltype,
                     round(principal * rng.uniform(0.95, 1.05), 2),
                     cibil, application_date, principal, interest_rate, tenure_months, emi_amount,
                     disbursal_date, principal, "active", collection_status))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.loan_account (loan_id, account_id, customer_id, loan_type, requested_amount,
                cibil_at_application, application_date, principal, interest_rate, tenure_months,
                emi_amount, disbursal_date, outstanding_principal, status, collection_status)
            VALUES %s
        """, rows)
    conn.commit()


def generate_loan_transactions(conn, rng: random.Random, planned_accounts, target_total):
    today = dt.date.today()
    n_loans = len([a for a in planned_accounts if "loan_id" in a])
    avg_emi = max(1, (target_total / max(1, n_loans)) - 1)  # -1 for the disbursal record

    batcher = FunctionCallBatcher(conn, config.COMMIT_BATCH_SIZE)
    approved = 0
    rejected = 0
    loan_windows = {}

    sql = "SELECT core.post_loan_txn(%s,%s,%s,%s,%s,%s,%s)"

    for acc in planned_accounts:
        if "loan_id" not in acc:
            continue
        loan_id = acc["loan_id"]
        principal = acc["principal"]
        emi_amount = acc["emi_amount"]
        interest_rate = acc["interest_rate"]
        tenure_months = acc["tenure_months"]
        disbursal_date = acc["disbursal_date"]

        # Disbursal record: does not reduce outstanding_principal (principal_paid=0).
        batcher.call(sql, (loan_id, principal, "disbursal", 0, 0, disbursal_date, fake.new_id("LIDEMP")))
        approved += 1

        months_elapsed = max(0, (today.year - disbursal_date.year) * 12 + (today.month - disbursal_date.month))
        n_emi = max(0, min(tenure_months, months_elapsed, max(1, round(rng.gauss(avg_emi, avg_emi * 0.4)))))
        if acc["status"] == "written_off":
            # Stopped paying partway through -- write-off is reached by the loop
            # ending early with a nonzero balance, not by a special-cased seed.
            n_emi = max(0, round(n_emi * rng.uniform(0.1, 0.6)))

        remaining = principal
        monthly_rate = interest_rate / 1200
        txn_date = disbursal_date

        for i in range(n_emi):
            txn_date = txn_date + dt.timedelta(days=30)
            if txn_date > today or remaining <= 0.01:
                break

            deliberate_test = rng.random() < DELIBERATE_REJECT_PROB
            if deliberate_test:
                amount = round(remaining + rng.uniform(50000, 500000), 2)
                result = batcher.call(sql, (loan_id, amount, "prepayment", amount, 0, txn_date, fake.new_id("LIDEMP")))
                rejected += 1
                continue

            is_prepayment = rng.random() < 0.05 and remaining > emi_amount
            if is_prepayment:
                principal_paid = round(min(remaining, rng.uniform(emi_amount, remaining)), 2)
                interest_paid = 0.0
                amount = principal_paid
                txn_type = "prepayment"
            else:
                interest_paid = round(remaining * monthly_rate, 2)
                principal_paid = round(min(remaining, max(emi_amount - interest_paid, 0)), 2)
                amount = round(principal_paid + interest_paid, 2)
                txn_type = "emi"

            result = batcher.call(sql, (loan_id, amount, txn_type, principal_paid, interest_paid,
                                         txn_date, fake.new_id("LIDEMP")))
            if result is not None:
                approved += 1
                remaining = round(remaining - principal_paid, 2)
            else:
                rejected += 1

        loan_windows[loan_id] = {"start": disbursal_date, "months": n_emi, "status": acc["status"]}

    batcher.flush()
    finalize_loan_status(conn, planned_accounts)
    return {"approved": approved, "rejected": rejected, "total_calls": batcher.total_calls,
             "loan_windows": loan_windows}


def finalize_loan_status(conn, planned_accounts):
    """Mirrors spine.finalize_account_status, but for loan_account.status --
    a separate column from dim_account.status, checked independently by
    post_loan_txn. Loans are created 'active' so their disbursal/EMI history
    can post; this applies each loan's intended final status once that
    history is done generating."""
    updates = [(acc["loan_id"], acc["status"]) for acc in planned_accounts
               if "loan_id" in acc and acc["status"] != "active"]
    if not updates:
        return
    with conn.cursor() as cur:
        execute_values(cur, """
            UPDATE core.loan_account AS l SET status = v.status
            FROM (VALUES %s) AS v(loan_id, status)
            WHERE l.loan_id = v.loan_id
        """, updates)
    conn.commit()
