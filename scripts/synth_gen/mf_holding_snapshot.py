"""Phase 3 §3.1 step 6 (gold.mf_holding_snapshot) -- the monthly-grain
history, reconstructed from the in-memory transaction ledger that
mf_ownership.generate_mf_transactions built while posting through
core.post_mf_transaction. This is what a real CDC/batch sync would produce
by freezing core.mf_holding_current at each month-end; since this is a
one-shot historical backfill rather than a running monthly job, the whole
history is reconstructed here in one pass instead.

Replicates core.post_mf_transaction's exact invested_amount floor-at-zero
rule (a redemption can reduce invested_amount below the true remaining cost
basis when selling into a gain, so it's clamped, same as the DB does) --
plain running sums would drift from what a month-by-month replay of the
same events against the DB would show.
"""
import datetime as dt
import random

from psycopg2.extras import execute_values

from . import fake
from .mf_ownership import nearest_nav_on_or_before


def _add_month(d: dt.date) -> dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1)
    return d.replace(month=d.month + 1)


def _month_end(d: dt.date) -> dt.date:
    nxt = _add_month(d.replace(day=1))
    return nxt - dt.timedelta(days=1)


def build_and_insert_holding_snapshots(conn, ledger, nav_map):
    today = dt.date.today()
    rows = []

    for (folio_id, scheme_code), events in ledger.items():
        if not events:
            continue
        events = sorted(events, key=lambda e: e[0])

        month_cursor = events[0][0].replace(day=1)
        last_month = min(today, events[-1][0]).replace(day=1)

        units_held = 0.0
        invested_amount = 0.0
        event_idx = 0

        while month_cursor <= last_month:
            month_end = min(_month_end(month_cursor), today)
            while event_idx < len(events) and events[event_idx][0] <= month_end:
                _, units_delta, amount_delta = events[event_idx]
                units_held = max(0.0, units_held + units_delta)
                if amount_delta < 0:
                    invested_amount = max(0.0, invested_amount + amount_delta)
                else:
                    invested_amount += amount_delta
                event_idx += 1

            nav_date, nav_value = nearest_nav_on_or_before(nav_map, scheme_code, month_end)
            if nav_value is not None:
                market_value = round(units_held * nav_value, 2)
                unrealised_gain = round(market_value - invested_amount, 2)
                rows.append((fake.new_id("HOLD"), folio_id, scheme_code, month_end,
                            round(units_held, 5), round(invested_amount, 2), nav_value,
                            market_value, unrealised_gain))

            month_cursor = _add_month(month_cursor)

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO gold.mf_holding_snapshot (holding_id, folio_id, scheme_code, snapshot_date,
                units_held, invested_amount, nav_value, market_value, unrealised_gain)
            VALUES %s
            ON CONFLICT (folio_id, scheme_code, snapshot_date) DO NOTHING
        """, rows)
    conn.commit()
    return {"rows": len(rows)}
