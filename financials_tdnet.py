"""
TDnet 決算短信(XBRLサマリー) から財務実績・会社予想を取得する。

kabutan がデータセンターIP（GitHub Actions / Renderプロキシ）を遮断したため、
公式一次データである TDnet(https://www.release.tdnet.info) の決算短信XBRLに置換する。
kabutan はそもそもTDnet短信の転記側なので、直接取ることで速報性・正確性はむしろ向上する。

書き込み先（kabutan(financials_kabutan)と同一スキーマ・同一単位=円で齟齬なく接続）:
  financials           … 実績。本決算短信→period_type 'A'（通期・full year）、
                          四半期短信→ 'Q'（3ヶ月。短信は累計値のため「累計−前四半期までの累計」で3ヶ月化）
  financials_forecast   … 会社予想。通期予想→ 'A'、中間予想→ 'H'。announced_at付きで
                          earnings_refresh.detect_revisions が既存予想と比較し上方/下方修正を検知する。

TDnet一覧(I_list_{page}_{YYYYMMDD}.html) → 決算短信行の td[4] にあるXBRL ZIP →
XBRLData/Summary/*-ixbrl.htm（インラインXBRL）を解析。要素名は標準の tse-ed-t 名前空間。

単体実行:
  python3 financials_tdnet.py              # 直近4日分を取込
  python3 financials_tdnet.py --days 30    # 直近30日分
"""

import io
import re
import sys
import zipfile
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from config import get_conn, bulk_upsert

TDNET_BASE = "https://www.release.tdnet.info/inbs/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# tse-ed-t の要素名（JGAAP優先・IFRSは別名を後方で試す）
_EL_REVENUE   = ("NetSales", "OperatingRevenues", "Revenue", "NetSalesIFRS", "RevenueIFRS", "SalesIFRS")
_EL_OP        = ("OperatingIncome", "OperatingIncomeIFRS", "OperatingProfitIFRS")
_EL_ORDINARY  = ("OrdinaryIncome",)  # 経常利益はJGAAPのみ（IFRS/米国基準は無し→None）
_EL_NET       = ("ProfitAttributableToOwnersOfParent", "NetIncome",
                 "ProfitAttributableToOwnersOfParentIFRS", "NetIncomeIFRS")
_EL_ASSETS    = ("TotalAssets", "TotalAssetsIFRS")
# total_equity は自己資本(OwnersEquity)。kabutanが自己資本を格納していた実測に合わせる
# （例: 3454 2025-11期 total_equity=26,143M = OwnersEquity。NetAssets 26,252M ではない）
_EL_EQUITY    = ("OwnersEquity", "OwnersEquityIFRS", "NetAssets")
_EL_CFO       = ("CashFlowsFromOperatingActivities", "CashFlowsFromOperatingActivitiesIFRS")


def _num(fact) -> float | None:
    """ix:nonFraction のスケール・符号を適用して整数（円）を返す。空欄は None。"""
    if fact is None:
        return None
    txt = fact.get_text(strip=True).replace(",", "").replace("△", "-").replace("▲", "-")
    if txt in ("", "-", "―", "－"):
        return None
    try:
        val = float(txt)
    except ValueError:
        return None
    scale = fact.get("scale")
    if scale not in (None, ""):
        val *= 10 ** int(scale)
    if fact.get("sign") == "-":
        val = -val
    return val


