#!/usr/bin/env python3
"""shops.yaml のページを取得し、本文テキストを data/snapshots/ に保存する。

差分が週次の抽出処理の入力になるため、出力はページの見た目ではなく
「意味のある変化があったときだけ変わる」ことを優先して正規化する。
"""

from __future__ import annotations

import hashlib
import json
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


def main() -> int:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    user_agent = defaults.get("user_agent", "MM-sales-calendar/1.0")
    timeout = defaults.get("timeout_sec", 20)
    delay = defaults.get("delay_sec", 3)
    retries = defaults.get("retries", 3)

    robots = RobotsCache(user_agent)
    session = requests.Session()
    session.headers["User-Agent"] = user_agent

    results = []
    first_request = True

    for shop in config.get("shops", []):
        if not shop.get("enabled", True):
            continue

        strip_patterns = shop.get("strip_patterns", [])
        shop_dir = SNAPSHOT_DIR / shop["id"]

        for page in shop.get("pages", []):
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
                response = fetch(session, url, timeout, retries, record)
                response.encoding = response.apparent_encoding or response.encoding
                text = extract_text(response.text, strip_patterns)
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

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    ok = sum(1 for r in results if r["result"] == "ok")
    changed = sum(1 for r in results if r.get("changed"))
    print(f"{ok}/{len(results)} pages fetched, {changed} changed")
    for record in results:
        if record["result"] != "ok":
            print(f"  FAILED {record['shop']}/{record['page']}: "
                  f"{record['result']} {record.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
