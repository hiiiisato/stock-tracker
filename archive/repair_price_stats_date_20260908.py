"""2026-09-08の手動救済で誤記したprice_stats.updated_atを訂正する一回限りの作業。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_conn


conn = get_conn()
cur = conn.cursor()
cur.execute(
    "UPDATE price_stats SET updated_at=%s WHERE updated_at=%s",
    ("2026-09-07", "2026-09-08"),
)
print("updated", cur.rowcount)
conn.commit()
cur.execute(
    "SELECT COUNT(*),MIN(updated_at),MAX(updated_at),MAX(ABS(ord_margin)) "
    "FROM price_stats"
)
print(cur.fetchone())
cur.close()
conn.close()