class _Summary:
    """1つの決算短信サマリーiXBRLを解析した結果。"""

    def __init__(self, htm: str):
        self.soup = BeautifulSoup(htm, "html.parser")
        # facts: (element_localname, contextRef) -> fact tag
        self.facts: dict = {}
        for f in self.soup.find_all(["ix:nonfraction", "ix:nonnumeric"]):
            name = f.get("name", "")
            local = name.split(":")[-1]
            ctx = f.get("contextref", "")
            self.facts[(local, ctx)] = f
        # contexts: id -> (instant, start, end)
        self.ctx: dict = {}
        for c in self.soup.find_all(re.compile(r"(^|:)context$")):
            cid = c.get("id", "")
            if not cid:
                continue
            inst = c.find(re.compile("instant")); sd = c.find(re.compile("startdate")); ed = c.find(re.compile("enddate"))
            self.ctx[cid] = (inst.text.strip() if inst else None,
                             sd.text.strip() if sd else None,
                             ed.text.strip() if ed else None)

    def meta(self, name: str) -> str | None:
        for (local, _ctx), f in self.facts.items():
            if local == name:
                return f.get_text(strip=True)
        return None

    def _pick_ctx(self, prefix: str, standalone: str) -> str | None:
        """損益・BSの実績コンテキスト {prefix}_ConsolidatedMember_ResultMember を厳密に返す
        （無ければ非連結→standalone）。AnnualMember/YearEndMember/QuarterMember 等が
        挟まる配当用contextを誤って拾わないよう、期間メンバー無しの本体contextに限定する。"""
        for member in ("ConsolidatedMember", "NonConsolidatedMember"):
            cid = f"{prefix}_{member}_ResultMember"
            if cid in self.ctx:
                return cid
        return standalone if standalone in self.ctx else None

    def val(self, elements: tuple, ctx_id: str | None) -> float | None:
        if not ctx_id:
            return None
        for el in elements:
            f = self.facts.get((el, ctx_id))
            if f is not None:
                v = _num(f)
                if v is not None:
                    return v
        return None


def _accounting_standard(doc_name: str) -> str:
    if "ＩＦＲＳ" in doc_name or "IFRS" in doc_name:
        return "IFRS"
    if "米国" in doc_name or "ＵＳ" in doc_name:
        return "US"
    return "JP"


def _parse_zip(zip_bytes: bytes) -> _Summary | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    summ = [n for n in z.namelist() if "/Summary/" in n and n.endswith("-ixbrl.htm")]
    if not summ:
        return None
    return _Summary(z.read(summ[0]).decode("utf-8", errors="replace"))


