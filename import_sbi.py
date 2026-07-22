"""SBI証券のCSV明細（保有証券一覧・約定履歴）を取り込んで DB 化する。

使い方（ローカル実行専用・手動）:
    python import_sbi.py SBI明細/20260722/     # フォルダ内のCSVを自動判別
    python import_sbi.py SBI明細               # 親を渡すと最新日付フォルダを自動選択

SBI証券には保有・約定を取り出せる一般向け公式APIが無いため、画面から出力した
CSV（Shift_JIS/cp932）を手動で取り込む方式にしている。SBIのCSVは
  - 冒頭にプリアンブル、口座区分ごとに複数セクション、株と投信で列意味が異なる
  - 手数料/税額が "--"（ゼロ革命）、コードに新JPX英数字（例 286A）が混在
といった癖があるため、素直な read_csv ではなく専用パーサで処理する。

生成テーブル:
  my_holdings … 保有スナップショット（現況の正）。取り込みのたび全置換。
  my_trades   … 約定履歴（時系列）。row_hash で冪等 upsert（期間重複再取込OK）。

※ このCSVは個人の金融情報。.gitignore で SBI明細/ を除外済み。DB以外に残さない。
"""
from __future__ import annotations

import csv
import glob
import hashlib
import io
import os
import re
import sys
from datetime import date, datetime

from config import db, bulk_upsert

# ─── 口座区分の統一語彙（保有・約定で共通） ──────────────────────────────
#   特定 / NISA成長 / NISAつみたて / 旧NISA / 一般 / 投信特定 / 投信つみたて
ACCOUNT_STOCK_SPECIFIC = "特定"


def ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS my_holdings (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            code            VARCHAR(16) NOT NULL,       -- 4桁 / 新JPX英数字 / 投信は FUND:xxxx 疑似コード
            name            VARCHAR(96),
            account_type    VARCHAR(24) NOT NULL,       -- 特定 / NISA成長 / 旧NISA / 投信特定 / 投信つみたて
            asset_class     VARCHAR(8)  NOT NULL,       -- stock / fund
            qty             DECIMAL(18,4),              -- 株数 or 口数
            avg_cost        DECIMAL(16,4),              -- 取得単価（SBI計算済＝正）
            acquired_amount BIGINT,                     -- 取得金額
            sbi_price       DECIMAL(16,4),              -- SBI現在値/基準価額（as_of時点・参考）
            sbi_value       BIGINT,                     -- SBI評価額
            sbi_pl          BIGINT,                     -- SBI評価損益
            as_of           DATE NOT NULL,              -- スナップショット日
            updated_at      DATETIME,
            UNIQUE KEY uq_holding (code, account_type)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS my_trades (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            row_hash        CHAR(64) NOT NULL,          -- CSV原行のSHA256（冪等キー）
            trade_date      DATE NOT NULL,
            code            VARCHAR(16),                -- 投信はNULL
            name            VARCHAR(96),
            side            VARCHAR(8),                 -- buy / sell
            account_type    VARCHAR(24),
            market          VARCHAR(24),
            asset_class     VARCHAR(8),                 -- stock / fund
            qty             DECIMAL(18,4),
            price           DECIMAL(16,4),
            fee             BIGINT DEFAULT 0,
            tax             BIGINT DEFAULT 0,
            settle_amount   BIGINT,
            settle_date     DATE,
            raw_action      VARCHAR(48),                -- 元の取引種別（株式現物買/投信金額買付 等）
            updated_at      DATETIME,
            UNIQUE KEY uq_trade (row_hash)
        )
    """)


# ─── パースの小道具 ──────────────────────────────────────────────────────

def _num(s: str | None) -> float | None:
    """'1,807.5' '+19000 ' '84252口' '--' '' → float / None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("口", "").replace("株", "").replace("%", "").strip()
    if s in ("", "--", "---", "-", "*"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(s: str | None) -> int | None:
    v = _num(s)
    return int(round(v)) if v is not None else None


def _norm_code(c: str | None) -> str | None:
    """SBIの銘柄コード表記揺れを正規化。新JPXコードは4文字(3桁+英字)だが、
    募集(IPO)明細で末尾に数字が付く例（'286A1'）があり、売却明細の'286A'と
    分断されて実現損益が突き合わせできない。4文字の正規形に寄せる。"""
    if not c:
        return c
    c = c.strip()
    if re.fullmatch(r"\d{3}[A-Za-z]\d", c):     # 286A1 → 286A
        return c[:4]
    return c


def _date(s: str | None) -> date | None:
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cells(line: str) -> list[str]:
    """1行をCSVパースしてstripしたセルlist（空行は[]）。"""
    if not line.strip():
        return []
    row = next(csv.reader(io.StringIO(line)), [])
    return [c.strip() for c in row]


def _account_from_title(title: str) -> tuple[str, str]:
    """保有一覧のセクション見出し → (account_type, asset_class)。"""
    if title.startswith("投資信託"):
        if "つみたて" in title:
            return "投信つみたて", "fund"
        if "旧NISA" in title:
            return "投信旧NISA", "fund"
        if "特定" in title:
            return "投信特定", "fund"
        return "投信NISA", "fund"
    # 株式
    if "旧NISA" in title:
        return "旧NISA", "stock"
    if "成長" in title:
        return "NISA成長", "stock"
    if "つみたて" in title:
        return "NISAつみたて", "stock"
    if "特定" in title:
        return "特定", "stock"
    return "一般", "stock"


def _account_from_azukari(azukari: str, asset_class: str) -> str:
    """約定履歴の預り欄 → 統一 account_type。"""
    a = (azukari or "").replace(" ", "")
    if "つ" in a:                       # NISA(つ)
        return "投信つみたて" if asset_class == "fund" else "NISAつみたて"
    if "成" in a:                       # NISA(成)
        return "NISA成長"
    if "旧" in a:                       # 旧NISA
        return "投信旧NISA" if asset_class == "fund" else "旧NISA"
    if "特定" in a:
        return "投信特定" if asset_class == "fund" else "特定"
    if "一般" in a:
        return "一般"
    return a or "不明"


# ─── 保有証券一覧のパース ────────────────────────────────────────────────

def parse_holdings(path: str, as_of: date) -> tuple[list[dict], dict[str, tuple[int, int]]]:
    """保有証券一覧CSV → (保有dictの list, 口座別合計 {account: (評価額, 評価損益)})。

    合計は照合（reconciliation）用。株と投信で列意味が違うので layout を切り替える。
    """
    with open(path, encoding="cp932") as f:
        lines = f.read().splitlines()

    rows: list[dict] = []
    totals: dict[str, tuple[int, int]] = {}
    cur_acct: str | None = None
    cur_asset: str | None = None
    layout: str | None = None          # 'stock' / 'fund'
    expect_total_for: str | None = None

    for raw in lines:
        cells = _cells(raw)
        if not cells:
            continue
        c0 = cells[0]

        # セクション見出し（1セル）
        if len(cells) == 1:
            if c0.endswith("合計"):
                acct, _ = _account_from_title(c0[:-2])
                expect_total_for = acct
            elif c0.startswith("株式") or c0.startswith("投資信託"):
                cur_acct, cur_asset = _account_from_title(c0)
                layout = None
            continue

        # 合計ブロック
        if c0 == "評価額合計":
            continue
        if expect_total_for and _num(c0) is not None and len(cells) == 2:
            totals[expect_total_for] = (_int(cells[0]) or 0, _int(cells[1]) or 0)
            expect_total_for = None
            continue

        # 明細のヘッダ行 → layout 確定
        if c0 == "銘柄コード":
            layout = "stock"
            continue
        if c0 == "ファンド名":
            layout = "fund"
            continue

        # 明細データ
        if cur_acct and layout == "stock" and len(cells) >= 9:
            rows.append({
                "code": _norm_code(c0),
                "name": cells[1],
                "account_type": cur_acct,
                "asset_class": "stock",
                "qty": _num(cells[2]),
                "avg_cost": _num(cells[4]),
                "sbi_price": _num(cells[5]),
                "acquired_amount": _int(cells[6]),
                "sbi_value": _int(cells[7]),
                "sbi_pl": _int(cells[8]),
            })
        elif cur_acct and layout == "fund" and len(cells) >= 8:
            name = cells[0]
            pseudo = "FUND:" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
            rows.append({
                "code": pseudo,
                "name": name,
                "account_type": cur_acct,
                "asset_class": "fund",
                "qty": _num(cells[1]),
                "avg_cost": _num(cells[3]),
                "sbi_price": _num(cells[4]),
                "acquired_amount": _int(cells[5]),
                "sbi_value": _int(cells[6]),
                "sbi_pl": _int(cells[7]),
            })
    return rows, totals


# ─── 約定履歴のパース ────────────────────────────────────────────────────

def parse_trades(path: str) -> list[dict]:
    with open(path, encoding="cp932") as f:
        text = f.read()
    lines = text.splitlines()

    # ヘッダ行を探す
    hi = next((i for i, l in enumerate(lines) if l.startswith("約定日,銘柄,")), None)
    if hi is None:
        return []

    rows: list[dict] = []
    for raw in lines[hi + 1:]:
        cells = _cells(raw)
        if len(cells) < 14:
            continue
        td = _date(cells[0])
        if td is None:
            continue
        code = _norm_code(cells[2].strip()) or None
        asset_class = "stock" if code else "fund"
        action = cells[4]
        side = "buy" if "買" in action else ("sell" if "売" in action else None)
        row_hash = hashlib.sha256(("|".join(cells)).encode("utf-8")).hexdigest()
        rows.append({
            "row_hash": row_hash,
            "trade_date": td,
            "code": code,
            "name": cells[1],
            "side": side,
            "account_type": _account_from_azukari(cells[6], asset_class),
            "market": (cells[3] if cells[3] not in ("--", "") else None),
            "asset_class": asset_class,
            "qty": _num(cells[8]),
            "price": _num(cells[9]),
            "fee": _int(cells[10]) or 0,
            "tax": _int(cells[11]) or 0,
            "settle_amount": _int(cells[13]),
            "settle_date": _date(cells[12]),
            "raw_action": action,
        })
    return rows


# ─── ファイル判別・取込 ─────────────────────────────────────────────────

def _classify(path: str) -> str | None:
    """CSVの中身から種類を判定: 'holdings' / 'trades' / None(信用建玉等)。"""
    try:
        with open(path, encoding="cp932") as f:
            head = f.read(600)
    except Exception:
        return None
    if "保有証券一覧" in head:
        return "holdings"
    if "約定履歴照会" in head:
        return "trades"
    return None


def _resolve_folder(arg: str) -> str:
    """フォルダ引数を解決。親（SBI明細）を渡されたら最新日付サブフォルダを選ぶ。"""
    if os.path.isfile(arg):
        return os.path.dirname(arg) or "."
    subs = sorted(d for d in glob.glob(os.path.join(arg, "*")) if os.path.isdir(d))
    if subs and not glob.glob(os.path.join(arg, "*.csv")):
        return subs[-1]         # 最新日付フォルダ
    return arg


def run_import(folder: str, code_check: bool = True) -> dict:
    """フォルダ内のSBI CSVを取り込む。結果サマリ dict を返す。"""
    folder = _resolve_folder(folder)
    csvs = sorted(glob.glob(os.path.join(folder, "*.csv")))
    if not csvs:
        raise SystemExit(f"CSVが見つかりません: {folder}")

    hold_file = next((p for p in csvs if _classify(p) == "holdings"), None)
    trade_file = next((p for p in csvs if _classify(p) == "trades"), None)

    as_of = _folder_date(folder) or date.today()
    summary: dict = {"folder": folder, "as_of": as_of, "holdings": 0, "trades": 0,
                     "totals": {}, "reconcile": [], "unmatched_codes": []}

    now = datetime.now()
    with db() as cur:
        ensure_tables(cur)

        # ── 保有: 全置換（ただし古いスナップショットでは上書きしない） ──
        cur.execute("SELECT MAX(as_of) FROM my_holdings")
        prev_asof = cur.fetchone()[0]
        if hold_file and prev_asof and as_of < prev_asof:
            summary["holdings_skipped"] = f"取込基準日 {as_of} < 既存 {prev_asof} のため保有はスキップ"
            hold_file = None
        if hold_file:
            holds, totals = parse_holdings(hold_file, as_of)
            cur.execute("DELETE FROM my_holdings")
            cols = ["code", "name", "account_type", "asset_class", "qty", "avg_cost",
                    "acquired_amount", "sbi_price", "sbi_value", "sbi_pl", "as_of", "updated_at"]
            data = [[h["code"], h["name"], h["account_type"], h["asset_class"], h["qty"],
                     h["avg_cost"], h["acquired_amount"], h["sbi_price"], h["sbi_value"],
                     h["sbi_pl"], as_of, now] for h in holds]
            bulk_upsert(cur, "my_holdings", cols, data)
            summary["holdings"] = len(holds)
            summary["totals"] = totals

            # 照合: パース合計 vs SBI表示合計
            by_acct: dict[str, list[int]] = {}
            for h in holds:
                v = by_acct.setdefault(h["account_type"], [0, 0])
                v[0] += h["sbi_value"] or 0
                v[1] += h["sbi_pl"] or 0
            for acct, (tv, tp) in totals.items():
                pv, pp = by_acct.get(acct, [0, 0])
                summary["reconcile"].append(
                    (acct, pv, tv, pv == tv, pp, tp, pp == tp))

            # stocks 未登録コード（投信の疑似コードは除く）
            if code_check:
                stock_codes = [h["code"] for h in holds if h["asset_class"] == "stock"]
                if stock_codes:
                    ph = ",".join(["%s"] * len(stock_codes))
                    cur.execute(f"SELECT code FROM stocks WHERE code IN ({ph})", stock_codes)
                    known = {r[0] for r in cur.fetchall()}
                    summary["unmatched_codes"] = [c for c in stock_codes if c not in known]

        # ── 約定: 冪等 upsert ──
        if trade_file:
            trades = parse_trades(trade_file)
            cols = ["row_hash", "trade_date", "code", "name", "side", "account_type",
                    "market", "asset_class", "qty", "price", "fee", "tax",
                    "settle_amount", "settle_date", "raw_action", "updated_at"]
            data = [[t["row_hash"], t["trade_date"], t["code"], t["name"], t["side"],
                     t["account_type"], t["market"], t["asset_class"], t["qty"], t["price"],
                     t["fee"], t["tax"], t["settle_amount"], t["settle_date"],
                     t["raw_action"], now] for t in trades]
            bulk_upsert(cur, "my_trades", cols, data)
            summary["trades"] = len(trades)

    return summary


def _folder_date(folder: str) -> date | None:
    """フォルダ名 'YYYYMMDD' を日付に。"""
    base = os.path.basename(os.path.normpath(folder))
    try:
        return datetime.strptime(base, "%Y%m%d").date()
    except ValueError:
        return None


def _print_summary(s: dict) -> None:
    print(f"取込元: {s['folder']}  基準日: {s['as_of']}")
    print(f"保有: {s['holdings']}件 / 約定: {s['trades']}件")
    if s.get("holdings_skipped"):
        print(f"⚠ 保有スキップ: {s['holdings_skipped']}")
    if s["reconcile"]:
        print("\n口座別 照合（パース合計 vs SBI表示合計）:")
        tot_v = tot_p = 0
        for acct, pv, tv, ok_v, pp, tp, ok_p in s["reconcile"]:
            mark = "✓" if (ok_v and ok_p) else "✗"
            print(f"  {mark} {acct:<10} 評価額 {pv:>12,} (SBI {tv:>12,})  損益 {pp:>+12,} (SBI {tp:>+12,})")
            tot_v += tv
            tot_p += tp
        print(f"  ── 総資産 {tot_v:,}円 / 含み損益 {tot_p:+,}円")
    if s["unmatched_codes"]:
        print(f"\n⚠ stocks未登録の保有コード: {s['unmatched_codes']}")
    else:
        print("\n全保有コードが stocks に照合できました。")


def main() -> None:
    folder = sys.argv[1] if len(sys.argv) > 1 else "SBI明細"
    s = run_import(folder)
    _print_summary(s)


if __name__ == "__main__":
    main()
