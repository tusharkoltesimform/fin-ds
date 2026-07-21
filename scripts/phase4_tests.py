"""One-off verification script for Phase 4's five transaction functions.

Exercises the exit criteria from .claude/specs/phase4-transaction-logic.md:
  (a) a valid write commits and post-verifies
  (b) an over-limit/insufficient write is rejected and logged with the correct reason_code
  (c) two concurrent calls against the same key correctly serialize
  (d) a repeated call with the same idempotency_key returns the original result

All test rows are prefixed P4TEST_ and deleted at the end regardless of outcome.
Run with: .venv/Scripts/python.exe scripts/phase4_tests.py
"""
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mf_loader import config  # noqa: E402
import psycopg2  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


@contextmanager
def conn():
    c = psycopg2.connect(host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DATABASE,
                          user=config.PG_USER, password=config.PG_PASSWORD)
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


def setup(cur):
    cur.execute("""
        INSERT INTO core.dim_customer (customer_id, customer_type, full_name, pan, date_of_birth,
            kyc_status, risk_category, segment, customer_since, status)
        VALUES ('P4TEST_CUST1','individual','Test Customer','ABCDE1234F','1990-01-01',
            'verified','low','retail','2020-01-01','active')
    """)
    for acc_id, acc_type in [('P4TEST_DACC1','deposit'),('P4TEST_DACC2','deposit'),('P4TEST_DACC3','deposit'),
                              ('P4TEST_CACC1','card'),('P4TEST_LACC1','loan')]:
        cur.execute("""
            INSERT INTO core.dim_account (account_id, customer_id, account_type, product_name, open_date, status)
            VALUES (%s,'P4TEST_CUST1',%s,'Test Product','2020-01-01','active')
        """, (acc_id, acc_type))

    cur.execute("""
        INSERT INTO core.deposit_account (account_id, deposit_type, current_balance, min_balance, overdraft_limit)
        VALUES ('P4TEST_DACC1','savings',1000.00,0,NULL)
    """)
    cur.execute("""
        INSERT INTO core.deposit_account (account_id, deposit_type, current_balance, min_balance, overdraft_limit)
        VALUES ('P4TEST_DACC2','savings',2000.00,0,NULL)
    """)
    cur.execute("""
        INSERT INTO core.deposit_account (account_id, deposit_type, current_balance, min_balance, overdraft_limit)
        VALUES ('P4TEST_DACC3','savings',1000.00,0,NULL)
    """)
    cur.execute("""
        INSERT INTO core.card_master (card_id, account_id, customer_id, card_type, network, card_token,
            issue_date, expiry_date, status, credit_limit, current_statement_balance)
        VALUES ('P4TEST_CARD1','P4TEST_CACC1','P4TEST_CUST1','credit','visa','tok123',
            '2020-01-01','2030-01-01','active',1000.00,0)
    """)
    cur.execute("""
        INSERT INTO core.loan_account (loan_id, account_id, customer_id, loan_type, principal,
            outstanding_principal, status)
        VALUES ('P4TEST_LOAN1','P4TEST_LACC1','P4TEST_CUST1','personal',5000.00,5000.00,'active')
    """)
    cur.execute("""
        INSERT INTO core.mf_folio (folio_id, customer_id, amc_name, folio_number, open_date, status)
        VALUES ('P4TEST_FOLIO1','P4TEST_CUST1','Test AMC','FOLIO123','2020-01-01','active')
    """)
    cur.execute("""
        INSERT INTO core.mf_nav_ref (scheme_code, nav_date, nav_value)
        VALUES ('P4TESTSCHEME','2026-01-01',10.00000)
    """)