def _build_records(s: _Summary, disclosed_dt: datetime, cur) -> tuple[list, list]:
    """(financials行, financials_forecast行) を返す。"""
    code5 = (s.meta("SecuritiesCode") or "").strip()
    code = code5[:4] if len(code5) >= 4 else None
    if not code or not code.isdigit():
        return [], []
    doc = s.meta("DocumentName") or ""
    if "決算短信" not in doc:
        return [], []
    std = _accounting_standard(doc)
    fye = s.meta("FiscalYearEnd")            # 例 '2026-11-30'
    qp = (s.meta("QuarterlyPeriod") or "").strip()   # '1'/'2'/'3' or 空(本決算)

    fins: list = []
    fcs: list = []

    # ── 実績 ──
    if qp in ("1", "2", "3"):
        # 四半期短信: 累計(AccumulatedQ{n}Duration) → 3ヶ月化
        dur_ctx = s._pick_ctx(f"CurrentAccumulatedQ{qp}Duration", f"CurrentAccumulatedQ{qp}Duration")
        inst_ctx = s._pick_ctx(f"CurrentAccumulatedQ{qp}Instant", f"CurrentAccumulatedQ{qp}Instant")
        period_end = s.ctx.get(dur_ctx, (None, None, None))[2] if dur_ctx else None
        if period_end:
            cum_rev = s.val(_EL_REVENUE, dur_ctx)
            cum_oi  = s.val(_EL_OP, dur_ctx)
            cum_ord = s.val(_EL_ORDINARY, dur_ctx)
            cum_ni  = s.val(_EL_NET, dur_ctx)
            # 3ヶ月化: 累計 − 同一決算期の前四半期までの3ヶ月実績の合計（DBから）
            rev, oi, ordi, ni = _cumulative_to_3m(cur, code, fye, period_end,
                                                  cum_rev, cum_oi, cum_ord, cum_ni)
            ta  = s.val(_EL_ASSETS, inst_ctx)
            teq = s.val(_EL_EQUITY, inst_ctx)
            if rev is not None or ni is not None:
                fins.append((code, period_end, "Q", rev, oi, ordi, ni, ta, teq, None, None))
    else:
        # 本決算短信（通期実績）
        dur_ctx = s._pick_ctx("CurrentYearDuration", "CurrentYearDuration")
        inst_ctx = s._pick_ctx("CurrentYearInstant", "CurrentYearInstant")
        period_end = (s.ctx.get(dur_ctx, (None, None, None))[2] if dur_ctx else None) or fye
        if period_end:
            rev = s.val(_EL_REVENUE, dur_ctx)
            oi  = s.val(_EL_OP, dur_ctx)
            ordi = s.val(_EL_ORDINARY, dur_ctx)
            ni  = s.val(_EL_NET, dur_ctx)
            ta  = s.val(_EL_ASSETS, inst_ctx)
            teq = s.val(_EL_EQUITY, inst_ctx)
            cfo_ctx = s._pick_ctx("CurrentYearDuration", "CurrentYearDuration")
            cfo = s.val(_EL_CFO, cfo_ctx)
            if rev is not None or ni is not None:
                fins.append((code, period_end, "A", rev, oi, ordi, ni, ta, teq, None, cfo))

    # ── 会社予想（通期='A' / 中間='H'）──
    # 四半期短信: 予想は当期(CurrentYearDuration)。本決算短信: 予想は翌期(NextYearDuration)。
    ann = disclosed_dt.date()
    is_quarterly = qp in ("1", "2", "3")
    yr_prefix = "CurrentYearDuration" if is_quarterly else "NextYearDuration"
    h_prefix = "CurrentAccumulatedQ2Duration" if is_quarterly else "NextAccumulatedQ2Duration"
    # 予想対象の決算期末: 四半期は当期FYE、本決算は翌期（コンテキストのend日付）
    fc_ctx = _forecast_ctx(s, yr_prefix)
    fc_fye = fye if is_quarterly else (s.ctx.get(fc_ctx, (None, None, None))[2] if fc_ctx else None)
    if fc_ctx and fc_fye:
        rev = s.val(_EL_REVENUE, fc_ctx); oi = s.val(_EL_OP, fc_ctx)
        ordi = s.val(_EL_ORDINARY, fc_ctx); ni = s.val(_EL_NET, fc_ctx)
        dps = _forecast_annual_dps(s, is_quarterly)
        if any(v is not None for v in (rev, oi, ordi, ni, dps)):
            fcs.append((code, fc_fye, "A", rev, oi, ordi, ni, dps, ann))
        # kabutan踏襲: 通期予想を financials にも 'A'(未来period_end=fc_fye) で持つ。
        # _progress_note が「通期予想の営業益」をここから読むため（実績'A'は period_end<=今日 で区別）。
        if any(v is not None for v in (rev, oi, ordi, ni)):
            fins.append((code, fc_fye, "A", rev, oi, ordi, ni, None, None, None, None))
    # 中間期予想（あれば）
    h_ctx = _forecast_ctx(s, h_prefix)
    if h_ctx:
        hpe = s.ctx.get(h_ctx, (None, None, None))[2]
        rev = s.val(_EL_REVENUE, h_ctx); oi = s.val(_EL_OP, h_ctx)
        ordi = s.val(_EL_ORDINARY, h_ctx); ni = s.val(_EL_NET, h_ctx)
        if hpe and any(v is not None for v in (rev, oi, ordi, ni)):
            fcs.append((code, hpe, "H", rev, oi, ordi, ni, None, ann))

    return fins, fcs


