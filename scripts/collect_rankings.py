from __future__ import annotations

import json
import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
DETAIL_WAIT_SEC = 0.05
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
    maker_name_en: str
    maker_name_ja: str
    unique_key: str
    norm_name: str
    norm_maker_en: str
    norm_maker_ja: str
    norm_unique_key: str


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
    text = (
        text.replace("＋", "+")
        .replace("／", "/")
        .replace("＆", "&")
        .replace("　", " ")
    )
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
    required = {"カテゴリ", "商品名", "定価", "メーカー名①", "メーカー名②", "固有のキー"}
    if not required.issubset(df.columns):
        raise ValueError(f"商品マスタCSVの列が不足しています。必要列: {sorted(required)}")

    masters: List[ProductMaster] = []
    for _, row in df.iterrows():
        category = clean_text(str(row["カテゴリ"]))
        product_name = clean_text(str(row["商品名"]))
        list_price = parse_int_or_none(row["定価"])
        maker_name_en = clean_text(str(row["メーカー名①"])) if not pd.isna(row["メーカー名①"]) else ""
        maker_name_ja = clean_text(str(row["メーカー名②"])) if not pd.isna(row["メーカー名②"]) else ""
        unique_key = clean_text(str(row["固有のキー"])) if not pd.isna(row["固有のキー"]) else ""

        if not category or not product_name:
            continue

        masters.append(
            ProductMaster(
                category=category,
                product_name=product_name,
                list_price=list_price,
                maker_name_en=maker_name_en,
                maker_name_ja=maker_name_ja,
                unique_key=unique_key,
                norm_name=normalize_for_match(product_name),
                norm_maker_en=normalize_for_match(maker_name_en),
                norm_maker_ja=normalize_for_match(maker_name_ja),
                norm_unique_key=normalize_for_match(unique_key),
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
            res = session.get(url, timeout=timeout)
            res.raise_for_status()
            if res.text:
                return res.text
        except Exception as e:
            last_error = e
            time.sleep(min(i * 2, 6))
    raise RuntimeError(f"requests取得失敗: {url} / {repr(last_error)}")


def best_fuzzy_score(master_norm: str, candidate_norm: str) -> float:
    if not master_norm or not candidate_norm:
        return 0.0

    direct_hit = 0.0
    if master_norm in candidate_norm or candidate_norm in master_norm:
        direct_hit = 100.0

    return float(max(
        direct_hit,
        fuzz.ratio(master_norm, candidate_norm),
        fuzz.partial_ratio(master_norm, candidate_norm),
        fuzz.token_set_ratio(master_norm, candidate_norm),
    ))


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
    return score >= 62, score


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


def extract_price_from_text(text: str) -> Optional[int]:
    text = clean_text(text)

    patterns = [
        r"税抜[^\d]{0,30}([\d,]+)\s*円",
        r"([\d,]+)\s*円[^\n]{0,30}税抜",
        r"クーポン[^\d]{0,30}([\d,]+)\s*円",
        r"([\d,]+)\s*円[^\n]{0,30}クーポン",
        r"限定[^\d]{0,20}([\d,]+)\s*円",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1).replace(",", ""))

    m = re.search(r"([\d,]+)\s*円[^\n]{0,20}税込", text)
    if m:
        inclusive = int(m.group(1).replace(",", ""))
        return math.floor(inclusive / 1.1)

    m = re.search(r"([\d,]+)\s*円", text)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def extract_price_from_html(html: str) -> Optional[int]:
    soup = BeautifulSoup(html, "lxml")

    selectors = [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[itemprop="price"]', "content"),
        ('meta[name="twitter:data1"]', "content"),
        ('span[itemprop="price"]', "content"),
        ('span[itemprop="price"]', "data-price"),
    ]
    for selector, attr in selectors:
        tag = soup.select_one(selector)
        if tag:
            value = tag.get(attr)
            price = parse_int_or_none(value)
            if price:
                return price

    for script in soup.find_all("script", type="application/ld+json"):
        txt = script.string or script.get_text()
        if not txt:
            continue
        try:
            obj = json.loads(txt)
            candidates = obj if isinstance(obj, list) else [obj]
            for entry in candidates:
                if not isinstance(entry, dict):
                    continue
                if "offers" in entry:
                    offers = entry["offers"]
                    offers_list = offers if isinstance(offers, list) else [offers]
                    for offer in offers_list:
                        if isinstance(offer, dict) and "price" in offer:
                            price = parse_int_or_none(offer.get("price"))
                            if price:
                                return price
                if "price" in entry:
                    price = parse_int_or_none(entry.get("price"))
                    if price:
                        return price
        except Exception:
            continue

    text = clean_text(soup.get_text(" ", strip=True))
    return extract_price_from_text(text)


def fetch_detail_info(session: requests.Session, url: str, fallback_title: str, fallback_price: Optional[int]) -> Tuple[str, Optional[int]]:
    if not url:
        return fallback_title, fallback_price

    try:
        html = safe_get_html(session, url, timeout=DETAIL_TIMEOUT, attempts=2)
        soup = BeautifulSoup(html, "lxml")

        title = fallback_title

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = clean_text(og_title["content"])
        else:
            h1 = soup.find("h1")
            if h1:
                h1_text = clean_text(h1.get_text(" ", strip=True))
                if h1_text:
                    title = h1_text
            else:
                title_tag = soup.find("title")
                if title_tag:
                    title_text = clean_text(title_tag.get_text(" ", strip=True))
                    if title_text:
                        title = title_text

        price = fallback_price
        if price is None:
            price = extract_price_from_html(html)

        return title, price
    except Exception:
        return fallback_title, fallback_price


def derive_product_name_from_title(title: str) -> str:
    s = clean_text(title)
    if not s:
        return ""

    while True:
        new_s = re.sub(r"^[\[\(【『「][^】』」\]\)]{1,30}[\]）】』」]\s*", "", s)
        if new_s == s:
            break
        s = new_s.strip()

    stop_words = [
        "ウルトラファインバブル",
        "節水シャワー",
        "6段階",
        "ミスト",
        "増圧",
        "手元止水",
        "高洗浄力",
        "毛穴",
        "汚れ",
        "美肌",
        "美髪",
        "新生活",
        "プレゼント",
        "ギフト",
        "スキンケア",
        "節水",
        "保温",
        "保湿",
        "頭皮",
        "洗浄力",
        "保証",
        "爆買",
        "公式",
        "ポイント",
    ]

    cut_pos = len(s)
    for word in stop_words:
        pos = s.find(word)
        if pos > 0:
            cut_pos = min(cut_pos, pos)

    s = s[:cut_pos].strip(" ・|｜-–—/")

    if len(s) > 28:
        parts = s.split(" ")
        compact = []
        for part in parts:
            if not part:
                continue
            trial = " ".join(compact + [part]).strip()
            if len(trial) > 28:
                break
            compact.append(part)
        if compact:
            s = " ".join(compact)

    return clean_text(s)


def choose_top1_name(top_item: Optional[RankingItem], masters: List[ProductMaster]) -> str:
    if top_item is None:
        return ""

    target_masters = [m for m in masters if m.product_name != "各チャネルの1位"]
    best_name = ""
    best_score = -1.0

    for master in target_masters:
        score = max(
            best_fuzzy_score(master.norm_name, normalize_for_match(top_item.listing_title)),
            best_fuzzy_score(master.norm_name, normalize_for_match(top_item.detail_title)),
        )
        if score > best_score:
            best_score = score
            best_name = master.product_name

    if best_score >= 70:
        return best_name

    return derive_product_name_from_title(top_item.detail_title or top_item.listing_title)


def pick_best_item_for_master(master: ProductMaster, ranking_items: List[RankingItem]) -> Optional[RankingItem]:
    matched_items: List[Tuple[RankingItem, float]] = []
    for item in ranking_items:
        matched, score = is_match(master, item)
        if matched:
            matched_items.append((item, score))

    if matched_items:
        matched_items.sort(key=lambda x: (x[0].rank, -x[1]))
        return matched_items[0][0]

    best_item: Optional[RankingItem] = None
    best_score = -1.0
    for item in ranking_items:
        score = max(
            best_fuzzy_score(master.norm_name, normalize_for_match(item.listing_title)),
            best_fuzzy_score(master.norm_name, normalize_for_match(item.detail_title)),
        )
        if score > best_score:
            best_score = score
            best_item = item

    if best_score >= 55:
        return best_item

    return None


def enrich_items_with_detail(session: requests.Session, items: List[RankingItem]) -> List[RankingItem]:
    enriched: List[RankingItem] = []
    for item in items:
        detail_title, detail_price = fetch_detail_info(
            session=session,
            url=item.item_url,
            fallback_title=item.listing_title,
            fallback_price=item.price_value,
        )
        item.detail_title = clean_text(detail_title or item.listing_title)
        item.price_value = detail_price if detail_price is not None else item.price_value
        enriched.append(item)
        time.sleep(DETAIL_WAIT_SEC)
    return enriched


def fetch_rakuten_rankings(category: str, urls: List[str], session: requests.Session) -> List[RankingItem]:
    raw_items: List[RankingItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="ja-JP")

        js = """
        () => {
          function txt(el) {
            return (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
          }

          function getCard(el) {
            let node = el;
            while (node && node !== document.body) {
              const text = txt(node);
              const itemLinks = node.querySelectorAll ? node.querySelectorAll('a[href*="item.rakuten.co.jp"]').length : 0;
              if (text.includes('円') && itemLinks >= 1 && itemLinks <= 3) {
                return node;
              }
              node = node.parentElement;
            }
            return el.parentElement || el;
          }

          const links = [...document.querySelectorAll('a[href*="item.rakuten.co.jp"]')];
          const rows = [];
          const seen = new Set();

          for (const a of links) {
            const href = a.href || '';
            if (!href) continue;

            let title = txt(a);
            if (!title) {
              const img = a.querySelector('img[alt]');
              title = img ? txt(img) : '';
            }
            if (!title) continue;

            const key = href;
            if (seen.has(key)) continue;
            seen.add(key);

            const card = getCard(a);
            const cardText = txt(card);
            const storeLinks = [...(card.querySelectorAll ? card.querySelectorAll('a[href*="www.rakuten.co.jp"]') : [])];
            let storeName = '';
            for (const s of storeLinks) {
              const t = txt(s);
              if (t && !t.includes('楽天市場') && !t.includes('bookmark')) {
                storeName = t;
                break;
              }
            }

            rows.push({
              href: href,
              title: title,
              cardText: cardText,
              storeName: storeName
            });
          }

          return rows;
        }
        """

        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(4000)
                rows = page.evaluate(js)
                for row in rows:
                    raw_items.append(
                        RankingItem(
                            mall="楽天",
                            category=category,
                            rank=0,
                            listing_title=clean_text(row.get("title", "")),
                            detail_title=clean_text(row.get("title", "")),
                            store_name=clean_text(row.get("storeName", "")),
                            price_value=extract_price_from_text(row.get("cardText", "")),
                            item_url=row.get("href", ""),
                        )
                    )
            except Exception:
                continue

        browser.close()

    seen_href = set()
    ordered_items: List[RankingItem] = []
    for item in raw_items:
        href = item.item_url
        if not href or href in seen_href:
            continue
        seen_href.add(href)
        ordered_items.append(item)

    ordered_items = ordered_items[:100]
    for i, item in enumerate(ordered_items, start=1):
        item.rank = i

    return enrich_items_with_detail(session, ordered_items)


def fetch_yahoo_rankings(category: str, url: str, session: requests.Session) -> List[RankingItem]:
    items: List[RankingItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="ja-JP")
        page.goto(url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)

        for _ in range(20):
            try:
                current_count = len(_extract_yahoo_general_rows(page))
                if current_count >= 100:
                    break
                more_button = page.get_by_text("もっと見る", exact=True)
                if more_button.count() == 0 or not more_button.first.is_visible():
                    break
                more_button.first.click(timeout=5000)
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError:
                break
            except Exception:
                break

        rows = _extract_yahoo_general_rows(page)
        browser.close()

    for i, row in enumerate(rows[:100], start=1):
        items.append(
            RankingItem(
                mall="Yahoo!",
                category=category,
                rank=i,
                listing_title=clean_text(row.get("title", "")),
                detail_title=clean_text(row.get("title", "")),
                store_name=clean_text(row.get("storeName", "")),
                price_value=parse_int_or_none(row.get("price")),
                item_url=row.get("href", ""),
            )
        )

    return enrich_items_with_detail(session, items)


def _extract_yahoo_general_rows(page) -> List[Dict[str, object]]:
    js = """
    () => {
      function txt(el) {
        return (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
      }

      function isBefore(a, b) {
        if (!a || !b) return true;
        return !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      }

      function getCard(el) {
        let node = el;
        while (node && node !== document.body) {
          const text = txt(node);
          const itemLinks = node.querySelectorAll ? node.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]').length : 0;
          if (text.includes('円') && itemLinks >= 1 && itemLinks <= 4) {
            return node;
          }
          node = node.parentElement;
        }
        return el.parentElement || el;
      }

      const brandHeading = [...document.querySelectorAll('*')].find(el => txt(el) === 'ブランド別 ランキング');
      const links = [...document.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]')];
      const rows = [];
      const seen = new Set();

      for (const a of links) {
        if (!isBefore(a, brandHeading)) continue;

        const href = a.href || '';
        if (!href) continue;

        let title = txt(a);
        if (!title) {
          const img = a.querySelector('img[alt]');
          title = img ? txt(img) : '';
        }
        if (!title) continue;

        const card = getCard(a);
        const cardText = txt(card);
        if (!cardText.includes('円')) continue;

        const key = href;
        if (seen.has(key)) continue;
        seen.add(key);

        const storeLinks = [...(card.querySelectorAll ? card.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]') : [])];
        let storeName = '';
        for (const s of storeLinks) {
          const t = txt(s);
          if (t && t !== title) {
            const href2 = s.href || '';
            if (href2 && href2.includes('store.shopping.yahoo.co.jp') && !href2.includes('/item/')) {
              storeName = t;
              break;
            }
          }
        }

        const priceMatch = cardText.match(/([\\d,]+)\\s*円/);
        rows.push({
          href: href,
          title: title,
          storeName: storeName,
          price: priceMatch ? priceMatch[1] : null
        });
      }

      return rows;
    }
    """
    rows = page.evaluate(js)

    dedup_rows: List[Dict[str, object]] = []
    seen_href = set()
    for row in rows:
        href = row.get("href", "")
        title = clean_text(str(row.get("title", "")))
        if not href or not title:
            continue
        if href in seen_href:
            continue
        seen_href.add(href)
        dedup_rows.append(row)

    return dedup_rows


def _contains_token(target_text: str, token: str) -> bool:
    if not token:
        return False
    return token in target_text


def yahoo_match_score(master: ProductMaster, item: RankingItem) -> float:
    combined = clean_text(f"{item.listing_title} {item.detail_title} {item.store_name}")
    norm_combined = normalize_for_match(combined)

    score = 0.0

    name_score = max(
        best_fuzzy_score(master.norm_name, normalize_for_match(item.listing_title)),
        best_fuzzy_score(master.norm_name, normalize_for_match(item.detail_title)),
        best_fuzzy_score(master.norm_name, norm_combined),
    )
    score += name_score * 0.35

    maker_hit = False
    if master.norm_maker_en and _contains_token(norm_combined, master.norm_maker_en):
        maker_hit = True
        score += 35
    if master.norm_maker_ja and _contains_token(norm_combined, master.norm_maker_ja):
        maker_hit = True
        score += 35

    key_hit = False
    if master.norm_unique_key and _contains_token(norm_combined, master.norm_unique_key):
        key_hit = True
        score += 45

    # ReFa系
    if master.norm_maker_en == "refa" or master.norm_maker_ja == "リファ":
        if "refa" in norm_combined or "リファ" in combined:
            score += 20
        if master.norm_unique_key == "u+":
            if "u+" in norm_combined:
                score += 50
            if "u" in norm_combined and "u+" not in norm_combined:
                score -= 35
        elif master.norm_unique_key == "u":
            if "u+" in norm_combined:
                score -= 40
            elif re.search(r"(finebubbleu)(?!\+)", norm_combined):
                score += 45
            elif "u" in norm_combined:
                score += 15
        elif master.norm_unique_key in {"veil", "pure", "150", "120", "90"}:
            if master.norm_unique_key in norm_combined:
                score += 55
            else:
                score -= 30

    # MYTREX系
    if master.norm_maker_en == "mytrex" or master.norm_maker_ja == "マイトレックス":
        if "mytrex" in norm_combined or "マイトレックス" in combined:
            score += 25
        if "hihofinebubble" in norm_combined:
            score += 20
        if master.norm_unique_key == "+e":
            if "+e" in norm_combined or "＋e" in combined:
                score += 60
            elif "+" in norm_combined or "＋" in combined:
                score -= 25
        elif master.norm_unique_key == "+":
            if "+e" in norm_combined or "＋e" in combined:
                score -= 35
            elif "+" in norm_combined or "＋" in combined:
                score += 45

    # ミラブル系
    if master.norm_maker_ja == "ミラブル":
        if "ミラブル" in combined:
            score += 25
        if master.norm_unique_key in {"plus", "zero"}:
            if master.norm_unique_key in norm_combined:
                score += 60
            else:
                score -= 20
        elif master.norm_unique_key in {"爽", "潤", "艶"}:
            if master.norm_unique_key in combined:
                score += 70
            else:
                score -= 25

    # SALONIA / Panasonic / アイリス
    if master.norm_maker_en == "salonia" or master.norm_maker_ja == "サロニア":
        if "salonia" in norm_combined or "サロニア" in combined:
            score += 35
        if master.norm_unique_key and master.norm_unique_key in norm_combined:
            score += 35

    if master.norm_maker_en == "panasonic" or master.norm_maker_ja == "パナソニック":
        if "panasonic" in norm_combined or "パナソニック" in combined:
            score += 35
        if master.norm_unique_key and master.norm_unique_key in norm_combined:
            score += 45

    if master.norm_maker_en == "micola" or master.norm_maker_ja == "アイリスオーヤマ":
        if "micola" in norm_combined or "アイリスオーヤマ" in combined:
            score += 35
        if master.norm_unique_key and master.norm_unique_key in norm_combined:
            score += 40

    if not maker_hit and (master.norm_maker_en or master.norm_maker_ja):
        score -= 20

    if master.norm_unique_key and not key_hit and master.product_name != "各チャネルの1位":
        score -= 10

    return score


def choose_top1_name_yahoo(top_item: Optional[RankingItem], masters: List[ProductMaster]) -> str:
    if top_item is None:
        return ""

    target_masters = [m for m in masters if m.product_name != "各チャネルの1位"]

    best_master: Optional[ProductMaster] = None
    best_score = -9999.0
    for master in target_masters:
        score = yahoo_match_score(master, top_item)
        if score > best_score:
            best_score = score
            best_master = master

    if best_master and best_score >= 80:
        return best_master.product_name

    return derive_product_name_from_title(top_item.detail_title or top_item.listing_title)


def pick_best_item_for_master_yahoo(master: ProductMaster, ranking_items: List[RankingItem]) -> Optional[RankingItem]:
    scored: List[Tuple[RankingItem, float]] = []
    for item in ranking_items:
        score = yahoo_match_score(master, item)
        scored.append((item, score))

    if not scored:
        return None

    scored.sort(key=lambda x: (-x[1], x[0].rank))

    best_item, best_score = scored[0]

    threshold = 85
    if master.product_name == "各チャネルの1位":
        threshold = 0
    elif master.norm_maker_ja == "ミラブル":
        threshold = 95
    elif master.norm_maker_en == "refa":
        threshold = 95
    elif master.norm_maker_en == "mytrex":
        threshold = 90

    if best_score < threshold:
        return None

    return best_item


def build_output_rows(
    mall: str,
    category: str,
    masters: List[ProductMaster],
    ranking_items: List[RankingItem],
    search_date_value: date,
) -> List[Dict[str, str]]:
    common = build_common_fields(search_date_value)
    rows: List[Dict[str, str]] = []

    top_item = ranking_items[0] if ranking_items else None
    if mall == "Yahoo!":
        top1_name = choose_top1_name_yahoo(top_item, masters)
    else:
        top1_name = choose_top1_name(top_item, masters)

    for master in masters:
        if master.product_name == "各チャネルの1位":
            rows.append(
                {
                    "開始日": common["開始日"],
                    "終了日": common["終了日"],
                    "期間": common["期間"],
                    "モール": mall,
                    "カテゴリ": category,
                    "商品名": master.product_name,
                    "セラー or 公式": "",
                    "ランキング": "1" if top_item else "圏外",
                    "各チャネルの1位": top1_name,
                    "配信日": common["配信日"],
                    "年月": common["年月"],
                    "検索実行日": common["検索実行日"],
                    "定価": "" if master.list_price is None else f"{master.list_price:,}",
                    "販売価格": "" if not top_item or top_item.price_value is None else f"{top_item.price_value:,}",
                    "商品タイトル": "" if not top_item else clean_text(top_item.detail_title or top_item.listing_title),
                }
            )
            continue

        if mall == "Yahoo!":
            best_item = pick_best_item_for_master_yahoo(master, ranking_items)
        else:
            best_item = pick_best_item_for_master(master, ranking_items)

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
                "各チャネルの1位": "",
                "配信日": common["配信日"],
                "年月": common["年月"],
                "検索実行日": common["検索実行日"],
                "定価": "" if master.list_price is None else f"{master.list_price:,}",
                "販売価格": "" if not best_item or best_item.price_value is None else f"{best_item.price_value:,}",
                "商品タイトル": "" if not best_item else clean_text(best_item.detail_title or best_item.listing_title),
            }
        )

    return rows


