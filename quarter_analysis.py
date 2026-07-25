"""四半期業績の見え方を、表示先に依存せず判定する純粋ロジック。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _number(row: Mapping[str, Any] | None, key: str) -> float | None:
    if not row or row.get(key) is None:
        return None
    try:
        return float(row[key])
    except (TypeError, ValueError):
        return None


def _signed(value: float) -> str:
    return f"{value:+.1f}%"


def assess_quarter(
    row: Mapping[str, Any],
    previous_quarter: Mapping[str, Any] | None = None,
    *,
    momentum_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    """最新四半期を前年同期比で評価し、直前四半期からの勢いも判定する。

    加速・減速は、連続する2四半期の「営業利益の前年同期比」の差で判定する。
    赤字や黒字転換をパーセントだけで比較すると誤解を招くため、勢い判定は
    当年・前年の営業利益が両四半期とも黒字の場合に限定する。
    """
    revenue_yoy = _number(row, "yoy_rev")
    op_yoy = _number(row, "yoy_op")
    op_margin_delta = _number(row, "yoy_op_mgn_delta")
    current_op = _number(row, "op")
    prior_year_op = _number(row, "prev_year_op")

    if current_op is not None and prior_year_op is not None:
        if prior_year_op <= 0 < current_op:
            label, tone = "黒字転換", "positive"
        elif prior_year_op >= 0 > current_op:
            label, tone = "赤字転落", "negative"
        elif prior_year_op < 0 and current_op < 0:
            if current_op > prior_year_op:
                label, tone = "赤字縮小", "positive"
            elif current_op < prior_year_op:
                label, tone = "赤字拡大", "negative"
            else:
                label, tone = "赤字横ばい", "neutral"
        elif revenue_yoy is not None and op_yoy is not None:
            if revenue_yoy >= 0 and op_yoy >= 0:
                label, tone = "増収増益", "positive"
            elif revenue_yoy >= 0 and op_yoy < 0:
                label, tone = "増収減益", "mixed"
            elif revenue_yoy < 0 and op_yoy >= 0:
                label, tone = "減収増益", "mixed"
            else:
                label, tone = "減収減益", "negative"
        elif op_yoy is not None:
            label, tone = ("営業増益", "positive") if op_yoy >= 0 else ("営業減益", "negative")
        else:
            label, tone = "判定材料不足", "neutral"
    elif revenue_yoy is not None and op_yoy is not None:
        if revenue_yoy >= 0 and op_yoy >= 0:
            label, tone = "増収増益", "positive"
        elif revenue_yoy >= 0 and op_yoy < 0:
            label, tone = "増収減益", "mixed"
        elif revenue_yoy < 0 and op_yoy >= 0:
            label, tone = "減収増益", "mixed"
        else:
            label, tone = "減収減益", "negative"
    else:
        label, tone = "判定材料不足", "neutral"

    facts: list[str] = []
    if revenue_yoy is not None:
        facts.append(f"売上高 {_signed(revenue_yoy)}")
    if op_yoy is not None:
        facts.append(f"営業利益 {_signed(op_yoy)}")
    if op_margin_delta is not None:
        facts.append(f"営業利益率 {op_margin_delta:+.1f}pt")
    description = "／".join(facts) if facts else "前年同期との比較データが不足しています"

    momentum = None
    momentum_delta = None
    previous_yoy = _number(previous_quarter, "yoy_op")
    previous_op = _number(previous_quarter, "op")
    previous_prior_year_op = _number(previous_quarter, "prev_year_op")
    all_positive = all(
        value is not None and value > 0
        for value in (current_op, prior_year_op, previous_op, previous_prior_year_op)
    )
    if all_positive and op_yoy is not None and previous_yoy is not None:
        momentum_delta = round(op_yoy - previous_yoy, 1)
        if momentum_delta >= momentum_threshold_pct:
            momentum = "加速"
        elif momentum_delta <= -momentum_threshold_pct:
            momentum = "減速"
        else:
            momentum = "横ばい"

    return {
        "label": label,
        "tone": tone,
        "description": description,
        "momentum": momentum,
        "momentum_delta": momentum_delta,
    }