def _forecast_ctx(s: _Summary, prefix: str) -> str | None:
    """{prefix}_ConsolidatedMember_ForecastMember（無ければ非連結）を返す。
    期間メンバー(AnnualMember/YearEndMember/QuarterMember等)が挟まる配当用contextは除外し、
    損益予想の本体contextのみを厳密に選ぶ。"""
    for member in ("ConsolidatedMember", "NonConsolidatedMember"):
        cid = f"{prefix}_{member}_ForecastMember"
        if cid in s.ctx:
            return cid
    return None


def _forecast_annual_dps(s: _Summary, is_quarterly: bool) -> float | None:
    """通期の1株配当予想（AnnualMember × ForecastMember）。
    四半期短信は当期(CurrentYear)、本決算短信は翌期(NextYear)の予想を見る。"""
    want = "CurrentYearDuration" if is_quarterly else "NextYearDuration"
    for cid in s.ctx:
        if cid.startswith(want) and "AnnualMember" in cid and "ForecastMember" in cid:
            v = s.val(("DividendPerShare",), cid)
            if v is not None:
                return v
    return None


def _cumulative_to_3m(cur, code, fye, period_end, cum_rev, cum_oi, cum_ord, cum_ni):
    """四半期累計 → 単独3ヶ月。同一決算期の前四半期までの3ヶ月実績(DB)の合計を引く。
    Q1（前四半期実績が無い＝累計と一致）はそのまま。前四半期がDBに無く引けない場合は
    齟齬を避けるため None を返す（その四半期実績は書かない）。"""
    if not fye:
        return None, None, None, None
    # 決算期の開始日 = 期末日の約1年前の翌日。period_end がその期の何番目Qかを問わず、
    # 「同一決算期・当該四半期より前」の 'Q' 実績を合計する。
    fy_end = fye
    try:
        fy_start = (datetime.strptime(fy_end, "%Y-%m-%d").date().replace(year=int(fy_end[:4]) - 1)
                    + timedelta(days=1)).isoformat()
    except ValueError:
        return None, None, None, None
    cur.execute("""
        SELECT COALESCE(SUM(revenue),0), COALESCE(SUM(operating_income),0),
               COALESCE(SUM(ordinary_income),0), COALESCE(SUM(net_income),0),
               COUNT(*)
        FROM financials
        WHERE code=%s AND period_type='Q' AND period_end >= %s AND period_end < %s
    """, (code, fy_start, period_end))
    srev, soi, sord, sni, n = cur.fetchone()

    def sub(cum, prior):
        return None if cum is None else (cum - float(prior))

    # 前四半期がDBに1件も無い場合:
    #   period_end がその決算期の最初のQ（fy_start から3〜4ヶ月以内）なら累計=3ヶ月なのでそのまま採用。
    #   そうでない（Q2/Q3なのに前Qが欠損）なら引けないので None（齟齬防止）。
    if n == 0:
        months = _months_between(fy_start, period_end)
        if months is not None and months <= 3:
            return cum_rev, cum_oi, cum_ord, cum_ni
        return None, None, None, None
    return sub(cum_rev, srev), sub(cum_oi, soi), sub(cum_ord, sord), sub(cum_ni, sni)


