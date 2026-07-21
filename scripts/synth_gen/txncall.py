"""Thin batching wrapper around calls to the Phase 4 core.post_* functions.

Every call is exactly what the generator submits to the constrained schema
(no direct bulk-INSERT into the 5 money-movement tables or the balance-
bearing parents they touch -- see phase3-synthetic-data-generation.md §3.2).
Commits are batched (every `batch_size` calls) purely for throughput; each
individual call is still one self-contained atomic unit inside the function
itself (Phase 4's own transaction boundary), so batching commits only
changes *when the durability point lands*, never correctness -- a crash
mid-batch loses the whole uncommitted batch, exactly as if those calls were
never attempted.
"""


class FunctionCallBatcher:
    def __init__(self, conn, batch_size):
        self.conn = conn
        self.cur = conn.cursor()
        self.batch_size = batch_size
        self._since_commit = 0
        self.total_calls = 0

    def call(self, sql, params):
        self.cur.execute(sql, params)
        result = self.cur.fetchone()[0]
        self.total_calls += 1
        self._since_commit += 1
        if self._since_commit >= self.batch_size:
            self.conn.commit()
            self._since_commit = 0
        return result

    def flush(self):
        self.conn.commit()
        self._since_commit = 0
