"""JPXの予定表・月報、TDnet本文による分割台帳。価格変動からは認定しない。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import re
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from batch_dates import jst_today
from config import (db, PRICE_ACTION_DISCLOSURE_LIMIT, PRICE_ACTION_HTTP_TIMEOUT,
                    PRICE_HISTORY_START, price_db_session)
from price_adjustments import Action, PriceBasisError
from price_store import ensure_schema, load_actions, put_action, snapshot

JPX_BASE = "https://www.jpx.co.jp"
SCHEDULE_URL = JPX_BASE + "/listing/others/ex-rights/index.html"
MONTHLY_URL = JPX_BASE + "/markets/statistics-equities/monthly/index.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-tracker/1.0)"}
_ROW = re.compile(r"\b(?P<code>[0-9][0-9A-Z]{3})\s+.+?\s+"
                  r"(?P<ex>\d{4}\.\d{2}\.\d{2})\s+(?P<record>\d{4}\.\d{2}\.\d{2})\s+"
                  r"(?P<old>\d+(?:\.\d+)?)\s*:\s*(?P<new>\d+(?:\.\d+)?)\s*"
                  r"(?P<kind>株式分割|株式併合|投資口分割|投資口併合|受益権分割|受益権併合|分割)")


def get_bytes(url: str) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=PRICE_ACTION_HTTP_TIMEOUT)
    response.raise_for_status()
    if not response.content:
        raise RuntimeError(f"公式情報が空です: {url}")
    return response.content


def parse_monthly_pdf(content: bytes, url: str) -> list[Action]:
    import pdfplumber
    actions = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = unicodedata.normalize("NFKC", page.extract_text() or "")
            for line in text.splitlines():
                match = _ROW.search(line)
                if match:
                    m = match.groupdict()
                    old, new = Decimal(m["old"]), Decimal(m["new"])
                    if old <= 0 or new <= 0:
                        raise PriceBasisError("公式月報の比率が正数ではありません")
                    actions.append(Action(m["code"], date.fromisoformat(m["ex"].replace(".", "-")),
                                          old, new, "consolidation" if "併合" in m["kind"] else "split",
                                          source="jpx_monthly", source_url=url,
                                          record_date=date.fromisoformat(m["record"].replace(".", "-"))))
                elif re.search(r"\d\s*:\s*\d.*(?:分割|併合)", line):
                    # 表の形式変更で一部の行だけを黙って欠落させない。
                    raise PriceBasisError(f"公式月報の行を解読できません: {line[:180]}")
    return actions


def parse_schedule_xls(content: bytes, url: str) -> list[Action]:
    import xlrd
    book = xlrd.open_workbook(file_contents=content)
    actions = []
    found_header = False
    for sheet in book.sheets():
        if sheet.nrows < 4:
            continue
        headers = [str(c).replace("\n", "").replace(" ", "") for c in sheet.row_values(3)]
        if not all(key in headers for key in ("銘柄コード", "備考", "権利落日（普通取引）")):
            continue
        found_header = True
        ci, ni, ei, ri = (headers.index(k) for k in ("銘柄コード", "備考", "権利落日（普通取引）", "基準日"))
        for row_index in range(4, sheet.nrows):
            row = sheet.row_values(row_index)
            note = str(row[ni])
            if not any(k in note for k in ("分割", "併合")):
                continue
            code = str(int(row[ci])) if isinstance(row[ci], (int, float)) else str(row[ci]).strip()
            if len(code) == 5 and code.endswith("0"):
                code = code[:-1]
            if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code):
                raise PriceBasisError(f"予定表の銘柄コードを解読できません: {code}")
            ex = xlrd.xldate_as_datetime(row[ei], book.datemode).date()
            record = xlrd.xldate_as_datetime(row[ri], book.datemode).date()
            actions.append(Action(code, ex, Decimal(1), Decimal(1),
                                  "consolidation" if "併合" in note else "split", "needs_ratio",
                                  "jpx_schedule", url, record))
    if not found_header:
        raise PriceBasisError("JPX予定表の列構成が変更されています")
    return actions


def parse_disclosure_ratio(text: str, action: Action, title: str = "") -> Action:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    # 対象の基準日が本文にあることを確認し、同社の過去の別分割を誤採用しない。
    record = action.record_date
    if record and f"{record.year}年{record.month}月{record.day}日" not in re.sub(r"(?<=\D)0(?=\d)", "", text):
        raise PriceBasisError("開示本文に予定表と一致する基準日がありません")
    if any(word in title for word in ("中止", "取りやめ", "撤回")):
        return replace(action, status="cancelled", source="tdnet")
    # 複数ETFを同時に公表する表は、対象コードの行だけを読む。
    table = re.search(r"\(" + re.escape(action.code) + r"\)(\d+(?:\.\d+)?)口を(\d+(?:\.\d+)?)口に分割", text)
    if table:
        return replace(action, before=Decimal(table[1]), after=Decimal(table[2]), status="confirmed", source="tdnet")
    if action.kind == "consolidation":
        section = re.search(r"併合の(?:割合|方法)(.{0,600})", text)
        if section:
            text = section[1]
    patterns = [
        r"(?:普通株式|当社株式|投資口)?(\d+(?:\.\d+)?)(?:株|口)(?:につき|に付き|に対し|を)[、,]?(\d+(?:\.\d+)?)(?:株|口)(?:の割合|に分割|に併合)",
        r"(\d+(?:\.\d+)?)対(\d+(?:\.\d+)?)の(?:割合|株式分割|株式併合)",
    ]
    ratios = set()
    for pattern in patterns:
        ratios.update((Decimal(a), Decimal(b)) for a, b in re.findall(pattern, text))
    if len(ratios) != 1:
        raise PriceBasisError("開示本文の比率が一意に確定できません")
    old, new = next(iter(ratios))
    if old <= 0 or new <= 0 or old == new:
        raise PriceBasisError("開示本文の分割比率が不正です")
    return replace(action, before=old, after=new, status="confirmed", source="tdnet")


def _remember_document(url: str, content: bytes, count: int, *, processed: bool = True) -> None:
    with db() as cur:
        snapshot(cur, "", "official_document", "document", [], url=url,
                 sha256=hashlib.sha256(content).hexdigest(), content_hex=content.hex())
        if not processed:
            return
        cur.execute("""INSERT INTO price_sync_state (name,value,error) VALUES (%s,%s,NULL)
                       ON DUPLICATE KEY UPDATE value=VALUES(value),error=NULL,checked_at=CURRENT_TIMESTAMP""",
                    (url[:160], str(count)))


def schedule_only_correction(text: str, action: Action) -> date | None:
    """訂正前後で日付・曜日だけが違う訂正は、比率の原開示と組み合わせる。"""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    sections = re.search(r"\(訂正前\)(.*?)\(訂正後\)(.*)", compact)
    if not sections or "分割の日程" not in compact:
        return None
    def shape(value):
        value = value.split("(注)")[0]
        return re.sub(r"\d{4}年\d{1,2}月\d{1,2}日(?:\([月火水木金土日]曜日\))?", "<date>", value)
    if shape(sections[1]) != shape(sections[2]):
        return None
    record = action.record_date
    if record and f"基準日{record.year}年{record.month}月{record.day}日" not in sections[2]:
        return None
    match = re.search(r"効力発生日(\d{4})年(\d{1,2})月(\d{1,2})日", sections[2])
    return date(*map(int, match.groups())) if match else None


def resolve_mature_conflicts() -> dict:
    from price_verification import free_cutoff, fetch_bars, remember_bars
    changed, errors = 0, []
    candidates = [a for aa in load_actions().values() for a in aa if a.status == "conflict"]
    for action in candidates[:PRICE_ACTION_DISCLOSURE_LIMIT]:
        if action.ex_date > free_cutoff():
            errors.append(f"{action.code}: 公式出典の比率が競合しています")
            continue
        try:
            bars = fetch_bars(code=action.code, since=action.ex_date, until=action.ex_date)
            remember_bars(bars, update_actions=False)
            ratios = [Decimal(str(r["AdjFactor"])) for r in bars if r.get("AdjFactor") is not None]
            if len(ratios) != 1 or abs(ratios[0] - action.price_factor) > Decimal("0.000002"):
                raise PriceBasisError("公式当日の係数でも比率が一致しません")
            changed += int(put_action(replace(action,status="confirmed"), correction=True))
        except Exception as exc:
            errors.append(f"{action.code}: {exc}")
    return {"changed": changed, "errors": errors}


@price_db_session()
def sync_official_actions(*, history: bool = False) -> dict:
    """毎日予定表、未取得月報、未確定比率を処理。例外は成功0件にしない。"""
    ensure_schema()
    total = 0
    errors = []
    with db() as cur:
        cur.execute("SELECT name FROM price_sync_state WHERE error IS NULL")
        processed = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT code FROM stocks")
        valid_codes = {r[0] for r in cur.fetchall()}
    schedule_html = get_bytes(SCHEDULE_URL)
    soup = BeautifulSoup(schedule_html, "html.parser")
    links = [urljoin(SCHEDULE_URL, a["href"]) for a in soup.select("a[href]") if re.search(r"/\d{8}\.xls$", a["href"])]
    if not links:
        raise RuntimeError("JPX予定表の配信リンクが見つかりません")
    # 4営業日分を毎回見る。訂正・再掲載は内容ハッシュで区別する。
    for url in links:
        content = get_bytes(url)
        _remember_document(url, content, 0, processed=False)
        for action in parse_schedule_xls(content, url):
            if action.code in valid_codes:
                total += int(put_action(action))
        _remember_document(url, content, 0)
    today = jst_today()
    pages = [MONTHLY_URL]
    if history:
        for year in range(today.year - 1, date.fromisoformat(PRICE_HISTORY_START).year - 1, -1):
            pages.append(urljoin(MONTHLY_URL, f"00-archives-{today.year-year:02d}.html"))
    for page_url in pages:
        try:
            page = BeautifulSoup(get_bytes(page_url), "html.parser")
            pdf_urls = [urljoin(page_url, a["href"]) for a in page.select("a[href]") if re.search(r"17_kenri\d{4}\.pdf$", a["href"])]
            if not pdf_urls:
                raise RuntimeError(f"月報の配信リンクがありません: {page_url}")
            for url in pdf_urls:
                if url[:160] in processed:
                    continue
                content = get_bytes(url)
                _remember_document(url, content, 0, processed=False)
                parsed = parse_monthly_pdf(content, url)
                print(f"  公式月報 {url.rsplit('/',1)[-1]}: {len(parsed)}件", flush=True)
                for action in parsed:
                    if action.code in valid_codes:
                        total += int(put_action(action))
                _remember_document(url, content, len(parsed))
        except Exception as exc:
            errors.append(f"月報: {exc}")
    # J-Quantsを正しく取り込んでいた既存イベントは引継ぐ。Yahoo/derived推測は引継がない。
    with db() as cur:
        cur.execute("SELECT code,ex_date,split_ratio FROM stock_splits WHERE source=%s", ("jquants",))
        legacy = cur.fetchall()
    current = load_actions()
    existing = {(a.code, a.ex_date) for aa in current.values() for a in aa}
    for code, ex, ratio in legacy:
        if (code, ex) not in existing:
            total += int(put_action(Action(code, ex, ratio, Decimal(1), source="jquants",
                                          source_url="https://jpx-jquants.com/ja/spec/eq-bars-daily", price_ratio=ratio)))
    resolved = resolve_disclosures(limit=PRICE_ACTION_DISCLOSURE_LIMIT)
    conflicts = resolve_mature_conflicts()
    return {"changed": total + resolved["changed"] + conflicts["changed"], "coverage_ok": not errors,
            "errors": errors + resolved["errors"] + conflicts["errors"]}


def resolve_disclosures(*, limit: int = 30) -> dict:
    import pdfplumber
    all_actions = load_actions()
    with db() as cur:
        cur.execute("""SELECT DISTINCT code FROM disclosures WHERE disclosed_at >= %s
                       AND (title LIKE %s OR title LIKE %s OR title LIKE %s)
                       AND (title LIKE %s OR title LIKE %s OR title LIKE %s)""",
                    (jst_today() - timedelta(days=14), "%分割%", "%併合%", "%投資口%",
                     "%訂正%", "%中止%", "%撤回%"))
        revised_codes = {r[0] for r in cur.fetchall()}
    pending = [a for aa in all_actions.values() for a in aa
               if a.status == "needs_ratio" or a.code in revised_codes]
    pending.sort(key=lambda a: a.ex_date)
    changed, errors = 0, []
    for action in pending[:limit]:
        with db() as cur:
            cur.execute("""SELECT title,pdf_url FROM disclosures WHERE code=%s
                           AND disclosed_at BETWEEN %s AND %s
                           AND (title LIKE %s OR title LIKE %s OR title LIKE %s)
                           ORDER BY disclosed_at DESC LIMIT 8""",
                        (action.code, datetime.combine(action.ex_date - timedelta(days=150), datetime.min.time()),
                         datetime.combine(action.record_date or action.ex_date, datetime.max.time()),
                         "%分割%", "%併合%", "%投資口分割%"))
            documents = cur.fetchall()
        last_error = "公式の分割比率を含む開示がまだありません"
        resolved = None
        corrected_effective = None
        for title, url in documents:
            if not url or not url.startswith("https://"):
                continue
            try:
                try:
                    content = get_bytes(url)
                except requests.HTTPError as exc:
                    # TDnetの短期公開URLが失効した場合も、JPXの同じ開示番号を使う。
                    match = re.fullmatch(r"https://www\.release\.tdnet\.info/inbs/(\d+)\.pdf", url)
                    if not match or exc.response.status_code != 404:
                        raise
                    url = f"https://www2.jpx.co.jp/disc/{action.code}0/{match[1]}.pdf"
                    content = get_bytes(url)
                _remember_document(url, content, 0, processed=False)
                with pdfplumber.open(io.BytesIO(content)) as pdf:
                    text = "\n".join(p.extract_text() or "" for p in pdf.pages[:8])
                if "訂正" in title and (effective := schedule_only_correction(text, action)):
                    corrected_effective = effective
                    _remember_document(url, content, 1)
                    continue
                resolved = parse_disclosure_ratio(text, replace(action, source_url=url), title)
                if corrected_effective:
                    resolved = replace(resolved,effective_date=corrected_effective)
                changed += int(put_action(resolved, correction="訂正" in title))
                _remember_document(url, content, 1)
                break
            except Exception as exc:
                last_error = str(exc)
                if "訂正" in title or any(w in title for w in ("中止", "撤回")):
                    # 解読できない最新訂正を飛ばして古い比率へ戻さない。
                    break
        else:
            if action.ex_date <= jst_today():
                errors.append(f"{action.code} {action.ex_date}: {last_error}")
            continue
        if resolved is None:
            if action.ex_date <= jst_today():
                errors.append(f"{action.code} {action.ex_date}: {last_error}")
    return {"changed": changed, "errors": errors}
