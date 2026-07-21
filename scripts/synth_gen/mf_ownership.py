"""Phase 3 §3.1 step 6 -- MF ownership: mf_folio -> fact_mf_transaction.

HARD-GATED ON PHASE 2 (§2.3): every scheme_code/nav_date this module touches
is sampled from what's already in silver.dim_mf_scheme / core.mf_nav_ref --
never invented. If a scheme has zero usable NAV rows (e.g. the three
all-zero-NAV schemes quarantined in Phase 2), it simply has no entries in
the in-memory nav map built here and is skipped, the same way it would be
structurally impossible to reference via the FK if this code had a bug.

MF activity is generated over a shorter, more recent window
(config.MF_HISTORY_MONTHS) than the rest of the model -- see config.py's
comment -- so the monthly mf_holding_snapshot history built from this
module's ledger lands near the appendix's ~250,000-row target.
"""
import bisect
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import config, fake
from .spine import assign_customers
from .txncall import FunctionCallBatcher

DELIBERATE_REJECT_PROB = 0.0004


def load_scheme_catalog(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code, amc_name FROM silver.dim_mf_scheme")
        rows = cur.fetchall()
    by_amc = {}
    for scheme_code, amc_name in rows:
        by_amc.setdefault(amc_name, []).append(scheme_code)
    return by_amc


def load_nav_map(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT scheme_code, nav_date, nav_value FROM core.mf_nav_ref ORDER BY scheme_code, nav_date")
        rows = cur.fetchall()
    nav_map = {}
    for scheme_code, nav_date, nav_value in rows:
        d = nav_map.setdefault(scheme_code, {"dates": [], "values": []})
        d["dates"].append(nav_date)
        d["values"].append(float(nav_value))
    return nav_map


def nearest_nav_on_or_before(nav_map, scheme_code, target_date):
    entry = nav_map.get(scheme_code)
    if not entry or not entry["dates"]:
        return None, None
    dates = entry["dates"]
    idx = bisect.bisect_right(dates, target_date) - 1
    if idx < 0:
        return None, None
    return dates[idx], entry["values"][idx]


def create_mf_folios(conn, rng: random.Random, customer_ids, settlement_accounts_by_customer,
                      scheme_catalog, nav_map, n_folios):
    today = dt.date.today()
    history_start = today - dt.timedelta(days=30 * config.MF_HISTORY_MONTHS)

    usable_amcs = [amc for amc, schemes in scheme_catalog.items()
                   if any(s in nav_map for s in schemes)]

    rows = []
    folios = []
    for cust_id in assign_customers(rng, customer_ids, n_folios):
        amc_name = rng.choice(usable_amcs)
        open_date = history_start + dt.timedelta(days=rng.randint(0, 30 * config.MF_HISTORY_MONTHS))
        status = "active" if rng.random() < 0.95 else "closed"
        settlement_options = settlement_accounts_by_customer.get(cust_id) or [None]
        settlement_account_id = rng.choice(settlement_options)
        folio_id = fake.new_id("FOLIO")
        eligible_schemes = [s for s in scheme_catalog[amc_name] if s in nav_map]
        n_schemes = min(len(eligible_schemes), rng.choice([1, 1, 2, 3]))
        chosen_schemes = rng.sample(eligible_schemes, n_schemes) if n_schemes else []

        # Inserted 'active' regardless of intended final status -- post_mf_transaction
        # rejects any posting against a non-active folio (§1.2), so the historical
        # buy/sell sequence can only be posted while it's still active. The real
        # final status is applied by finalize_folio_status() after that history
        # is done generating (same pattern as spine.finalize_account_status).
        rows.append((folio_id, cust_id, amc_name, fake.new_id("FNUM"), settlement_account_id,
                     open_date, "active"))
        folios.append({"folio_id": folio_id, "customer_id": cust_id, "amc_name": amc_name,
                       "open_date": open_date, "status": status, "schemes": chosen_schemes})

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO core.mf_folio (folio_id, customer_id, amc_name, folio_number,
                settlement_account_id, open_date, status)
            VALUES %s
        """, rows)
    conn.commit()
    return folios


def generate_mf_transactions(conn, rng: random.Random, folios, nav_map, target_total):
    today = dt.date.today()
    pairs = [(f["folio_id"], f["open_date"], s) for f in folios for s in f["schemes"]]
    if not pairs:
        return {"approved": 0, "rejected": 0, "total_calls": 0, "ledger": {}}

    avg_per_pair = max(1.0, target_total / len(pairs))
    batcher = FunctionCallBatcher(conn, config.COMMIT_BATCH_SIZE)
    approved = 0
    rejected = 0
    ledger = {}  # (folio_id, scheme_code) -> list of (txn_date, units_delta, amount_delta)

    sql = "SELECT core.post_mf_transaction(%s,%s,%s,%s,%s,%s,%s,%s)"

    for folio_id, open_date, scheme_code in pairs:
        n_txn = max(1, round(rng.gauss(avg_per_pair, avg_per_pair * 0.4)))
        span = max(1, (today - open_date).days)
        txn_dates = sorted(open_date + dt.timedelta(days=rng.randint(0, span)) for _ in range(n_txn))

        units_held = 0.0
        key = (folio_id, scheme_code)
        ledger[key] = []

        for i, txn_date in enumerate(txn_dates):
            deliberate_test = rng.random() < DELIBERATE_REJECT_PROB
            if deliberate_test and units_held > 0:
                txn_type = "redemption"
                nav_date, nav_value = nearest_nav_on_or_before(nav_map, scheme_code, txn_date)
                if nav_date is None:
                    continue
                amount = round((units_held + 1000) * nav_value, 2)  # guaranteed units_requested > units_held
            else:
                nav_date, nav_value = nearest_nav_on_or_before(nav_map, scheme_code, txn_date)
                if nav_date is None:
                    continue
                roll = rng.random()
                if i == 0 or roll < 0.55:
                    txn_type = "sip" if rng.random() < 0.5 else "purchase"
                elif roll < 0.85 and units_held > 0:
                    txn_type = "redemption"
                elif roll < 0.93 and units_held > 0:
                    txn_type = "switch_out"
                elif roll < 0.97:
                    txn_type = "switch_in"
                else:
                    txn_type = "dividend"

                if txn_type in ("redemption", "switch_out"):
                    max_amount = units_held * nav_value
                    if max_amount < 100:
                        txn_type = "purchase"
                        amount = round(rng.uniform(1000, 20000), 2)
                    else:
                        amount = round(rng.uniform(100, max_amount * 0.7), 2)
                elif txn_type == "dividend":
                    amount = round(units_held * nav_value * rng.uniform(0.005, 0.02), 2) or 100.0
                else:
                    amount = round(rng.uniform(1000, 50000), 2)

            is_sip = txn_type == "sip"
            idem_key = fake.new_id("MFIDEMP")
            result = batcher.call(sql, (folio_id, scheme_code, txn_type, txn_date, nav_date,
                                        amount, is_sip, idem_key))

            if result is not None:
                units = round(amount / nav_value, 5)
                if txn_type in ("redemption", "switch_out"):
                    units_held = max(0.0, units_held - units)
                    ledger[key].append((txn_date, -units, -amount))
                else:
                    units_held += units
                    ledger[key].append((txn_date, units, amount))
                approved += 1
            else:
                rejected += 1

    batcher.flush()
    finalize_folio_status(conn, folios)
    return {"approved": approved, "rejected": rejected, "total_calls": batcher.total_calls, "ledger": ledger}


def finalize_folio_status(conn, folios):
    """Mirrors spine.finalize_account_status: applies each folio's intended
    final status once its buy/sell history is done generating."""
    updates = [(f["folio_id"], f["status"]) for f in folios if f["status"] != "active"]
    if not updates:
        return
    with conn.cursor() as cur:
        execute_values(cur, """
            UPDATE core.mf_folio AS m SET status = v.status
            FROM (VALUES %s) AS v(folio_id, status)
            WHERE m.folio_id = v.folio_id
        """, updates)
    conn.commit()
