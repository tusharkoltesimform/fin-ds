"""CDC the thin (scheme_code, nav_date, nav_value) mirror into core.mf_nav_ref
(OLTP) — §2.1 step 6. This is the one place data flows Lakehouse -> OLTP:
fact_mf_transaction's composite FK (Phase 1) needs it there to be enforced
declaratively at write time.
"""
import psycopg2.extras


def sync_mf_nav_ref(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO core.mf_nav_ref (scheme_code, nav_date, nav_value)
            VALUES %s
            ON CONFLICT (scheme_code, nav_date) DO UPDATE SET nav_value = EXCLUDED.nav_value
            """,
            [(r["scheme_code"], r["nav_date"], r["nav_value"]) for r in rows],
        )
    conn.commit()
    return len(rows)
