"""模擬運用の保有・未約定注文も、価格と同じ公式台帳の単位へ揃える。

約定原本・現金・実保有(SBI取込)は変更しない。端株・ライツ・適用済み情報の訂正は
推測で評価せず要確認とし、分割の適用は価格公開と同様に冪等にする。
"""
from datetime import date
from decimal import Decimal
import json

from config import bulk_upsert
from price_adjustments import Action, PriceBasisError, actions_digest, number
from price_store import load_actions


def adjusted_units(shares, unit_price, action: Action) -> tuple[int | None, Decimal | None]:
    if action.kind not in {"split", "consolidation"} or action.status != "confirmed":
        raise PriceBasisError("模擬運用の権利処理を確認する必要があります")
    qty = number(shares)
    price = number(unit_price)
    if qty is not None:
        qty *= action.volume_factor
        if qty != qty.to_integral_value():
            raise PriceBasisError("分割・併合で端株が生じるため、模擬運用の権利処理を確認してください")
    return (int(qty) if qty is not None else None,
            (price * action.price_factor).quantize(Decimal("0.00000001")) if price is not None else None)


def ensure_table(cur) -> None:
    # 初回互換移行: 既存のポジション・約定表には列を追加せず適用履歴を分離する。
    cur.execute("""CREATE TABLE IF NOT EXISTS ai_fund_action_applications (
        object_key VARCHAR(100) NOT NULL, ex_date DATE NOT NULL,
        revision CHAR(64) NOT NULL, details JSON NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (object_key,ex_date)
    )""")


def apply_actions(cur, as_of: date) -> int:
    """約定/NAVと同じトランザクションで呼ぶ。現金は変わらず取得総額も維持する。"""
    cur.execute("SELECT id FROM ai_fund_state WHERE id=1 FOR UPDATE")
    cur.fetchone()
    cur.execute("SELECT code,shares,avg_cost,buy_date FROM ai_fund_positions ORDER BY code FOR UPDATE")
    positions = cur.fetchall()
    cur.execute("SELECT id,code,shares,limit_price,decided_date FROM ai_fund_orders WHERE status=%s ORDER BY id FOR UPDATE", ("pending",))
    orders = cur.fetchall()
    cur.execute("SELECT object_key,ex_date,revision FROM ai_fund_action_applications")
    applied = {(key, ex): revision for key, ex, revision in cur.fetchall()}
    actions = load_actions(cur)
    cur.execute("SELECT code,status FROM price_adjustment_state")
    states = dict(cur.fetchall())
    journals = []
    for kind, objects in (("position", positions), ("order", orders)):
        for obj in objects:
            if kind == "position":
                code, qty, price, since = obj
                key = f"position:{code}:{since}"
                identifier = code
            else:
                identifier, code, qty, price, since = obj
                key = f"order:{identifier}"
            changed = False
            for action in actions.get(code, []):
                if since is None and action.ex_date <= as_of:
                    raise PriceBasisError(f"{code}: 取得日不明の模擬保有・注文の権利処理を確認してください")
                if since is None or not since < action.ex_date <= as_of:
                    continue
                revision = actions_digest([action],as_of)
                previous = applied.get((key,action.ex_date))
                if previous:
                    if previous != revision:
                        raise PriceBasisError(f"{code}: 適用済み権利情報が訂正され、模擬保有の確認が必要です")
                    continue
                if action.status == "cancelled":
                    continue
                if states.get(code) not in {"published", "audit_due"}:
                    raise PriceBasisError(f"{code}: 価格の権利補正が未完了のため模擬運用を保留します")
                before = {"shares":qty,"unit_price":price}
                qty, price = adjusted_units(qty,price,action)
                journals.append((key,action.ex_date,revision,json.dumps(
                    {"code":code,"before":before,"after":{"shares":qty,"unit_price":price}},default=str)))
                changed = True
            if changed:
                if kind == "position":
                    cur.execute("UPDATE ai_fund_positions SET shares=%s,avg_cost=%s WHERE code=%s",(qty,price,identifier))
                else:
                    cur.execute("UPDATE ai_fund_orders SET shares=%s,limit_price=%s WHERE id=%s",(qty,price,identifier))
    bulk_upsert(cur,"ai_fund_action_applications",["object_key","ex_date","revision","details"],journals)
    return len(journals)
