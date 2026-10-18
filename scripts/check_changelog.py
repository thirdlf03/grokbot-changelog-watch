#!/usr/bin/env python3
"""Watch Grok Bot RSS changelog and notify via webhook when a new release appears."""

from __future__ import annotations

import argparse
import email.utils
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

CHANGELOG_PAGE = "https://x.ai/changelog/bot"
DEFAULT_FEED_URL = "https://x.ai/changelog/bot/rss.xml"
USER_AGENT = "Mozilla/5.0 (compatible; GrokBotChangelogWatcher/1.0; +https://github.com)"
WEBHOOK_TIMEOUT_SEC = 8


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    title: str
    url: str
    published: str
    guid: str
    sort_key: tuple[int, ...]

    def payload(self) -> dict[str, str]:
        return {
            "version": self.version,
            "title": self.title,
            "url": self.url,
            "published": self.published,
        }


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def discover_feed_url(page_html: str) -> str | None:
    match = re.search(
        r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/rss\+xml["\'][^>]+href=["\']([^"\']+)["\']',
        page_html,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']alternate["\'][^>]+type=["\']application/rss\+xml["\']',
            page_html,
            re.IGNORECASE,
        )
    if not match:
        return None
    href = match.group(1)
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return f"https://x.ai{href}"
    return f"{CHANGELOG_PAGE.rstrip('/')}/{href.lstrip('/')}"


def parse_version(text: str) -> str | None:
    for pattern in (
        r"#(v\d+\.\d+\.\d+)",
        r"\((v?\d+\.\d+\.\d+)\)",
        r"\b(v\d+\.\d+\.\d+)\b",
        r"\b(\d+\.\d+\.\d+)\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            version = match.group(1)
            if not version.startswith("v"):
                version = f"v{version}"
            return version.lower()
    return None


def version_key(version: str) -> tuple[int, ...]:
    nums = version.lstrip("vV").split(".")
    out: list[int] = []
    for part in nums:
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def parse_rss(xml_bytes: bytes) -> list[ReleaseEntry]:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS feed has no channel element")

    entries: list[ReleaseEntry] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or CHANGELOG_PAGE).strip()
        published = (item.findtext("pubDate") or "").strip()
        guid = (item.findtext("guid") or url).strip()
        version = parse_version(guid) or parse_version(url) or parse_version(title)
        if not version:
            continue
        entries.append(
            ReleaseEntry(
                version=version,
                title=title,
                url=url,
                published=published,
                guid=guid,
                sort_key=version_key(version),
            )
        )
    return entries


def load_entries() -> tuple[str, list[ReleaseEntry]]:
    try:
        page = fetch(CHANGELOG_PAGE).decode("utf-8", errors="replace")
        feed_url = discover_feed_url(page) or DEFAULT_FEED_URL
        source = "rss"
    except urllib.error.URLError:
        feed_url = DEFAULT_FEED_URL
        source = "rss_fallback"

    try:
        entries = parse_rss(fetch(feed_url))
        if entries:
            return feed_url, entries
    except (urllib.error.URLError, ET.ParseError):
        pass

    page = fetch(CHANGELOG_PAGE).decode("utf-8", errors="replace")
    version_match = re.search(r"Latest\s+(v\d+\.\d+\.\d+)", page, re.IGNORECASE)
    if not version_match:
        raise RuntimeError("Could not parse changelog feed or page for releases")
    version = version_match.group(1).lower()
    source = "html_fallback"
    print(f"Feed unavailable; using HTML fallback (latest {version})", file=sys.stderr)
    return CHANGELOG_PAGE, [
        ReleaseEntry(
            version=version,
            title=f"Grok Bot {version}",
            url=f"{CHANGELOG_PAGE}#{version}",
            published="",
            guid=f"{CHANGELOG_PAGE}#{version}",
            sort_key=version_key(version),
        )
    ]


def read_state(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RuntimeError(f"State file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, entry: ReleaseEntry) -> None:
    data = {
        "version": entry.version,
        "title": entry.title,
        "url": entry.url,
        "published": entry.published,
        "guid": entry.guid,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def entries_after(state: dict[str, str], entries: list[ReleaseEntry]) -> list[ReleaseEntry]:
    last_version = (state.get("version") or "").lower()
    last_key = version_key(last_version) if last_version else (-1, -1, -1)
    last_guid = state.get("guid")

    newer = [e for e in entries if e.sort_key > last_key]
    if last_guid and not newer:
        # Same top version but unseen guid (edge case)
        seen_guids = {last_guid}
        newer = [e for e in entries if e.guid not in seen_guids and e.sort_key == last_key]

    newer.sort(key=lambda e: (e.sort_key, email.utils.parsedate_to_datetime(e.published).timestamp() if e.published else 0))
    return newer


def post_webhook(entry: ReleaseEntry, webhook_url: str, webhook_key: str) -> int:
    body = json.dumps(entry.payload()).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {webhook_key}",
            "X-Automation-Key": webhook_key,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SEC) as resp:
        return resp.status


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Grok Bot changelog for new releases.")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("state/last_seen.json"),
        help="Path to last-seen state JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads that would be sent; do not POST or update state",
    )
    args = parser.parse_args()

    feed_url, entries = load_entries()
    print(f"Feed URL: {feed_url}")
    if not entries:
        print("No releases found in feed", file=sys.stderr)
        return 1

    latest = max(entries, key=lambda e: e.sort_key)
    print(
        "Latest entry:",
        json.dumps(latest.payload(), ensure_ascii=False),
    )

    state = read_state(args.state)
    pending = entries_after(state, entries)
    if not pending:
        print("No new releases since last seen state.")
        return 0

    print(f"New releases to notify ({len(pending)}), oldest first:")
    for entry in pending:
        print(json.dumps(entry.payload(), ensure_ascii=False))

    if args.dry_run:
        print("Dry run: skipping webhook POST and state update.")
        return 0

    webhook_url = __import__("os").environ.get("GROKBOT_WEBHOOK_URL", "").strip()
    webhook_key = __import__("os").environ.get("GROKBOT_WEBHOOK_KEY", "").strip()
    if not webhook_url or not webhook_key:
        print("GROKBOT_WEBHOOK_URL and GROKBOT_WEBHOOK_KEY must be set", file=sys.stderr)
        return 1

    for entry in pending:
        try:
            status = post_webhook(entry, webhook_url, webhook_key)
        except urllib.error.HTTPError as exc:
            print(f"Webhook failed for {entry.version}: HTTP {exc.code}", file=sys.stderr)
            return 1
        except urllib.error.URLError as exc:
            print(f"Webhook failed for {entry.version}: {exc.reason}", file=sys.stderr)
            return 1

        if status != 200:
            print(f"Webhook failed for {entry.version}: HTTP {status}", file=sys.stderr)
            return 1

        write_state(args.state, entry)
        print(f"Notified and advanced state to {entry.version}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
