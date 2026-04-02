from __future__ import annotations

import csv
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set, Tuple

from zoneinfo import ZoneInfo
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


# =========================================================
# 基本設定
# =========================================================
JST = ZoneInfo("Asia/Tokyo")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "targets.json"
DATA_DIR = BASE_DIR / "data"

CSV_HEADERS = [
    "開始日",
    "終了日",
    "期間",
    "配信日",
    "年月",
    "検索実行日",
    "モール",
    "ランキング",
    "カテゴリ",
    "メーカー名",
    "商品名",
    "商品タイトル",
    "販売価格",
    "値引き情報",
    "商品ページURL",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 45000
MAX_RETRY = 3
SCROLL_WAIT_MS = 1500
MIN_SLEEP_SEC = 0.6
MAX_SLEEP_SEC = 1.3


# =========================================================
# データ構造
# =========================================================
@dataclass
class Target:
    mall: str
    category: str
    page_type: str
    url: str
    rank_start: int
    rank_end: int


@dataclass
class RankingCandidate:
    ranking: int = 0
    detail_url: str = ""
    list_title: str = ""
    list_price_text: str = ""
    list_direct_discount_text: str = ""
    list_coupon_text: str = ""


@dataclass
class DetailInfo:
    ranking: int
    maker_name: str
    product_name: str
    product_title: str
    price: str
    discount_text: str
    detail_url: str


# =========================================================
# 共通関数
# =========================================================
def log(message: str) -> None:
    print(message, flush=True)


def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sleep_random() -> None:
    time.sleep(random.uniform(MIN_SLEEP_SEC, MAX_SLEEP_SEC))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_jst_now() -> datetime:
    forced = os.getenv("RUN_DATE", "").strip()
    if forced:
        try:
            d = datetime.strptime(forced, "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=JST)
        except ValueError:
            log(f"[WARN] RUN_DATE の形式が不正です: {forced} / 期待形式: YYYY-MM-DD")
    return datetime.now(JST)


def format_ymd_slash(dt) -> str:
    return dt.strftime("%Y/%m/%d")


def weekday_jp(dt) -> str:
    return "月火水木金土日"[dt.weekday()]


def format_period(start_date, end_date) -> str:
    return (
        f"{start_date.month}月{start_date.day}日({weekday_jp(start_date)})"
        f"～"
        f"{end_date.month}月{end_date.day}日({weekday_jp(end_date)})"
    )


def format_delivery_date(dt) -> str:
    return f"{dt.month}月{dt.day}日"


def format_year_month(dt) -> str:
    return f"{dt.year}年{dt.month}月"


def get_csv_path_for_year(year: int) -> Path:
    return DATA_DIR / f"{year}_ランキング.csv"


def load_targets() -> List[Target]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    targets: List[Target] = []
    for item in data["targets"]:
        if item["mall"] != "Yahoo!":
            continue
        targets.append(
            Target(
                mall=item["mall"],
                category=item["category"],
                page_type=item["page_type"],
                url=item["url"],
                rank_start=int(item["rank_start"]),
                rank_end=int(item["rank_end"]),
            )
        )
    return targets


def ensure_csv_exists(csv_path: Path) -> None:
    if csv_path.exists():
        return
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
    log(f"[INFO] 新規CSV作成: {csv_path}")


def read_same_day_keys(csv_path: Path, run_date_str: str) -> Set[Tuple[str, str, str, str, str]]:
    keys: Set[Tuple[str, str, str, str, str]] = set()

    if not csv_path.exists():
        return keys

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if normalize_text(row.get("検索実行日")) != run_date_str:
                continue
            key = (
                normalize_text(row.get("検索実行日")),
                normalize_text(row.get("モール")),
                normalize_text(row.get("カテゴリ")),
                normalize_text(str(row.get("ランキング", ""))),
                normalize_text(row.get("商品ページURL")),
            )
            keys.add(key)

    return keys


def append_rows(csv_path: Path, rows: List[List[str]]) -> None:
    if not rows:
        return
    with csv_path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    log(f"[INFO] CSV追記件数: {len(rows)}")


def safe_goto(page: Page, url: str, label: str) -> None:
    last_error = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            log(f"[INFO] ページ遷移開始: {label} / attempt={attempt} / {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(1500)
            return
        except Exception as e:
            last_error = e
            log(f"[WARN] ページ遷移失敗: {label} / attempt={attempt} / {url} / {e}")
            page.wait_for_timeout(2000)
    raise RuntimeError(f"ページ遷移失敗: {label} / {url} / {last_error}")


def clean_url(url: str) -> str:
    return normalize_text(url)


def normalize_yahoo_product_url(url: str) -> str:
    url = clean_url(url)
    if not url:
        return ""
    url = url.replace("-img", "-title")
    url = url.replace("-image", "-title")
    return url


def extract_first_int_price(text: str) -> str:
    text = normalize_text(text)
    m = re.search(r"([0-9][0-9,]*)\s*円", text)
    if m:
        return m.group(1).replace(",", "")
    m2 = re.search(r"([0-9][0-9,]*)", text)
    if m2:
        return m2.group(1).replace(",", "")
    return ""


def choose_first_non_empty(*values: str) -> str:
    for v in values:
        v = normalize_text(v)
        if v:
            return v
    return ""


def normalize_percent_text(text: str) -> str:
    text = normalize_text(text)
    text = text.replace("%", "％")
    return text


def extract_direct_discount_text(text: str) -> str:
    text = normalize_text(text)

    patterns = [
        r"([0-9]{1,3}\s*[%％]\s*OFF価格)",
        r"([0-9]{1,3}\s*[%％]\s*OFF(?!クーポン))",
        r"([0-9]{1,3}\s*[%％]\s*オフ(?!クーポン))",
    ]

    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            value = normalize_percent_text(m.group(1))
            value = re.sub(r"\s+", "", value)
            value = value.replace("％OFF価格", "％OFF")
            return value

    return ""


def extract_coupon_text(text: str) -> str:
    text = normalize_text(text)

    m_coupon = re.search(r"([0-9]{1,3}\s*[%％]\s*OFFクーポン)", text, flags=re.IGNORECASE)
    if not m_coupon:
        m_coupon = re.search(r"([0-9]{1,3}\s*[%％]\s*オフクーポン)", text, flags=re.IGNORECASE)

    if not m_coupon:
        return ""

    coupon = normalize_percent_text(m_coupon.group(1))
    coupon = re.sub(r"\s+", "", coupon)
    coupon = coupon.replace("％オフクーポン", "％OFFクーポン")

    m_limit = re.search(r"(値引き上限\s*[0-9,]+円)", text)
    if not m_limit:
        m_limit = re.search(r"(上限\s*[0-9,]+円)", text)

    if m_limit:
        limit_text = normalize_text(m_limit.group(1))
        return f"{coupon} {limit_text}"

    return coupon


def choose_discount_text(direct_discount_text: str, coupon_text: str) -> str:
    direct_discount_text = normalize_text(direct_discount_text)
    coupon_text = normalize_text(coupon_text)
    if direct_discount_text:
        return direct_discount_text
    if coupon_text:
        return coupon_text
    return ""


def remove_brackets_prefix(text: str) -> str:
    text = normalize_text(text)
    for _ in range(10):
        new_text = re.sub(r"^\[[^\]]+\]\s*", "", text)
        new_text = re.sub(r"^【[^】]+】\s*", "", new_text)
        new_text = re.sub(r"^\([^)]+\)\s*", "", new_text)
        if new_text == text:
            break
        text = new_text.strip()
    return text


def strip_leading_maker_from_title(title: str, maker_name: str) -> str:
    title = normalize_text(title)
    maker_name = normalize_text(maker_name)
    if not title:
        return ""

    if maker_name:
        patterns = [
            rf"^{re.escape(maker_name)}[\s　\-_/／・]+",
            rf"^{re.escape(maker_name)}$",
        ]
        for p in patterns:
            new_title = re.sub(p, "", title, flags=re.IGNORECASE).strip()
            if new_title and new_title != title:
                return new_title

    return title


def infer_maker_from_title(title: str) -> str:
    title = remove_brackets_prefix(title)
    title = normalize_text(title)

    if not title:
        return ""

    tokens = [t for t in re.split(r"[\s　]+", title) if t]
    if not tokens:
        return ""

    generic_head_words = {
        "シャワーヘッド", "ドライヤー", "ヘアドライヤー", "ヘアアイロン",
        "ストレートアイロン", "カールアイロン", "脱毛器", "オーラル",
        "電動歯ブラシ", "口腔洗浄器", "ブラシ", "アイロン",
    }

    first = tokens[0]
    if first in generic_head_words:
        return ""

    if re.search(r"(公式通販|公式|最新モデル|正規品|送料無料|限定|ランキング)", first, flags=re.IGNORECASE):
        return ""

    if len(first) <= 20:
        return first

    return ""


def infer_product_name(title: str, maker_name: str = "") -> str:
    title = remove_brackets_prefix(title)
    title = normalize_text(title)

    generic_phrases = [
        "最新モデル", "公式通販", "公式", "正規品", "日本製", "母の日", "父の日",
        "プレゼント", "ギフト", "新生活", "送料無料", "限定", "人気", "おすすめ",
        "シャワーヘッド", "ドライヤー", "ヘアアイロン", "ストレートアイロン",
        "カールアイロン", "2way", "2WAY", "ナノバブル", "ウルトラファインバブル",
        "マイクロナノバブル", "ファインバブル", "ミスト", "節水", "増圧",
        "脱塩素", "高洗浄力", "毛穴汚れ除去", "美肌", "美髪", "手元止水", "6段階",
    ]

    maker_name = normalize_text(maker_name)
    base = strip_leading_maker_from_title(title, maker_name)

    if maker_name and base != title:
        parts = [t for t in re.split(r"[\s　]+", base) if t]
        if parts:
            if len(parts) >= 2 and len(parts[0]) <= 20 and re.search(r"[A-Za-zァ-ヶ一-龯0-9]", parts[0]):
                return " ".join(parts[:2]).strip()
            return parts[0]

    cleaned = base
    for w in generic_phrases:
        cleaned = re.sub(re.escape(w), " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"[|｜/／・,，:：]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    tokens = [t for t in cleaned.split(" ") if t]
    stop_tokens = {
        "ランキング", "通販", "モデル", "価格", "対応", "搭載", "専用",
        "ヘッド", "シャワー", "美容", "家電", "用品",
    }

    meaningful: List[str] = []
    for token in tokens:
        if token in stop_tokens:
            continue
        if len(token) <= 1:
            continue
        meaningful.append(token)
        if len(meaningful) >= 2:
            break

    result = " ".join(meaningful).strip()
    if result:
        return result

    fallback = strip_leading_maker_from_title(remove_brackets_prefix(title), maker_name)
    fallback = re.sub(r"\s+", " ", fallback).strip()
    return fallback[:30]


def split_maker_and_product(product_title: str, maker_from_page: str) -> Tuple[str, str]:
    maker_name = normalize_text(maker_from_page)
    if not maker_name:
        maker_name = infer_maker_from_title(product_title)

    product_name = infer_product_name(product_title, maker_name)

    if not product_name:
        product_name = strip_leading_maker_from_title(product_title, maker_name)

    return maker_name, product_name


def parse_json_ld_objects(page: Page) -> List[dict]:
    scripts = page.locator('script[type="application/ld+json"]').all_text_contents()
    objects: List[dict] = []

    for raw in scripts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for x in data:
                    if isinstance(x, dict):
                        objects.append(x)
            elif isinstance(data, dict):
                if "@graph" in data and isinstance(data["@graph"], list):
                    for x in data["@graph"]:
                        if isinstance(x, dict):
                            objects.append(x)
                else:
                    objects.append(data)
        except Exception:
            continue

    return objects


def extract_name_from_jsonld(jsonlds: List[dict]) -> str:
    for obj in jsonlds:
        typ = str(obj.get("@type", "")).lower()
        if "product" not in typ:
            continue
        name = normalize_text(obj.get("name"))
        if name:
            return name
    return ""


def extract_price_from_jsonld(jsonlds: List[dict]) -> str:
    for obj in jsonlds:
        typ = str(obj.get("@type", "")).lower()
        if "product" not in typ:
            continue
        offers = obj.get("offers")
        if isinstance(offers, dict):
            price = normalize_text(str(offers.get("price", "")))
            if price:
                return re.sub(r"[^\d]", "", price)
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    price = normalize_text(str(offer.get("price", "")))
                    if price:
                        return re.sub(r"[^\d]", "", price)
    return ""


def build_csv_row(
    run_dt: datetime,
    mall: str,
    category: str,
    detail: DetailInfo,
) -> List[str]:
    run_date = run_dt.date()
    start_date = run_date - timedelta(days=5)
    end_date = run_date + timedelta(days=1)
    delivery_date = run_date + timedelta(days=6)

    return [
        format_ymd_slash(start_date),
        format_ymd_slash(end_date),
        format_period(start_date, end_date),
        format_delivery_date(delivery_date),
        format_year_month(delivery_date),
        format_ymd_slash(run_date),
        mall,
        str(detail.ranking),
        category,
        detail.maker_name,
        detail.product_name,
        detail.product_title,
        detail.price,
        detail.discount_text,
        detail.detail_url,
    ]


def build_row_key_from_csv_row(row: List[str]) -> Tuple[str, str, str, str, str]:
    return (
        normalize_text(row[5]),
        normalize_text(row[6]),
        normalize_text(row[8]),
        normalize_text(row[7]),
        normalize_text(row[14]),
    )


# =========================================================
# Yahoo! 一覧ページの取得
# =========================================================
def click_more_if_visible(page: Page) -> bool:
    try:
        clicked = page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('button, a, div, span'));
              for (const el of nodes) {
                const text = (el.textContent || '').trim();
                if (!text) continue;
                if (!text.includes('もっと見る')) continue;

                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                el.click();
                return true;
              }
              return false;
            }
            """
        )
        if clicked:
            page.wait_for_timeout(1800)
            return True
    except Exception:
        pass
    return False


def count_main_ranking_title_urls(page: Page) -> int:
    try:
        return int(
            page.evaluate(
                """
                () => {
                  const anchors = Array.from(document.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]'));
                  const seen = new Set();

                  for (const a of anchors) {
                    let href = a.href || '';
                    if (!href.includes('store.shopping.yahoo.co.jp')) continue;
                    if (href.includes('/brand/')) continue;
                    if (href.includes('-img')) continue;
                    href = href.replace('-image', '-title');
                    if (!href.includes('-title')) continue;
                    seen.add(href);
                  }

                  return seen.size;
                }
                """
            )
        )
    except Exception:
        return 0


def auto_expand_yahoo(page: Page, target_rank: int = 100) -> None:
    last_count = 0
    stable = 0

    for i in range(120):
        page.evaluate(
            """
            () => {
              const step = Math.max(window.innerHeight * 0.95, 800);
              window.scrollBy(0, step);
            }
            """
        )
        page.wait_for_timeout(SCROLL_WAIT_MS)

        clicked = click_more_if_visible(page)
        count = count_main_ranking_title_urls(page)

        log(f"[INFO] Yahoo! 展開 {i + 1}回目 / detected_title_count={count} / clicked_more={clicked}")

        if count >= target_rank:
            return

        if count == last_count and not clicked:
            stable += 1
        else:
            stable = 0

        if stable >= 12:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2500)
            final_count = count_main_ranking_title_urls(page)
            if final_count >= target_rank:
                return

        last_count = count

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2500)


def extract_yahoo_candidates(page: Page, rank_start: int, rank_end: int) -> List[RankingCandidate]:
    auto_expand_yahoo(page, target_rank=rank_end)

    data = page.evaluate(
        """
        () => {
          function normalizeUrl(href) {
            if (!href) return '';
            let u = href;
            u = u.replace('-img', '-title');
            u = u.replace('-image', '-title');
            return u;
          }

          function isProductUrl(href) {
            if (!href) return false;
            if (!href.includes('store.shopping.yahoo.co.jp')) return false;
            if (href.includes('/brand/')) return false;
            if (!href.includes('-title')) return false;
            return true;
          }

          function findCard(el) {
            let node = el;
            let best = el.parentElement || el;

            for (let i = 0; i < 14 && node; i++) {
              const text = (node.innerText || '').trim();
              if (!text) {
                node = node.parentElement;
                continue;
              }

              if (
                /円/.test(text) ||
                /ランキング/.test(text) ||
                /PR/.test(text) ||
                /件/.test(text) ||
                /OFF/.test(text) ||
                /クーポン/.test(text)
              ) {
                best = node;
              }

              node = node.parentElement;
            }

            return best;
          }

          const anchors = Array.from(document.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]'));
          const rows = [];
          const urlSeen = new Set();

          for (const a of anchors) {
            const rawHref = a.href || '';
            const normalized = normalizeUrl(rawHref);

            if (!isProductUrl(normalized)) continue;
            if (urlSeen.has(normalized)) continue;

            const rect = a.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            const card = findCard(a);
            const cardText = (card?.innerText || '').trim();
            const title =
              ((a.textContent || '').trim() || (a.getAttribute('title') || '').trim());

            rows.push({
              url: normalized,
              title: title,
              nearbyText: cardText,
              top: rect.top
            });

            urlSeen.add(normalized);
          }

          return rows;
        }
        """
    )

    if not isinstance(data, list):
        return []

    candidates: List[RankingCandidate] = []

    for item in data:
        url = normalize_yahoo_product_url(item.get("url", ""))
        if not url:
            continue
        if "/brand/" in url:
            continue
        if "-img" in url:
            continue

        nearby = normalize_text(item.get("nearbyText", ""))
        title = normalize_text(item.get("title", ""))

        direct_discount_text = extract_direct_discount_text(nearby)
        coupon_text = ""
        if not direct_discount_text:
            coupon_text = extract_coupon_text(nearby)

        candidates.append(
            RankingCandidate(
                ranking=0,
                detail_url=url,
                list_title=title,
                list_price_text=extract_first_int_price(nearby),
                list_direct_discount_text=direct_discount_text,
                list_coupon_text=coupon_text,
            )
        )

    deduped: List[RankingCandidate] = []
    seen_urls = set()
    for c in candidates:
        if c.detail_url in seen_urls:
            continue
        seen_urls.add(c.detail_url)
        deduped.append(c)

    ranked: List[RankingCandidate] = []
    current_rank = rank_start
    for c in deduped:
        if current_rank > rank_end:
            break
        c.ranking = current_rank
        ranked.append(c)
        current_rank += 1

    return ranked


# =========================================================
# 詳細ページの取得
# =========================================================
def extract_maker_from_detail_page(page: Page) -> str:
    try:
        maker = page.evaluate(
            """
            () => {
              const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();

              const bad = (text) => {
                if (!text) return true;
                if (text.length > 40) return true;
                if (/詳細|最安値|ランキング|レビュー|クーポン|価格|送料無料|件|％|%|円|OFF/i.test(text)) return true;
                if (/Yahoo|ショッピング|ストア|ショップ|公式通販|店$/i.test(text)) return true;
                return false;
              };

              const h1 = document.querySelector('h1');
              if (!h1) return '';

              const candidates = [];

              let node = h1.previousElementSibling;
              for (let i = 0; i < 8 && node; i++) {
                const text = clean(node.innerText);
                if (!bad(text)) candidates.push(text);

                const links = Array.from(node.querySelectorAll('a, span, div, p'));
                for (const el of links) {
                  const t = clean(el.textContent);
                  if (!bad(t)) candidates.push(t);
                }

                node = node.previousElementSibling;
              }

              let parent = h1.parentElement;
              for (let depth = 0; depth < 4 && parent; depth++) {
                const links = Array.from(parent.querySelectorAll('a, span, div, p'));
                for (const el of links) {
                  const text = clean(el.textContent);
                  if (!text) continue;
                  if (el === h1) continue;

                  const rect = el.getBoundingClientRect();
                  const h1Rect = h1.getBoundingClientRect();

                  if (rect.bottom <= h1Rect.top + 5) {
                    if (!bad(text)) candidates.push(text);
                  }
                }
                parent = parent.parentElement;
              }

              const uniq = [...new Set(candidates.map(clean).filter(Boolean))];
              if (uniq.length === 0) return '';

              uniq.sort((a, b) => a.length - b.length);
              return uniq[0] || '';
            }
            """
        )
        return normalize_text(maker)
    except Exception:
        return ""


def extract_detail_info(page: Page, candidate: RankingCandidate, mall: str) -> DetailInfo:
    safe_goto(page, candidate.detail_url, f"detail_{mall}")
    page.wait_for_timeout(1500)

    title_text = normalize_text(page.title())
    jsonlds = parse_json_ld_objects(page)

    og_title = ""
    try:
        og_title = normalize_text(page.locator('meta[property="og:title"]').get_attribute("content"))
    except Exception:
        og_title = ""

    h1_text = ""
    for selector in ["h1", '[data-testid="heading"]', ".item_name", ".productTitle"]:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                h1_text = normalize_text(loc.inner_text())
                if h1_text:
                    break
        except Exception:
            continue

    product_title = choose_first_non_empty(
        extract_name_from_jsonld(jsonlds),
        h1_text,
        og_title,
        title_text,
        candidate.list_title,
    )

    body_text = ""
    try:
        body_text = normalize_text(page.locator("body").inner_text())
    except Exception:
        body_text = ""

    meta_price = ""
    for selector, attr in [
        ('meta[property="product:price:amount"]', "content"),
        ('meta[name="twitter:data1"]', "content"),
    ]:
        try:
            value = page.locator(selector).get_attribute(attr)
            value = normalize_text(value)
            if value:
                meta_price = re.sub(r"[^\d]", "", value)
                if meta_price:
                    break
        except Exception:
            continue

    if not meta_price:
        for selector in [
            '[itemprop="price"]',
            '.price',
            '.item_price',
            '.elPrice',
            '.ProductPrice',
            '.highlightPrice',
        ]:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    txt = normalize_text(loc.inner_text())
                    p = extract_first_int_price(txt)
                    if p:
                        meta_price = p
                        break
            except Exception:
                continue

    price = choose_first_non_empty(
        meta_price,
        extract_price_from_jsonld(jsonlds),
        candidate.list_price_text,
    )

    direct_discount_text = extract_direct_discount_text(body_text)
    coupon_text = ""
    if not direct_discount_text:
        coupon_text = extract_coupon_text(body_text)

    discount_text = choose_discount_text(
        direct_discount_text or candidate.list_direct_discount_text,
        coupon_text or candidate.list_coupon_text,
    )

    detail_url = page.url
    maker_from_page = extract_maker_from_detail_page(page)
    maker_name, product_name = split_maker_and_product(product_title, maker_from_page)

    return DetailInfo(
        ranking=candidate.ranking,
        maker_name=maker_name,
        product_name=product_name,
        product_title=product_title,
        price=price,
        discount_text=discount_text,
        detail_url=detail_url,
    )


# =========================================================
# 収集処理
# =========================================================
def collect_target_rows(context: BrowserContext, target: Target, run_dt: datetime) -> List[List[str]]:
    log(f"[INFO] 収集開始: mall={target.mall} / category={target.category} / url={target.url}")

    page = context.new_page()
    safe_goto(page, target.url, f"list_{target.mall}_{target.category}")

    if target.page_type != "yahoo_ranking":
        raise ValueError(f"未対応の page_type: {target.page_type}")

    candidates = extract_yahoo_candidates(page, target.rank_start, target.rank_end)
    page.close()

    log(f"[INFO] 一覧URL抽出件数: mall={target.mall} / category={target.category} / got={len(candidates)}")

    rows: List[List[str]] = []
    success_count = 0
    fail_count = 0

    for candidate in candidates:
        detail_page = context.new_page()
        try:
            sleep_random()
            detail = extract_detail_info(detail_page, candidate, target.mall)

            row = build_csv_row(
                run_dt=run_dt,
                mall=target.mall,
                category=target.category,
                detail=detail,
            )
            rows.append(row)
            success_count += 1

            log(
                f"[INFO] 詳細取得成功: mall={target.mall} / category={target.category} / "
                f"rank={detail.ranking} / url={detail.detail_url} / maker={detail.maker_name}"
            )
        except Exception as e:
            fail_count += 1
            log(
                f"[WARN] 詳細取得失敗: mall={target.mall} / category={target.category} / "
                f"rank={candidate.ranking} / url={candidate.detail_url} / error={e}"
            )
        finally:
            detail_page.close()

    rows.sort(key=lambda r: int(r[7]))

    log(
        f"[INFO] 収集終了: mall={target.mall} / category={target.category} / "
        f"success={success_count} / fail={fail_count} / final_rows={len(rows)}"
    )
    return rows


# =========================================================
# メイン処理
# =========================================================
def create_browser_context(playwright: Playwright) -> Tuple[Browser, BrowserContext]:
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1400, "height": 1800},
    )

    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {
          get: () => undefined
        });
        """
    )

    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    return browser, context


def main() -> int:
    ensure_data_dir()

    run_dt = get_jst_now()
    run_date_str = format_ymd_slash(run_dt.date())
    csv_path = get_csv_path_for_year(run_dt.year)

    ensure_csv_exists(csv_path)
    same_day_keys = read_same_day_keys(csv_path, run_date_str)
    log(f"[INFO] 当日既存キー件数: {len(same_day_keys)} / run_date={run_date_str}")

    targets = load_targets()
    log(f"[INFO] 読み込みターゲット数: {len(targets)}")

    all_rows: List[List[str]] = []

    with sync_playwright() as playwright:
        browser, context = create_browser_context(playwright)
        try:
            for target in targets:
                try:
                    rows = collect_target_rows(context, target, run_dt)
                    all_rows.extend(rows)
                except Exception as e:
                    log(
                        f"[WARN] ターゲット単位で失敗: mall={target.mall} / category={target.category} / "
                        f"url={target.url} / error={e}"
                    )
                    continue
        finally:
            context.close()
            browser.close()

    log(f"[INFO] 総取得件数(重複判定前): {len(all_rows)}")

    new_rows: List[List[str]] = []
    skipped = 0

    for row in all_rows:
        key = build_row_key_from_csv_row(row)
        if key in same_day_keys:
            skipped += 1
            continue
        same_day_keys.add(key)
        new_rows.append(row)

    new_rows.sort(key=lambda r: (r[5], r[6], r[8], int(r[7])))

    log(f"[INFO] 新規追記件数: {len(new_rows)}")
    log(f"[INFO] 重複スキップ件数: {skipped}")

    if new_rows:
        append_rows(csv_path, new_rows)
    else:
        log("[INFO] 追記対象なし")

    return 0


if __name__ == "__main__":
    sys.exit(main())
