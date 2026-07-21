"""Bronze landing — raw API payloads, byte-for-byte, no transform (§2.2)."""
import json


def land_mf_list(conn, payload) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bronze.mf_api_raw (endpoint, scheme_code, payload) VALUES (%s, %s, %s)",
            ("mf_list", None, json.dumps(payload)),
        )
    conn.commit()


def land_mf_scheme(conn, scheme_code, payload) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bronze.mf_api_raw (endpoint, scheme_code, payload) VALUES (%s, %s, %s)",
            ("mf_scheme", str(scheme_code), json.dumps(payload)),
        )
    conn.commit()
