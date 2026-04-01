from __future__ import annotations

import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
MASTER_CSV = CONFIG_DIR / "product_master.csv"

REQUEST_TIMEOUT = (20, 90)
DETAIL_TIMEOUT = (15, 45)
DETAIL_WAIT_SEC = 0.2
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]

CATEGORY_CONFIG = {
    "シャワー": {
        "rakuten_urls": [
            "https://ranking.rakuten.co.jp/weekly/508456/",
            "https://ranking.rakuten.co.jp/weekly/508456/p=2/",
        ],
        "yahoo_url": "https://shopping.yahoo.co.jp/categoryranking/46691/list",
    }
}


@dataclass
class ProductMaster:
    category: str
    product_name: str
    list_price: Optional[int]
    norm_name: str


@dataclass
class RankingItem:
    mall: str
    category: str
    rank: int
    listing_title: str
    detail_title: str
    store_name: str
    price_value: Optional[int]
    item_url: str
    matched_master_name: Optional[str] = None
    match_score: float = 0.0


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def now_jst() -> datetime:
    return datetime.now(JST)


def clean_text(text: str) -> str:
    text = text or ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_for_match(text: str) -> str:
    text = clean_text(text).lower()

    replacements = {
        "＋": "+",
        "／": "/",
        "＆": "&",
        "　": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[ \t\r\n\-ー―‐_/\\.,，。・!！?？:：;；'\"“”‘’()\[\]【】『』<>＜＞|]+", "", text)
    return text


def parse_int_or_none(value) -> Optional[int]:
    if pd.isna(value):
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s else None


def load_master() -> List[ProductMaster]:
    if not MASTER_CSV.exists():
        raise FileNotFoundError(f"商品マスタCSVがありません: {MASTER_CSV}")

    df = pd.read_csv(MASTER_CSV, encoding="utf-8-sig")
    required = {"カテゴリ", "商品名", "定価"}
    if not required.issubset(df.columns):
        raise ValueError(f"商品マスタCSVの列が不足しています。必要列: {sorted(required)}")

    masters: List[ProductMaster] = []
    for _, row in df.iterrows():
        category = clean_text(str(row["カテゴリ"]))
        product_name = clean_text(str(row["商品名"]))
        list_price = parse_int_or_none(row["定価"])
        if not category or not product_name:
            continue
        masters.append(
            ProductMaster(
                category=category,
                product_name=product_name,
                list_price=list_price,
                norm_name=normalize_for_match(product_name),
            )
        )
    return masters


def get_requests_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def safe_get_html(session: requests.Session, url: str, timeout=REQUEST_TIMEOUT, attempts: int = 3) -> str:
    last_error: Optional[Exception] = None

    for i in range(1, attempts + 1):
        try:
            print(f"[INFO] GET {url} attempt={i}/{attempts}")
            res = session.get(url, timeout=timeout)
            res.raise_for_status()
            if res.text:
                return res.text
        except Exception as e:
            last_error = e
            wait_sec = min(3 * i, 10)
            print(f"[WARN] GET失敗: {url} / attempt={i} / error={repr(e)} / wait={wait_sec}s")
            time.sleep(wait_sec)

    raise RuntimeError(f"requests取得失敗: {url} / {repr(last_error)}")


def safe_get_html_with_playwright(url: str, wait_ms: int = 5000) -> str:
    print(f"[INFO] Playwright fallback GET {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="ja-JP")
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
    return html


def get_html_resilient(session: requests.Session, url: str, use_playwright_fallback: bool = True) -> str:
    try:
        return safe_get_html(session, url)
    except Exception as e:
        print(f"[WARN] requestsで取得失敗: {url} / error={repr(e)}")
        if not use_playwright_fallback:
            raise
        return safe_get_html_with_playwright(url)


def collect_text_with_img_alt(tag) -> str:
    parts: List[str] = []
    if tag is None:
        return ""

    txt = tag.get_text(" ", strip=True)
    if txt:
        parts.append(txt)

    for img in tag.find_all("img"):
        alt = clean_text(img.get("alt", ""))
        if alt:
            parts.append(alt)

    return clean_text(" ".join(parts))


def extract_rank_from_text(text: str) -> Optional[int]:
    text = clean_text(text)
    m = re.search(r"(?<!\d)(\d{1,3})\s*位(?!\d)", text)
    if m:
        return int(m.group(1))
    return None


def extract_price_from_text(text: str) -> Optional[int]:
    text = clean_text(text)

    patterns = [
        r"税抜[^\d]{0,15}([\d,]+)\s*円",
        r"([\d,]+)\s*円[^\n]{0,15}税抜",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1).replace(",", ""))

    m = re.search(r"([\d,]+)\s*円[^\n]{0,15}税込", text)
    if m:
        inclusive = int(m.group(1).replace(",", ""))
        return math.floor(inclusive / 1.1)

    m = re.search(r"([\d,]+)\s*円(?:\s*[~～〜])?", text)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def fetch_detail_title(session: requests.Session, url: str, fallback_title: str) -> str:
    if not url:
        return fallback_title

    try:
        html = get_html_resilient(session, url, use_playwright_fallback=False)
        soup = BeautifulSoup(html, "lxml")

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return clean_text(og_title["content"])

        title_tag = soup.find("title")
        if title_tag:
            title_text = clean_text(title_tag.get_text(" ", strip=True))
            if title_text:
                return title_text

        h1 = soup.find("h1")
        if h1:
            h1_text = clean_text(h1.get_text(" ", strip=True))
            if h1_text:
                return h1_text

    except Exception as e:
        print(f"[WARN] 詳細ページ取得失敗: {url} / error={repr(e)}")
        return fallback_title

    return fallback_title


def best_fuzzy_score(master_norm: str, candidate_norm: str) -> float:
    if not master_norm or not candidate_norm:
        return 0.0

    direct_hit = 0.0
    if master_norm in candidate_norm or candidate_norm in master_norm:
        direct_hit = 100.0

    score = max(
        direct_hit,
        fuzz.ratio(master_norm, candidate_norm),
        fuzz.partial_ratio(master_norm, candidate_norm),
        fuzz.token_set_ratio(master_norm, candidate_norm),
    )
    return float(score)


def is_match(master: ProductMaster, item: RankingItem) -> Tuple[bool, float]:
    candidates = [
        item.listing_title,
        item.detail_title,
        f"{item.listing_title} {item.detail_title}",
    ]
    scores = [best_fuzzy_score(master.norm_name, normalize_for_match(c)) for c in candidates if c]
    if not scores:
        return False, 0.0

    score = max(scores)
    matched = score >= 78
    return matched, score


def extract_top1_name(top_item: Optional[RankingItem], masters_in_category: List[ProductMaster]) -> str:
    if top_item is None:
        return ""

    best_name = ""
    best_score = -1.0
    for master in masters_in_category:
        score = max(
            best_fuzzy_score(master.norm_name, normalize_for_match(top_item.listing_title)),
            best_fuzzy_score(master.norm_name, normalize_for_match(top_item.detail_title)),
        )
        if score > best_score:
            best_score = score
            best_name = master.product_name

    if best_score >= 78:
        return best_name

    return top_item.listing_title or top_item.detail_title


def next_wednesday(search_date: date) -> date:
    for i in range(1, 8):
        d = search_date + timedelta(days=i)
        if d.weekday() == 2:
            return d
    return search_date + timedelta(days=6)


def fmt_yyyy_mm_dd(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def fmt_delivery_md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def fmt_year_month(d: date) -> str:
    return f"{d.year}年{d.month}月"


def fmt_period(start_date: date, end_date: date) -> str:
    return (
        f"{start_date.month}月{start_date.day}日({WEEKDAY_JA[start_date.weekday()]})"
        f"～"
        f"{end_date.month}月{end_date.day}日({WEEKDAY_JA[end_date.weekday()]})"
    )


def build_common_fields(search_dt: date) -> Dict[str, str]:
    start_date = search_dt - timedelta(days=5)
    end_date = search_dt + timedelta(days=1)
    delivery_date = next_wednesday(search_dt)

    return {
        "開始日": fmt_yyyy_mm_dd(start_date),
        "終了日": fmt_yyyy_mm_dd(end_date),
        "期間": fmt_period(start_date, end_date),
        "配信日": fmt_delivery_md(delivery_date),
        "年月": fmt_year_month(delivery_date),
        "検索実行日": fmt_yyyy_mm_dd(search_dt),
    }


def parse_rakuten_cards_from_html(category: str, html: str, page_url: str, session: requests.Session) -> List[RankingItem]:
    soup = BeautifulSoup(html, "lxml")
    items: List[RankingItem] = []
    seen = set()

    for anchor in soup.select('a[href*="item.rakuten.co.jp"]'):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        href = urljoin(page_url, href)

        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            continue

        card = anchor
        card_text = ""
        for _ in range(8):
            parent = card.parent
            if parent is None:
                break
            card = parent
            card_text = collect_text_with_img_alt(card)
            rank = extract_rank_from_text(card_text)
            price = extract_price_from_text(card_text)
            if rank is not None and price is not None:
                break

        rank = extract_rank_from_text(card_text)
        if rank is None or rank > 100:
            continue

        key = (rank, href)
        if key in seen:
            continue
        seen.add(key)

        price = extract_price_from_text(card_text)
        detail_title = fetch_detail_title(session, href, title)
        time.sleep(DETAIL_WAIT_SEC)

        items.append(
            RankingItem(
                mall="楽天",
                category=category,
                rank=rank,
                listing_title=title,
                detail_title=detail_title,
                store_name="",
                price_value=price,
                item_url=href,
            )
        )

    dedup: Dict[int, RankingItem] = {}
    for item in sorted(items, key=lambda x: x.rank):
        if item.rank not in dedup:
            dedup[item.rank] = item

    return [dedup[r] for r in sorted(dedup.keys()) if r <= 100]


def fetch_rakuten_rankings(category: str, urls: List[str], session: requests.Session) -> List[RankingItem]:
    all_items: List[RankingItem] = []

    for url in urls:
        try:
            html = get_html_resilient(session, url, use_playwright_fallback=True)
            items = parse_rakuten_cards_from_html(category, html, url, session)
            print(f"[INFO] 楽天ページ取得成功: {url} / items={len(items)}")
            all_items.extend(items)
        except Exception as e:
            print(f"[ERROR] 楽天ページ取得失敗のためスキップ: {url} / error={repr(e)}")
            continue

    dedup_by_rank: Dict[int, RankingItem] = {}
    for item in sorted(all_items, key=lambda x: x.rank):
        if item.rank <= 100 and item.rank not in dedup_by_rank:
            dedup_by_rank[item.rank] = item

    return [dedup_by_rank[r] for r in sorted(dedup_by_rank.keys())]


def fetch_yahoo_rankings(category: str, url: str, session: requests.Session) -> List[RankingItem]:
    items: List[RankingItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="ja-JP")
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)

        for _ in range(20):
            if len(items) >= 100:
                break
            try:
                more_button = page.get_by_text("もっと見る", exact=True)
                if more_button.count() == 0:
                    break
                if not more_button.first.is_visible():
                    break
                more_button.first.click(timeout=5000)
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError:
                break
            except Exception:
                break

            current = _extract_yahoo_items_from_page(page, category)
            if len(current) > len(items):
                items = current

        if not items:
            items = _extract_yahoo_items_from_page(page, category)

        browser.close()

    enriched: List[RankingItem] = []
    for item in items[:100]:
        detail_title = fetch_detail_title(session, item.item_url, item.listing_title)
        time.sleep(DETAIL_WAIT_SEC)
        item.detail_title = detail_title
        enriched.append(item)

    return enriched[:100]


def _extract_yahoo_items_from_page(page, category: str) -> List[RankingItem]:
    js = """
    () => {
      const anchors = [...document.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]')];
      const rows = [];
      const seen = new Set();

      function normalizeText(s) {
        return (s || '').replace(/\\s+/g, ' ').trim();
      }

      for (const a of anchors) {
        const href = a.href || '';
        const title = normalizeText(a.textContent || '');
        if (!href || !title) continue;

        let node = a;
        let chosen = null;

        for (let i = 0; i < 8 && node; i++) {
          const txt = normalizeText(node.innerText || '');
          if (/\\b\\d{1,3}\\s*位\\b/.test(txt) && /円/.test(txt)) {
            chosen = node;
            break;
          }
          node = node.parentElement;
        }

        if (!chosen) continue;

        const text = normalizeText(chosen.innerText || '');
        const rankMatch = text.match(/(?:^|\\s)(\\d{1,3})\\s*位(?:\\s|$)/);
        const priceMatch = text.match(/([\\d,]+)\\s*円/);

        if (!rankMatch) continue;

        const rank = Number(rankMatch[1]);
        if (!rank || rank > 100) continue;

        const key = `${rank}__${href}`;
        if (seen.has(key)) continue;
        seen.add(key);

        rows.push({
          rank: rank,
          title: title,
          href: href,
          text: text,
          price: priceMatch ? Number((priceMatch[1] || '').replace(/,/g, '')) : null
        });
      }

      rows.sort((a, b) => a.rank - b.rank);

      const dedup = [];
      const usedRank = new Set();
      for (const row of rows) {
        if (usedRank.has(row.rank)) continue;
        usedRank.add(row.rank);
        dedup.push(row);
      }

      return dedup.slice(0, 100);
    }
    """

    raw = page.evaluate(js)
    items: List[RankingItem] = []
    for row in raw:
        items.append(
            RankingItem(
                mall="Yahoo!",
                category=category,
                rank=int(row["rank"]),
                listing_title=clean_text(row["title"]),
                detail_title=clean_text(row["title"]),
                store_name="",
                price_value=parse_int_or_none(row["price"]),
                item_url=row["href"],
            )
        )
    return items


def build_output_rows(
    mall: str,
    category: str,
    masters: List[ProductMaster],
    ranking_items: List[RankingItem],
    search_date_value: date,
) -> List[Dict[str, str]]:
    common = build_common_fields(search_date_value)
    rows: List[Dict[str, str]] = []

    top_item = next((x for x in sorted(ranking_items, key=lambda i: i.rank) if x.rank == 1), None)
    top1_name = extract_top1_name(top_item, masters)

    for master in masters:
        matched_items: List[Tuple[RankingItem, float]] = []
        for item in ranking_items:
            matched, score = is_match(master, item)
            if matched:
                matched_items.append((item, score))

        matched_items.sort(key=lambda x: (x[0].rank, -x[1]))
        best_item = matched_items[0][0] if matched_items else None

        rows.append(
            {
                "開始日": common["開始日"],
                "終了日": common["終了日"],
                "期間": common["期間"],
                "モール": mall,
                "カテゴリ": category,
                "商品名": master.product_name,
                "セラー or 公式": "",
                "ランキング": str(best_item.rank) if best_item else "圏外",
                "各チャネルの1位": top1_name,
                "配信日": common["配信日"],
                "年月": common["年月"],
                "検索実行日": common["検索実行日"],
                "定価": "" if master.list_price is None else f"{master.list_price:,}",
                "販売価格": "" if not best_item or best_item.price_value is None else f"{best_item.price_value:,}",
                "商品タイトル": "" if not best_item else (best_item.detail_title or best_item.listing_title),
            }
        )

    return rows


def output_path_for_year(search_date_value: date, mall: str) -> Path:
    safe_mall = mall.replace("!", "")
    return DATA_DIR / f"{search_date_value.year}_{safe_mall}_ランキング.csv"


def read_existing_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=[
            "開始日", "終了日", "期間", "モール", "カテゴリ", "商品名", "セラー or 公式",
            "ランキング", "各チャネルの1位", "配信日", "年月", "検索実行日", "定価", "販売価格", "商品タイトル"
        ])
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")


def append_rows_no_duplicate(path: Path, new_rows: List[Dict[str, str]]) -> bool:
    existing = read_existing_csv(path)
    new_df = pd.DataFrame(new_rows).fillna("")

    if existing.empty:
        combined = new_df.copy()
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)

    combined["_dedup_key"] = (
        combined["モール"].astype(str) + "||" +
        combined["カテゴリ"].astype(str) + "||" +
        combined["商品名"].astype(str) + "||" +
        combined["検索実行日"].astype(str)
    )
    combined = combined.drop_duplicates(subset=["_dedup_key"], keep="first").drop(columns=["_dedup_key"])

    combined["_sort_date"] = pd.to_datetime(combined["検索実行日"], format="%Y/%m/%d", errors="coerce")
    combined = combined.sort_values(
        by=["_sort_date", "モール", "カテゴリ", "商品名"],
        ascending=[True, True, True, True]
    ).drop(columns=["_sort_date"])

    before = len(existing)
    after = len(combined)

    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(path, index=False, encoding="utf-8-sig")

    return after > before