def _months_between(start_iso, end_iso):
    try:
        s = datetime.strptime(start_iso, "%Y-%m-%d").date()
        e = datetime.strptime(end_iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (e.year - s.year) * 12 + (e.month - s.month) + 1


# ─── TDnet 一覧の取得 ─────────────────────────────────────────────────────────

def _tdnet_earnings(d: date) -> list[dict]:
    """指定日のTDnet一覧から決算短信の行を返す: {code, disclosed, xbrl_url, title}。"""
    ds = d.strftime("%Y%m%d")
    out = []
    page = 1
    while page <= 30:
        url = f"{TDNET_BASE}I_list_{page:03d}_{ds}.html"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
        except Exception:
            break
        if r.status_code != 200 or not r.content:
            break
        soup = BeautifulSoup(r.content, "html.parser")
        found = 0
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            title = tds[3].get_text(strip=True)
            if "決算短信" not in title:
                continue
            xa = tds[4].find("a")
            xhref = xa.get("href", "") if xa else ""
            if not xhref.endswith(".zip"):
                continue
            code5 = tds[1].get_text(strip=True)
            code = code5[:4] if len(code5) >= 4 and code5[:4].isdigit() else None
            t_str = tds[0].get_text(strip=True)   # "17:00"
            try:
                hh, mm = map(int, t_str.split(":"))
                disclosed = datetime(d.year, d.month, d.day, hh, mm)
            except Exception:
                disclosed = datetime(d.year, d.month, d.day, 15, 0)
            out.append({"code": code, "disclosed": disclosed, "title": title,
                        "xbrl_url": TDNET_BASE + xhref if not xhref.startswith("http") else xhref})
            found += 1
        if found == 0 and page > 1:
            break
        page += 1
    return out


# ─── 取込 ─────────────────────────────────────────────────────────────────────

_FIN_COLS = ["code", "period_end", "period_type", "revenue", "operating_income",
             "ordinary_income", "net_income", "total_assets", "total_equity",
             "total_debt", "cf_operating"]
_FC_COLS = ["code", "fiscal_year_end", "period_type", "revenue", "operating_income",
            "ordinary_income", "net_income", "div_per_share", "announced_at"]


def import_recent(days: int = 4, only_codes: set | None = None, force: bool = False) -> dict:
    """直近days日のTDnet決算短信を取り込み、financials / financials_forecast を更新する。
    only_codes を渡すとその銘柄のみ（earnings_refreshの自己修復用）。冪等。
    force=True で「取込済みスキップ」を無効化（過去データの再取得・修復用）。"""
    conn = get_conn(); cur = conn.cursor()
    fin_rows, fc_rows = [], []
    n_disc = 0
    seen_zip = set()
    for i in range(days):
        d = date.today() - timedelta(days=i)
        for item in _tdnet_earnings(d):
            if item["xbrl_url"] in seen_zip:
                continue
            seen_zip.add(item["xbrl_url"])
            code = item.get("code")
            if only_codes is not None and code not in only_codes:
                continue
            # 取込済み（同一銘柄・同一発表日の予想が既にDBにある）ならXBRLダウンロードを省く（冪等・高速化）
            if code and only_codes is None and not force:
                cur.execute("""SELECT 1 FROM financials_forecast
                               WHERE code=%s AND announced_at=%s LIMIT 1""",
                            (code, item["disclosed"].date()))
                if cur.fetchone():
                    continue
            try:
                r = requests.get(item["xbrl_url"], headers=_HEADERS, timeout=20)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            s = _parse_zip(r.content)
            if s is None:
                continue
            code5 = (s.meta("SecuritiesCode") or "").strip()
            if only_codes is not None and code5[:4] not in only_codes:
                continue
            fins, fcs = _build_records(s, item["disclosed"], cur)
            fin_rows.extend(fins)
            fc_rows.extend(fcs)
            n_disc += 1

    if fin_rows:
        bulk_upsert(cur, "financials", _FIN_COLS, fin_rows,
                    update_cols=[c for c in _FIN_COLS if c not in ("code", "period_end", "period_type")])
    if fc_rows:
        bulk_upsert(cur, "financials_forecast", _FC_COLS, fc_rows,
                    update_cols=[c for c in _FC_COLS if c not in ("code", "fiscal_year_end", "period_type", "announced_at")])
    conn.commit(); cur.close(); conn.close()
    print(f"  [TDnet財務] 決算短信 {n_disc}件 → 実績{len(fin_rows)}行 / 予想{len(fc_rows)}行 を更新")
    return {"disclosures": n_disc, "financials": len(fin_rows), "forecasts": len(fc_rows)}


if __name__ == "__main__":
    days = 4
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    import_recent(days=days)
