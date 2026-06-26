import os
import pymysql
from dotenv import load_dotenv

load_dotenv()

JQUANTS_API_KEY = os.environ["JQUANTS_API_KEY"]
JQUANTS_BASE_URL = "https://api.jquants.com/v2"
JQUANTS_HEADERS = {"x-api-key": JQUANTS_API_KEY}

_ssl_ca = os.environ.get("TIDB_SSL_CA", "")

DB_CONFIG = dict(
    host=os.environ["TIDB_HOST"],
    port=int(os.environ.get("TIDB_PORT", 4000)),
    database=os.environ.get("TIDB_DATABASE", "stock_tracker"),
    user=os.environ["TIDB_USER"],
    password=os.environ["TIDB_PASSWORD"],
    ssl={"ca": _ssl_ca} if _ssl_ca else {},
    charset="utf8mb4",
    autocommit=False,
)


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def bulk_upsert(cur, table, columns, rows, update_cols=None, batch_size=500):
    """MySQL/TiDB向けバルクUPSERT (INSERT ... ON DUPLICATE KEY UPDATE)"""
    if not rows:
        return
    col_list = ",".join(f"`{c}`" for c in columns)
    row_ph = "(" + ",".join(["%s"] * len(columns)) + ")"
    if update_cols is None:
        update_cols = columns
    updates = ",".join(f"`{c}`=VALUES(`{c}`)" for c in update_cols)
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        values_ph = ",".join([row_ph] * len(batch))
        sql = f"INSERT INTO `{table}` ({col_list}) VALUES {values_ph} ON DUPLICATE KEY UPDATE {updates}"
        cur.execute(sql, [v for row in batch for v in row])
