"""Phase 3 orchestrator -- runs the generation order from
.claude/specs/phase3-synthetic-data-generation.md §3.1, end to end:

  1. Spine: dim_customer -> dim_account
  (2. Phase 2, already complete -- not generated here)
  3. deposit_account, card_master
  4. fact_deposit_txn, fact_card_txn, fact_payment
  5. credit_bureau_score -> loan_account -> fact_loan_txn -> loan_delinquency
  6. mf_folio -> fact_mf_transaction -> mf_holding_snapshot (HARD-GATED on Phase 2)
  7. customer_wealth_snapshot

Run with: .venv/Scripts/python.exe -m scripts.synth_gen.main
Env vars: SYNTH_SCALE (default 1.0), SYNTH_RANDOM_SEED, SYNTH_COMMIT_BATCH.
"""
import datetime as dt
import json
import random
import time

from faker import Faker

from . import config, credit_bureau, db, deposits, cards, loan_delinquency, loans
from . import mf_holding_snapshot, mf_ownership, payments, spine, wealth


def _phase2_gate(conn) -> None:
    """§2.3 hard gate, restated: MF ownership may not generate before Phase 2's
    real data is loaded and validated. Fail loudly rather than silently
    inventing scheme/NAV data if this isn't met."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM silver.dim_mf_scheme")
        n_schemes = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM core.mf_nav_ref")
        n_nav = cur.fetchone()[0]
    if n_schemes == 0 or n_nav == 0:
        raise RuntimeError(
            f"Phase 2 gate failed: silver.dim_mf_scheme has {n_schemes} rows, "
            f"core.mf_nav_ref has {n_nav} rows. Phase 3 step 6 (MF ownership) "
            "cannot run until Phase 2 is loaded and validated.")
    print(f"  Phase 2 gate OK: {n_schemes} schemes, {n_nav} NAV rows on file.")


def run():
    t0 = time.time()
    rng = random.Random(config.RANDOM_SEED)
    Faker.seed(config.RANDOM_SEED)
    faker = Faker("en_IN")

    run_log = {"started_at": dt.datetime.now().isoformat(), "seed": config.RANDOM_SEED, "scale": config.SCALE}

    with db.get_conn() as conn:
        print(f"[1/7] Spine: dim_customer x{config.N_CUSTOMERS} -> dim_account")
        customer_ids = spine.generate_customers(conn, rng, faker, config.N_CUSTOMERS)
        planned = spine.generate_accounts(conn, rng, customer_ids)
        print(f"  customers={len(customer_ids)} deposit_accts={len(planned['deposit'])} "
              f"card_accts={len(planned['card'])} loan_accts={len(planned['loan'])}")

        print("  Phase 2 gate check (must pass before MF ownership can run later)")
        _phase2_gate(conn)

        print(f"[2/7] deposit_account + fact_deposit_txn (target {config.N_DEPOSIT_TXN})")
        deposits.create_deposit_accounts(conn, rng, planned["deposit"])
        deposit_stats = deposits.generate_deposit_transactions(
            conn, rng, planned["deposit"], config.N_DEPOSIT_TXN)
        spine.finalize_account_status(conn, planned["deposit"])
        print(f"  approved={deposit_stats['approved']} deliberate_rejections={deposit_stats['rejected']}")

        deposit_accounts_by_customer = {}
        for acc in planned["deposit"]:
            deposit_accounts_by_customer.setdefault(acc["customer_id"], []).append(acc["account_id"])

        print(f"[3/7] card_master + fact_card_txn (target {config.N_CARD_TXN})")
        cards.create_cards(conn, rng, faker, planned["card"])
        card_stats = cards.generate_card_transactions(conn, rng, faker, planned["card"], config.N_CARD_TXN)
        spine.finalize_account_status(conn, planned["card"])
        print(f"  approved={card_stats['approved']} declined={card_stats['declined']}")

        print(f"[4/7] fact_payment (target {config.N_PAYMENT_TXN})")
        payment_stats = payments.generate_payments(conn, rng, faker, config.N_PAYMENT_TXN)
        print(f"  approved={payment_stats['approved']} rejected={payment_stats['rejected']}")

        print(f"[5/7] credit_bureau_score (target {config.N_CREDIT_BUREAU_PULLS}) -> loan_account "
              f"-> fact_loan_txn (target {config.N_LOAN_TXN}) -> loan_delinquency")
        bureau_by_customer = credit_bureau.generate_credit_bureau_scores(
            conn, rng, customer_ids, config.N_CREDIT_BUREAU_PULLS)
        loans.create_loan_accounts(conn, rng, planned["loan"], bureau_by_customer)
        loan_stats = loans.generate_loan_transactions(conn, rng, planned["loan"], config.N_LOAN_TXN)
        spine.finalize_account_status(conn, planned["loan"])
        print(f"  approved={loan_stats['approved']} rejected={loan_stats['rejected']}")
        emi_by_loan = {acc["loan_id"]: acc["emi_amount"] for acc in planned["loan"] if "loan_id" in acc}
        delinquency_stats = loan_delinquency.generate_loan_delinquency(
            conn, rng, loan_stats["loan_windows"], emi_by_loan)
        print(f"  loan_delinquency rows={delinquency_stats['rows']}")

        print(f"[6/7] mf_folio -> fact_mf_transaction (target {config.N_MF_TXN}) -> mf_holding_snapshot "
              "-- HARD-GATED ON PHASE 2")
        scheme_catalog = mf_ownership.load_scheme_catalog(conn)
        nav_map = mf_ownership.load_nav_map(conn)
        print(f"  loaded {len(scheme_catalog)} AMCs, {sum(len(v['dates']) for v in nav_map.values())} NAV rows")
        folios = mf_ownership.create_mf_folios(
            conn, rng, customer_ids, deposit_accounts_by_customer, scheme_catalog, nav_map, config.N_MF_FOLIOS)
        mf_stats = mf_ownership.generate_mf_transactions(conn, rng, folios, nav_map, config.N_MF_TXN)
        print(f"  folios={len(folios)} approved={mf_stats['approved']} rejected={mf_stats['rejected']}")
        holding_stats = mf_holding_snapshot.build_and_insert_holding_snapshots(conn, mf_stats["ledger"], nav_map)
        print(f"  mf_holding_snapshot rows={holding_stats['rows']}")

        print(f"[7/7] customer_wealth_snapshot (needs deposits + MF holdings)")
        wealth_stats = wealth.generate_wealth_snapshots(
            conn, rng, customer_ids, deposit_accounts_by_customer, deposit_stats["ledger"])
        print(f"  customer_wealth_snapshot rows={wealth_stats['rows']}")

        run_log.update({
            "customers": len(customer_ids),
            "deposit_accounts": len(planned["deposit"]),
            "card_accounts": len(planned["card"]),
            "loan_accounts": len(planned["loan"]),
            "deposit_txn": deposit_stats,
            "card_txn": {k: v for k, v in card_stats.items()},
            "payment_txn": payment_stats,
            "loan_txn": {k: v for k, v in loan_stats.items() if k != "loan_windows"},
            "loan_delinquency": delinquency_stats,
            "mf_folios": len(folios),
            "mf_txn": {k: v for k, v in mf_stats.items() if k != "ledger"},
            "mf_holding_snapshot": holding_stats,
            "customer_wealth_snapshot": wealth_stats,
        })

    run_log["elapsed_seconds"] = round(time.time() - t0, 1)
    config.LOG_DIR.mkdir(exist_ok=True)
    out_path = config.LOG_DIR / f"phase3_generation_{dt.datetime.now():%Y%m%dT%H%M%S}.json"

    def _default(o):
        return str(o)

    out_path.write_text(json.dumps(run_log, indent=2, default=_default))
    print(f"\nDone in {run_log['elapsed_seconds']}s. Run log: {out_path}")


if __name__ == "__main__":
    run()
