"""Phase 3 §3.1 step 7 (customer_wealth_snapshot) -- needs deposits + MF
holdings, so it runs last. Lakehouse-native derived batch table, bulk-loaded
directly.

deposit_balance as of each month-end is reconstructed from the in-memory
fact_deposit_txn ledger built in deposits.py (there's no persisted monthly
deposit-balance history table in the 17-table model -- only OLTP "current"
balance -- so replaying the ledger is the only way to get a point-in-time
figure). mf_aum is read back from gold.mf_holding_snapshot, which was just
built and inserted by mf_holding_snapshot.py -- querying the authoritative
table it just wrote is simpler and exactly matches the cross-table sum
Phase 1 requires (mf_aum = SUM(mf_holding_snapshot.market_value) for that
customer/month), rather than re-deriving it independently in Python.
"""
import bisect
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import config, fake
from .mf_holding_snapshot import _add_month, _month_end


def _segment(total_relationship_value: float) -> str:
    if total_relationship_value >= 10_000_000:
        return "ultra_hni"
    if total_relationship_value >= 1_000_000:
        return "hni"
    if total_relationship_value >= 100_000:
        return "affluent"
    return "mass"


def _recent_month_ends(n_months):
    today = dt.date.today()
    month_ends = []
    cursor = today.replace(day=1)
    for _ in range(n_months):
        month_ends.append(min(_month_end(cursor), today))
        cursor = cursor - dt.timedelta(days=1)
        cursor = cursor.replace(day=1)
    return sorted(month_ends)


def _balance_at(ledger_entries, as_of: dt.date):
    if not ledger_entries:
        return 0.0
    dates = [e[0] for e in ledger_entries]
    idx = bisect.bisect_right(dates, as_of) - 1
    if idx < 0:
        return 0.0
    return ledger_entries[idx][1]


def _load_mf_aum(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.customer_id, h.snapshot_date, SUM(h.market_value)
            FROM gold.mf_holding_snapshot h JOIN core.mf_folio f ON f.folio_id = h.folio_id
            GROUP BY f.customer_id, h.snapshot_date
        """)
        rows = cur.fetchall()
    aum = {}
    for customer_id, snapshot_date, total in rows:
        aum[(customer_id, snapshot_date)] = float(total)
    return aum


def generate_wealth_snapshots(conn, rng: random.Random, customer_ids, deposit_accounts_by_customer,
                               deposit_ledger):
    month_ends = _recent_month_ends(config.WEALTH_SNAPSHOT_MONTHS)
    mf_aum_by_customer_month = _load_mf_aum(conn)

    rows = []
    for cust_id in customer_ids:
        acct_ids = deposit_accounts_by_customer.get(cust_id, [])
        for month_end in month_ends:
            deposit_balance = round(sum(
                _balance_at(deposit_ledger.get(acc_id, []), month_end) for acc_id in acct_ids), 2)
            mf_aum = round(mf_aum_by_customer_month.get((cust_id, month_end), 0.0), 2)
            # total must equal the *stored* (already-rounded) parts exactly, so the
            # chk_wealth_total tolerance check can never be tripped by double-rounding.
            total = round(deposit_balance + mf_aum, 2)
            rows.append((fake.new_id("WLTH"), cust_id, month_end, deposit_balance,
                        mf_aum, total, _segment(total)))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO gold.customer_wealth_snapshot (wealth_id, customer_id, snapshot_date,
                deposit_balance, mf_aum, total_relationship_value, wealth_segment)
            VALUES %s
            ON CONFLICT (customer_id, snapshot_date) DO NOTHING
        """, rows)
    conn.commit()
    return {"rows": len(rows)}