def cleanup(cur):
    stmts = [
        "DELETE FROM core.txn_rejection_log WHERE idempotency_key LIKE 'P4TEST_%'",
        "DELETE FROM core.fact_deposit_txn WHERE account_id LIKE 'P4TEST_%'",
        "DELETE FROM core.fact_card_txn WHERE card_id LIKE 'P4TEST_%'",
        "DELETE FROM core.fact_payment WHERE from_account_id LIKE 'P4TEST_%'",
        "DELETE FROM core.fact_loan_txn WHERE loan_id LIKE 'P4TEST_%'",
        "DELETE FROM core.fact_mf_transaction WHERE folio_id LIKE 'P4TEST_%'",
        "DELETE FROM core.mf_holding_current WHERE folio_id LIKE 'P4TEST_%'",
        "DELETE FROM core.mf_nav_ref WHERE scheme_code = 'P4TESTSCHEME'",
        "DELETE FROM core.mf_folio WHERE folio_id LIKE 'P4TEST_%'",
        "DELETE FROM core.loan_account WHERE loan_id LIKE 'P4TEST_%'",
        "DELETE FROM core.card_master WHERE card_id LIKE 'P4TEST_%'",
        "DELETE FROM core.deposit_account WHERE account_id LIKE 'P4TEST_%'",
        "DELETE FROM core.dim_account WHERE account_id LIKE 'P4TEST_%'",
        "DELETE FROM core.dim_customer WHERE customer_id LIKE 'P4TEST_%'",
    ]
    for s in stmts:
        cur.execute(s)


