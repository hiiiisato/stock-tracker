"""年次財務指標の唯一の計算実装。

個別画面、最新スクリーニング、PIT履歴、複数年指標は
すべてこのモジュールの定義を使う。値の用途が違うPERや配当利回りは
ここで無理に一つにせず、実績・TTM・予想を別指標として扱う。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


FINANCIAL_FORMULA_VERSION = "annual-ratios-v1"


_ALIASES = {
    "revenue": ("revenue", "rev"),
    "operating_income": ("operating_income", "opi"),
    "ordinary_income": ("ordinary_income", "ord"),
    "net_income": ("net_income", "ni"),
    "total_assets": ("total_assets", "ta"),
    "net_assets": ("net_assets", "total_equity", "teq"),
    "total_debt": ("total_debt", "tdebt"),
    "cf_operating": ("cf_operating", "cfo"),
    "roe_official": ("roe_official",),
}


def as_float(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result


def fact(row: Mapping[str, Any] | None, name: str) -> float | None:
    if not row:
        return None
    for key in _ALIASES[name]:
        if key in row and row[key] is not None:
            return as_float(row[key])
    return None


def percent_ratio(numerator: Any, denominator: Any, digits: int = 4) -> float | None:
    n, d = as_float(numerator), as_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return round(n / d * 100, digits)


def _positive_denominator_ratio(
    numerator: Any, denominator: Any, digits: int = 4,
) -> float | None:
    d = as_float(denominator)
    if d is None or d <= 0:
        return None
    return percent_ratio(numerator, d, digits)


def growth_pct(current: Any, prior: Any, digits: int = 4) -> float | None:
    old = as_float(prior)
    if old is None or old == 0:
        return None
    new = as_float(current)
    if new is None:
        return None
    return round((new / old - 1) * 100, digits)


def forecast_roe_pct(forecast_net_income: Any, latest_equity: Any) -> float | None:
    """予想ROEは実績ROEと別指標。直近実績自己資本を分母にする。"""
    return _positive_denominator_ratio(forecast_net_income, latest_equity, 4)


def _average_balance(current: float | None, prior: float | None) -> tuple[float | None, str]:
    if current is None:
        return None, "missing"
    if prior is None:
        return current, "ending_balance_fallback"
    return (current + prior) / 2, "average_balance"


def _reported_roe_pct(value: Any) -> float | None:
    raw = as_float(value)
    if raw is None:
        return None
    # financials_edinet_annual.roe_official の契約は 0.101=10.1% の比率。
    # 4.5%と「4.5」で保存した値は数値だけで判別できないため、
    # ヒューリスティックで単位を切り替えない。
    pct = raw * 100
    return round(pct, 4) if abs(pct) <= 500 else None


def _input_digest(current: Mapping[str, Any], prior: Mapping[str, Any] | None) -> str:
    fact_names = (
        "revenue", "operating_income", "ordinary_income", "net_income",
        "total_assets", "net_assets", "total_debt", "roe_official",
    )

    def normalized(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        values = {
            name: (str(value) if (value := fact(row, name)) is not None else None)
            for name in fact_names
        }
        values["period_end"] = (
            str(row.get("period_end")) if row.get("period_end") is not None else None
        )
        values["source"] = str(row.get("source")) if row.get("source") is not None else None
        return values

    payload = {
        "version": FINANCIAL_FORMULA_VERSION,
        "current": normalized(current),
        "prior": normalized(prior),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def calculate_annual_metrics(
    current: Mapping[str, Any],
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一つの年次実績と前期実績から、定義が同じ財務指標を作る。"""
    revenue = fact(current, "revenue")
    operating_income = fact(current, "operating_income")
    ordinary_income = fact(current, "ordinary_income")
    net_income = fact(current, "net_income")
    assets = fact(current, "total_assets")
    equity = fact(current, "net_assets")
    debt = fact(current, "total_debt")
    prior_assets = fact(prior, "total_assets")
    prior_equity = fact(prior, "net_assets")

    average_equity, equity_basis = _average_balance(equity, prior_equity)
    average_assets, asset_basis = _average_balance(assets, prior_assets)
    reported_roe = _reported_roe_pct(fact(current, "roe_official"))
    calculated_roe = _positive_denominator_ratio(net_income, average_equity)
    roe_actual = reported_roe if reported_roe is not None else calculated_roe
    roe_source = "reported" if reported_roe is not None else f"calculated_{equity_basis}"

    invested_capital = None
    if equity is not None or debt is not None:
        invested_capital = (equity or 0) + (debt or 0)

    return {
        "roe_actual_pct": roe_actual,
        "roe_reported_pct": reported_roe,
        "roe_calculated_pct": calculated_roe,
        "roe_source": roe_source,
        "roa_actual_pct": _positive_denominator_ratio(net_income, average_assets),
        "roa_source": f"calculated_{asset_basis}",
        "operating_margin_pct": _positive_denominator_ratio(operating_income, revenue),
        "ordinary_margin_pct": _positive_denominator_ratio(ordinary_income, revenue),
        "net_margin_pct": _positive_denominator_ratio(net_income, revenue),
        "equity_ratio_pct": _positive_denominator_ratio(equity, assets),
        "debt_to_equity_pct": _positive_denominator_ratio(debt, equity),
        "roic_pct": _positive_denominator_ratio(operating_income, invested_capital),
        "formula_version": FINANCIAL_FORMULA_VERSION,
        "input_digest": _input_digest(current, prior),
    }


def enrich_annual_series(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """年次実績（古い順）に共通指標と証跡を付与する。"""
    enriched: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        item = dict(raw)
        prior = rows[index - 1] if index > 0 else None
        item.update(calculate_annual_metrics(item, prior))
        enriched.append(item)
    return enriched
