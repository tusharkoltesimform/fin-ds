from contextlib import contextmanager

import psycopg2

from . import config


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=config.PG_DATABASE,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
    )
    try:
        yield conn
    finally:
        conn.close()
