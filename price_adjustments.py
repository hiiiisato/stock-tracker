"""価格の単位を明示した分割調整。DB・HTTPに依存しない計算部分。

取得時点で分割調整済みの価格と、当時の約定基準の価格を区別する。
イベントの真偽を日次の値動きから推定しない。原本から毎回再生成する。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from typing import Iterable


class PriceBasisError(ValueError):
    """価格基準の根拠不足・矛盾。正常データとして公開しない。"""


class StalePublication(PriceBasisError):
    """並行処理で先に公開された新しい結果を巻き戻さない。"""


def number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceBasisError(f"数値ではありません: {value!r}") from exc
    if not result.is_finite():
        raise PriceBasisError("価格に非有限値が含まれています")
    return result


def day(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class Action:
    code: str
    ex_date: date
    before: Decimal
    after: Decimal
    kind: str = "split"
    status: str = "confirmed"
    source: str = ""
    source_url: str = ""
    record_date: date | None = None
    effective_date: date | None = None
    # ライツは株価係数と株数係数が別。分割の逆数式を流用しない。
    price_ratio: Decimal | None = None

    @property
    def price_factor(self) -> Decimal:
        if self.before <= 0 or self.after <= 0:
            raise PriceBasisError("分割の旧株数・新株数は正数が必要です")
        if self.price_ratio is not None and (not self.price_ratio.is_finite() or self.price_ratio <= 0):
            raise PriceBasisError("価格調整係数は有限の正数が必要です")
        return self.price_ratio if self.price_ratio is not None else self.before / self.after

    @property
    def volume_factor(self) -> Decimal:
        return Decimal(1) if self.kind == "rights" else self.after / self.before


def actions_digest(actions: Iterable[Action], basis_date: date) -> str:
    values = sorted((a.code, a.ex_date.isoformat(), str(a.before), str(a.after), a.kind,
                     str(a.price_ratio), a.status) for a in actions if a.ex_date <= basis_date)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def factors(price_date: date, actions: Iterable[Action], basis_date: date) -> tuple[Decimal, Decimal]:
    price = volume = Decimal(1)
    for action in actions:
        if action.status == "cancelled" or action.ex_date > basis_date or action.ex_date <= price_date:
            continue
        if action.status != "confirmed":
            raise PriceBasisError(f"{action.code} {action.ex_date}: 権利情報が未確定です")
        price *= action.price_factor
        volume *= action.volume_factor
    return price, volume


def matches(actual: Decimal, expected: Decimal, relative=Decimal("0.001"), absolute=Decimal("0.06")) -> bool:
    return abs(actual - expected) <= max(absolute, abs(expected) * relative)


def identify_basis(rows: list[dict], actions: list[Action], basis_date: date,
                   raw_anchors: dict[str, Decimal], *, relative=Decimal("0.001"),
                   absolute=Decimal("0.06"), field: str = "close") -> str:
    """同じ日の公式当時値との比較で取得基準を決める。前日比は使わない。

    累積係数が1の日は判別力がない。複数の参考日で矛盾する応答は拒否する。
    """
    candidates = {"raw", "split_adjusted"}
    informative = False
    needs_basis = False
    for row in rows:
        pf, vf = factors(day(row["date"]), actions, basis_date)
        if field == "volume":
            pf = vf
        if pf != 1:
            needs_basis = True
        anchor = raw_anchors.get(str(row["date"]))
        if anchor is None or row.get(field) is None:
            continue
        close = number(row[field])
        allowed = set()
        if matches(close, anchor, relative, absolute):
            allowed.add("raw")
        if matches(close, anchor * pf, relative, absolute):
            allowed.add("split_adjusted")
        # 係数1の参考日も、不整合な提供値の検出には使う。
        candidates &= allowed
        if pf != 1 and allowed:
            informative = True
        if not candidates:
            raise PriceBasisError("取得価格が公式当時値・確認済み分割係数と一致しません")
    if not needs_basis:
        return "raw"  # 両基準の結果が同じ。未知の分割を認定したことにはならない。
    if not informative or len(candidates) != 1:
        raise PriceBasisError("分割前価格の基準を確認できる公式当時値が不足しています")
    return next(iter(candidates))


def normalize(rows: list[dict], actions: list[Action], basis_date: date, source_basis: str,
              previous_adjusted: Decimal | None = None, volume_basis: str | None = None) -> list[dict]:
    if source_basis not in {"raw", "split_adjusted"}:
        raise PriceBasisError("未確認の価格基準は公開できません")
    volume_basis = volume_basis or source_basis
    out = []
    previous = previous_adjusted
    with localcontext() as ctx:
        ctx.prec = 38
        for row in sorted(rows, key=lambda r: str(r["date"])):
            dt = day(row["date"])
            if dt > basis_date:
                raise PriceBasisError("調整基準日より未来の価格が含まれています")
            pf, vf = factors(dt, actions, basis_date)
            result = {"code": row["code"], "date": str(dt), "factor": pf}
            for field in ("open", "high", "low", "close"):
                value = number(row.get(field))
                raw = value / pf if value is not None and source_basis == "split_adjusted" else value
                result[field] = raw.quantize(Decimal("0.0001")) if raw is not None else None
                result["adj_" + field] = (result[field] * pf).quantize(Decimal("0.0001")) if raw is not None else None
            close = result["close"]
            if close is None or close <= 0:
                raise PriceBasisError(f"{dt}: 正の終値が必要です")
            if result["adj_close"] is None or result["adj_close"] <= 0:
                raise PriceBasisError(f"{dt}: 調整後終値を正の値で保持できません")
            if any(result[f] is not None and result[f] <= 0 for f in ("open", "high", "low")):
                raise PriceBasisError(f"{dt}: OHLCに0以下の値があります")
            if result["high"] is not None and result["high"] + Decimal("0.02") < max(close, result["open"] or close):
                raise PriceBasisError(f"{dt}: 高値が始値・終値を下回ります")
            if result["low"] is not None and result["low"] - Decimal("0.02") > min(close, result["open"] or close):
                raise PriceBasisError(f"{dt}: 安値が始値・終値を上回ります")
            volume = number(row.get("volume"))
            if volume is not None and volume < 0:
                raise PriceBasisError(f"{dt}: 出来高が負です")
            raw_volume = volume / vf if volume is not None and volume_basis == "split_adjusted" else volume
            result["volume"] = int(raw_volume.to_integral_value()) if raw_volume is not None else None
            result["adj_volume"] = (Decimal(result["volume"]) * vf).quantize(Decimal("0.0001")) if raw_volume is not None else None
            result["turnover"] = int(close * result["volume"]) if raw_volume is not None else None
            result["change_pct"] = ((result["adj_close"] / previous - 1) * 100).quantize(Decimal("0.0001")) if previous else None
            previous = result["adj_close"]
            out.append(result)
    return out