def main():
    with conn() as c:
        cur = c.cursor()
        cleanup(cur)  # in case a prior failed run left residue
        setup(cur)

        # ---- (a) valid deposit debit commits + post-verifies ----
        k1 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("SELECT core.post_deposit_txn('P4TEST_DACC1',100.00,'debit','atm',NULL,now(),%s)", (k1,))
        txn_id = cur.fetchone()[0]
        cur.execute("SELECT current_balance FROM core.deposit_account WHERE account_id='P4TEST_DACC1'")
        bal = cur.fetchone()[0]
        check("(a) valid deposit debit returns txn_id", txn_id is not None)
        check("(a) balance decremented correctly (1000 -> 900)", bal == 900.00, f"got {bal}")

        # ---- (b) rejection paths, each logged with the right reason_code ----
        k2 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("SELECT core.post_deposit_txn('P4TEST_DACC1',10000.00,'debit','atm',NULL,now(),%s)", (k2,))
        r2 = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k2,))
        row = cur.fetchone()
        check("(b) deposit over-balance rejected (NULL return)", r2 is None)
        check("(b) deposit rejection logged INSUFFICIENT_BALANCE", row and row[0] == 'INSUFFICIENT_BALANCE',
              f"got {row}")
        cur.execute("SELECT current_balance FROM core.deposit_account WHERE account_id='P4TEST_DACC1'")
        check("(b) balance unchanged after rejection", cur.fetchone()[0] == 900.00)

        k3 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_card_txn('P4TEST_CARD1',1500.00,'purchase','Test Merchant',
                        '5411','grocery','Mumbai','pos',NULL,NULL,now(),%s)""", (k3,))
        card_txn_id = cur.fetchone()[0]
        cur.execute("SELECT status FROM core.fact_card_txn WHERE card_txn_id=%s", (card_txn_id,))
        cstatus = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k3,))
        crow = cur.fetchone()
        check("(b) card over-limit purchase inserted as declined", cstatus == 'declined', f"got {cstatus}")
        check("(b) card rejection logged LIMIT_EXCEEDED", crow and crow[0] == 'LIMIT_EXCEEDED', f"got {crow}")

        k4 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_loan_txn('P4TEST_LOAN1',6000.00,'prepayment',6000.00,0,
                        CURRENT_DATE,%s)""", (k4,))
        r4 = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k4,))
        lrow = cur.fetchone()
        check("(b) loan prepayment over outstanding rejected", r4 is None)
        check("(b) loan rejection logged INSUFFICIENT_BALANCE", lrow and lrow[0] == 'INSUFFICIENT_BALANCE',
              f"got {lrow}")

        k5 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_mf_transaction('P4TEST_FOLIO1','P4TESTSCHEME','redemption',
                        CURRENT_DATE,'2026-01-01',100.00,FALSE,%s)""", (k5,))
        r5 = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k5,))
        mrow = cur.fetchone()
        check("(b) mf redemption with no units held rejected", r5 is None)
        check("(b) mf rejection logged UNITS_EXCEEDED", mrow and mrow[0] == 'UNITS_EXCEEDED', f"got {mrow}")

        k6 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_mf_transaction('P4TEST_FOLIO1','P4TESTSCHEME','purchase',
                        CURRENT_DATE,'2099-01-01',100.00,FALSE,%s)""", (k6,))
        r6 = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k6,))
        mrow2 = cur.fetchone()
        check("(b) mf purchase with unknown nav_date rejected", r6 is None)
        check("(b) mf rejection logged NAV_NOT_FOUND", mrow2 and mrow2[0] == 'NAV_NOT_FOUND', f"got {mrow2}")

        k7 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_payment('P4TEST_DACC2',5000.00,'upi',NULL,'test@upi','upi',
                        NULL,NULL,NULL,now(),%s)""", (k7,))
        r7 = cur.fetchone()[0]
        cur.execute("SELECT reason_code FROM core.txn_rejection_log WHERE idempotency_key=%s", (k7,))
        prow = cur.fetchone()
        check("(b) payment exceeding balance rejected", r7 is None)
        check("(b) payment rejection logged INSUFFICIENT_BALANCE", prow and prow[0] == 'INSUFFICIENT_BALANCE',
              f"got {prow}")

        # ---- valid mf purchase then valid redemption, to prove the happy path too ----
        k8 = f"P4TEST_{uuid.uuid4()}"
        cur.execute("""SELECT core.post_mf_transaction('P4TEST_FOLIO1','P4TESTSCHEME','purchase',
                        CURRENT_DATE,'2026-01-01',500.00,FALSE,%s)""", (k8,))
        r8 = cur.fetchone()[0]
        check("(a) mf purchase against real NAV commits", r8 is not None)
        cur.execute("""SELECT units_held, market_value FROM core.mf_holding_current
                        WHERE folio_id='P4TEST_FOLIO1' AND scheme_code='P4TESTSCHEME'""")
        uh, mv = cur.fetchone()
        check("(a) mf holding units_held = amount/nav (50 units)", float(uh) == 50.00000, f"got {uh}")
        check("(a) mf holding market_value = units*nav (500.00)", float(mv) == 500.00, f"got {mv}")

        # ---- (d) idempotency: repeat the very first debit call with the same key ----
        cur.execute("SELECT core.post_deposit_txn('P4TEST_DACC1',100.00,'debit','atm',NULL,now(),%s)", (k1,))
        replay_txn_id = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM core.fact_deposit_txn WHERE idempotency_key=%s", (k1,))
        replay_count = cur.fetchone()[0]
        cur.execute("SELECT current_balance FROM core.deposit_account WHERE account_id='P4TEST_DACC1'")
        bal_after_replay = cur.fetchone()[0]
        check("(d) idempotent replay returns the original txn_id", replay_txn_id == txn_id,
              f"orig={txn_id} replay={replay_txn_id}")
        check("(d) idempotent replay did not create a second row", replay_count == 1, f"count={replay_count}")
        check("(d) idempotent replay did not double-post the balance", bal_after_replay == 900.00,
              f"got {bal_after_replay}")

        # ---- (c) concurrency: two threads race a debit against a fresh account ----
        # (uses P4TEST_DACC3, untouched so far, so its running_balance chain is clean)
        results = {}
        barrier = threading.Barrier(2)

        def race(name):
            with conn() as c2:
                cur2 = c2.cursor()
                barrier.wait()
                key = f"P4TEST_{uuid.uuid4()}"
                cur2.execute(
                    "SELECT core.post_deposit_txn('P4TEST_DACC3',600.00,'debit','atm',NULL,now(),%s)", (key,))
                results[name] = cur2.fetchone()[0]

        t1 = threading.Thread(target=race, args=("t1",))
        t2 = threading.Thread(target=race, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        succeeded = [v for v in results.values() if v is not None]
        rejected = [v for v in results.values() if v is None]
        cur.execute("SELECT current_balance FROM core.deposit_account WHERE account_id='P4TEST_DACC3'")
        final_bal = cur.fetchone()[0]
        check("(c) exactly one of the two concurrent 600-debits succeeded",
              len(succeeded) == 1 and len(rejected) == 1, f"results={results}")
        check("(c) final balance reflects exactly one debit (1000 -> 400), no lost-update overdraw",
              final_bal == 400.00, f"got {final_bal}")

        cleanup(cur)
        cur.execute("SELECT COUNT(*) FROM core.dim_customer WHERE customer_id LIKE 'P4TEST_%'")
        check("post-test cleanup left no residue", cur.fetchone()[0] == 0)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    else:
        print("All Phase 4 exit-criteria checks PASSED.")


if __name__ == "__main__":
    main()