def run(search_date_str: Optional[str] = None) -> int:
    ensure_dirs()

    if search_date_str:
        search_date_value = datetime.strptime(search_date_str, "%Y-%m-%d").date()
    else:
        search_date_value = now_jst().date()

    if search_date_value.weekday() != 3:
        print(f"[INFO] 本日は木曜日ではありません: {search_date_value} / 処理は実行します")
    else:
        print(f"[INFO] 木曜日実行: {search_date_value}")

    masters_all = load_master()
    if not masters_all:
        raise ValueError("商品マスタが空です")

    session = get_requests_session()
    any_changed = False

    categories = sorted(set(m.category for m in masters_all))
    for category in categories:
        config = CATEGORY_CONFIG.get(category)
        if not config:
            print(f"[WARN] CATEGORY_CONFIG 未設定のためスキップ: {category}")
            continue

        masters = [m for m in masters_all if m.category == category]

        print(f"[INFO] 楽天収集中: {category}")
        rakuten_items = fetch_rakuten_rankings(category, config["rakuten_urls"], session)
        rakuten_rows = build_output_rows("楽天", category, masters, rakuten_items, search_date_value)
        rakuten_path = output_path_for_year(search_date_value, "楽天")
        changed = append_rows_no_duplicate(rakuten_path, rakuten_rows)
        any_changed = any_changed or changed
        print(f"[INFO] 楽天 完了: items={len(rakuten_items)} file={rakuten_path.name} changed={changed}")

        print(f"[INFO] Yahoo!収集中: {category}")
        yahoo_items = fetch_yahoo_rankings(category, config["yahoo_url"], session)
        yahoo_rows = build_output_rows("Yahoo!", category, masters, yahoo_items, search_date_value)
        yahoo_path = output_path_for_year(search_date_value, "Yahoo!")
        changed = append_rows_no_duplicate(yahoo_path, yahoo_rows)
        any_changed = any_changed or changed
        print(f"[INFO] Yahoo! 完了: items={len(yahoo_items)} file={yahoo_path.name} changed={changed}")

    print(f"[INFO] all_done any_changed={any_changed}")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else None
    raise SystemExit(run(arg))
