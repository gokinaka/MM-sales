#!/usr/bin/env python3
"""shops.yaml のページを取得し、本文テキストを data/snapshots/ に保存する。

差分が週次の抽出処理の入力になるため、出力はページの見た目ではなく
「意味のある変化があったときだけ変わる」ことを優先して正規化する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "shops.yaml"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
STATUS_FILE = SNAPSHOT_DIR / "_status.json"

WHITESPACE = re.compile(r"[ \t　]+")


def extract_text(html: str, strip_patterns: list[str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    lines = []
    for raw in soup.get_text("\n").splitlines():
        line = WHITESPACE.sub(" ", raw).strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    for pattern in strip_patterns:
        text = re.sub(pattern, "", text)
    return text.strip() + "\n"


def fetch(session, url: str, timeout: int, retries: int, record: dict):
    """一時的な失敗を再試行する。GitHub Actions のランナーからは接続が
    落とされる、または極端に遅いサイトがあるため、諦める前に数回待つ。"""
    delay = 2
    for attempt in range(1, retries + 1):
        last = attempt == retries
        record["attempts"] = attempt
        try:
            response = session.get(url, timeout=timeout)
        except requests.RequestException:
            if last:
                raise
            time.sleep(delay)
            delay *= 2
            continue

        record["http_status"] = response.status_code
        if response.status_code < 400:
            return response
        # 4xx はサイト側の確定的な応答なので即座に諦める。HTTPError を上の
        # except の外で投げるのは、それが RequestException を継承しており
        # 内側で捕まると再試行に回ってしまうため。
        if response.status_code < 500 or last:
            response.raise_for_status()
        time.sleep(delay)
        delay *= 2


class Renderer:
    """JavaScript でページを組み立てるサイト向けに、ヘッドレスブラウザで
    描画後の HTML を返す。ブラウザの起動は高価なので、render を要求する
    ページが実際に現れるまで立ち上げない。"""

    def __init__(self, user_agent: str, timeout: int) -> None:
        self.user_agent = user_agent
        self.timeout_ms = timeout * 1000
        self._pw = None
        self._browser = None

    def _browser_or_start(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            # ブラウザを同梱済みでダウンロードできない環境向けの逃げ道。
            self._browser = self._pw.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_EXECUTABLE") or None
            )
        return self._browser

    def html(self, url: str) -> tuple[int, str]:
        ctx = self._browser_or_start().new_context(user_agent=self.user_agent)
        try:
            page = ctx.new_page()
            # networkidle を到達条件にすると、計測タグを鳴らし続けるサイトでは
            # 永久に満たされず丸ごと失敗する。到達を待つのは DOM までにして、
            # 静定は best-effort で待つ。
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            return (response.status if response else 0), page.content()
        finally:
            ctx.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = self._pw = None


class RobotsCache:
    """ホストごとの robots.txt 判定。取得できない場合は許可として扱う。"""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                parser.read()
            except Exception:
                parser = None
            self._cache[origin] = parser

        parser = self._cache[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)


def wanted(shop: dict, page: dict, mode: str) -> bool:
    if not shop.get("enabled", True) or not page.get("enabled", True):
        return False
    if mode == "all":
        return True
    # 遮断はホスト単位で起きるので、同じ店舗でもページごとに経路が分かれる。
    blocked = page.get("runner_blocked", shop.get("runner_blocked", False))
    return blocked if mode == "blocked" else not blocked


def main() -> int:
    mode = "all"
    if len(sys.argv) > 1:
        mode = sys.argv[1].removeprefix("--mode=")
        if mode not in ("all", "runner", "blocked"):
            print(f"unknown mode: {mode}", file=sys.stderr)
            return 2

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    user_agent = defaults.get("user_agent", "MM-sales-calendar/1.0")
    timeout = defaults.get("timeout_sec", 20)
    delay = defaults.get("delay_sec", 3)
    retries = defaults.get("retries", 3)

    robots = RobotsCache(user_agent)
    renderer = Renderer(user_agent, timeout)
    session = requests.Session()
    session.headers["User-Agent"] = user_agent

    results = []
    first_request = True

    for shop in config.get("shops", []):
        strip_patterns = shop.get("strip_patterns", [])
        shop_dir = SNAPSHOT_DIR / shop["id"]

        for page in shop.get("pages", []):
            if not wanted(shop, page, mode):
                continue

            url = page["url"]
            record = {"shop": shop["id"], "page": page["id"], "url": url}

            if not first_request:
                time.sleep(delay)
            first_request = False

            if not robots.allows(url):
                record["result"] = "robots_disallowed"
                results.append(record)
                continue

            try:
                if page.get("render", shop.get("render", False)):
                    record["rendered"] = True
                    status, html = renderer.html(url)
                    record["http_status"] = status
                    if status >= 400:
                        raise RuntimeError(f"HTTP {status}")
                else:
                    response = fetch(session, url, timeout, retries, record)
                    response.encoding = response.apparent_encoding or response.encoding
                    html = response.text
                text = extract_text(html, strip_patterns)
            except Exception as exc:
                record["result"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"
                results.append(record)
                continue

            path = shop_dir / f"{page['id']}.txt"
            previous = path.read_text(encoding="utf-8") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

            record["result"] = "ok"
            record["changed"] = previous != text
            record["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            record["chars"] = len(text)
            results.append(record)

    renderer.close()

    # runner モードと blocked モードは別の経路から別々に走るため、
    # 自分が担当した分だけを差し替えて相手の結果を消さないようにする。
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    status: dict = {}
    if STATUS_FILE.exists():
        try:
            loaded = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded.get("runs"), dict):
                status = loaded
        except json.JSONDecodeError:
            pass
    status.setdefault("runs", {})
    status["runs"][mode] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "results": results,
    }
    STATUS_FILE.write_text(
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    ok = sum(1 for r in results if r["result"] == "ok")
    changed = sum(1 for r in results if r.get("changed"))
    print(f"[{mode}] {ok}/{len(results)} pages fetched, {changed} changed")
    for record in results:
        if record["result"] != "ok":
            print(f"  FAILED {record['shop']}/{record['page']}: "
                  f"{record['result']} {record.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
