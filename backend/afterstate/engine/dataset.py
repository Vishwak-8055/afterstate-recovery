"""Canonical dataset checksum shared by the validator, the fixture builder and the CLI helper.

Algorithm (documented so operators can compute the expected value against their source database):
  for every row of the query, in query order:  sha256.update( row_to_json(row)::text  +  "\\n" ).
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg import sql


def dataset_checksum(conn: psycopg.Connection[Any], query: str, *, itersize: int = 5000) -> tuple[str, int]:
    wrapped = sql.SQL("SELECT row_to_json(q)::text FROM ({}) AS q").format(sql.SQL(query))
    digest, rows = hashlib.sha256(), 0
    with conn.transaction(), conn.cursor(name="afterstate_dataset") as cur:  # named cursors need a transaction
        cur.itersize = itersize
        cur.execute(wrapped)
        for (text,) in cur:
            digest.update(text.encode("utf-8"))
            digest.update(b"\n")
            rows += 1
    return digest.hexdigest(), rows
