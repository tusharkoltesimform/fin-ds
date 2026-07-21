from contextlib import contextmanager

import psycopg2

from . import config


@contextmanager
def get_conn(autocommit: bool = False):
    conn = psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=config.PG_DATABASE,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
    )
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        conn.close()