def output_path_for_year(search_date_value: date) -> Path:
    return DATA_DIR / f"{search_date_value.year}_ランキング.csv"


def read_existing_csv(path: Path) -> pd.DataFrame:
    columns = [
        "開始日", "終了日", "期間", "モール", "カテゴリ", "商品名", "セラー or 公式",
        "ランキング", "各チャネルの1位", "配信日", "年月", "検索実行日", "定価", "販売価格", "商品タイトル"
    ]
    if not path.exists():
        return pd.DataFrame(columns=columns)
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
    combined["_mall_order"] = combined["モール"].map({"楽天": 1, "Yahoo!": 2}).fillna(9)
    combined = combined.sort_values(
        by=["_sort_date", "_mall_order", "カテゴリ", "商品名"],
        ascending=[True, True, True, True]
    ).drop(columns=["_sort_date", "_mall_order"])

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

    masters_all = load_master()
    if not masters_all:
        raise ValueError("商品マスタが空です")

    session = get_requests_session()
    all_rows: List[Dict[str, str]] = []

    categories = sorted(set(m.category for m in masters_all))
    for category in categories:
        config = CATEGORY_CONFIG.get(category)
        if not config:
            continue

        masters = [m for m in masters_all if m.category == category]

        rakuten_items = fetch_rakuten_rankings(category, config["rakuten_urls"], session)
        all_rows.extend(build_output_rows("楽天", category, masters, rakuten_items, search_date_value))

        yahoo_items = fetch_yahoo_rankings(category, config["yahoo_url"], session)
        all_rows.extend(build_output_rows("Yahoo!", category, masters, yahoo_items, search_date_value))

    output_path = output_path_for_year(search_date_value)
    append_rows_no_duplicate(output_path, all_rows)

    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else None
    raise SystemExit(run(arg))
