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
    "開始日","終了日","期間","配信日","年月","検索実行日",
    "モール","ランキング","カテゴリ","メーカー名","商品名",
    "商品タイトル","販売価格","値引き情報","商品ページURL",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 45000


# =========================================================
# データ構造
# =========================================================
@dataclass
class Target:
    mall: str
    category: str
    url: str


@dataclass
class Candidate:
    ranking: int
    url: str
    title: str
    price: str
    direct_discount: str
    coupon: str


@dataclass
class Detail:
    ranking: int
    maker: str
    product_name: str
    title: str
    price: str
    discount: str
    url: str


# =========================================================
# 共通
# =========================================================
def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def price_to_int(text: str) -> str:
    m = re.search(r"([0-9,]+)", text)
    return m.group(1).replace(",", "") if m else ""


def extract_direct_discount(text: str) -> str:
    m = re.search(r"([0-9]{1,3})\s*[%％]\s*OFF", text, re.I)
    return f"{m.group(1)}％OFF" if m else ""


def extract_coupon(text: str) -> str:
    m = re.search(r"([0-9]{1,3})\s*[%％]\s*OFFクーポン", text, re.I)
    if not m:
        return ""
    limit = re.search(r"(上限\s*[0-9,]+円|値引き上限\s*[0-9,]+円)", text)
    if limit:
        return f"{m.group(1)}％OFFクーポン {normalize(limit.group(1))}"
    return f"{m.group(1)}％OFFクーポン"


def choose_discount(direct, coupon):
    return direct if direct else coupon


# =========================================================
# 一覧取得（ランキング順をここで確定）
# =========================================================
def get_candidates(page: Page) -> List[Candidate]:
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    data = page.evaluate("""
    () => {
        const items = [];
        const cards = document.querySelectorAll('a[href*="store.shopping.yahoo.co.jp"]');
        const seen = new Set();

        for (const a of cards) {
            let url = a.href || "";
            if (!url.includes("-title")) continue;

            if (seen.has(url)) continue;
            seen.add(url);

            const card = a.closest("li, div");
            const text = card ? card.innerText : "";

            items.push({
                url: url,
                title: a.innerText,
                text: text
            });
        }
        return items;
    }
    """)

    results = []
    rank = 1

    for d in data:
        results.append(
            Candidate(
                ranking=rank,
                url=d["url"],
                title=normalize(d["title"]),
                price=price_to_int(d["text"]),
                direct_discount=extract_direct_discount(d["text"]),
                coupon=extract_coupon(d["text"]),
            )
        )
        rank += 1
        if rank > 100:
            break

    return results


# =========================================================
# メーカー名（改善版）
# =========================================================
def extract_maker(page: Page) -> str:
    try:
        return page.evaluate("""
        () => {
            const h1 = document.querySelector("h1");
            if (!h1) return "";

            const nodes = [];
            let el = h1.previousElementSibling;

            for (let i=0; i<5 && el; i++){
                const text = (el.innerText || "").trim();
                if (text && text.length < 30) nodes.push(text);
                el = el.previousElementSibling;
            }

            return nodes.reverse().find(t => !t.match(/価格|円|レビュー|ランキング|件/)) || "";
        }
        """)
    except:
        return ""


# =========================================================
# 商品名
# =========================================================
def infer_name(title: str) -> str:
    title = re.sub(r"【.*?】", "", title)
    title = re.sub(r"\[.*?\]", "", title)
    title = normalize(title)

    words = title.split(" ")
    return " ".join(words[:2])


# =========================================================
# 詳細取得
# =========================================================
def get_detail(page: Page, c: Candidate) -> Detail:
    page.goto(c.url, wait_until="domcontentloaded")

    page.wait_for_timeout(1000)

    title = normalize(page.title())

    body = normalize(page.inner_text("body"))

    price = price_to_int(body) or c.price

    direct = extract_direct_discount(body)
    coupon = extract_coupon(body)

    maker = extract_maker(page)

    return Detail(
        ranking=c.ranking,
        maker=maker,
        product_name=infer_name(title),
        title=title,
        price=price,
        discount=choose_discount(direct or c.direct_discount, coupon or c.coupon),
        url=page.url
    )


# =========================================================
# CSV
# =========================================================
def build_row(run_dt, mall, category, d: Detail):
    start = run_dt.date() - timedelta(days=5)
    end = run_dt.date() + timedelta(days=1)
    delivery = run_dt.date() + timedelta(days=6)

    return [
        start.strftime("%Y/%m/%d"),
        end.strftime("%Y/%m/%d"),
        f"{start.month}月{start.day}日～{end.month}月{end.day}日",
        f"{delivery.month}月{delivery.day}日",
        f"{delivery.year}年{delivery.month}月",
        run_dt.strftime("%Y/%m/%d"),
        mall,
        d.ranking,
        category,
        d.maker,
        d.product_name,
        d.title,
        d.price,
        d.discount,
        d.url,
    ]


# =========================================================
# メイン
# =========================================================
def main():
    run_dt = datetime.now(JST)

    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{run_dt.year}_ランキング.csv"

    if not path.exists():
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        page = context.new_page()
        page.goto("https://shopping.yahoo.co.jp/categoryranking/46691/list")

        candidates = get_candidates(page)

        rows = []
        for c in candidates:
            dp = context.new_page()
            try:
                d = get_detail(dp, c)
                rows.append(build_row(run_dt, "Yahoo!", "シャワー", d))
            finally:
                dp.close()

        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(rows)

        browser.close()


if __name__ == "__main__":
    main()
