#!/usr/bin/env python3
"""Find live GTA 6 / gaming clipping campaigns on public pages and write campaigns.md.

Reads public pages only (no login, no joining, no accepting terms).
Never guesses: anything not visible on a page is written as 'unknown'.
Never fails the run: on any error it still writes campaigns.md and exits 0.
"""
import datetime
import html
import re
import sys
import urllib.request

SOURCES = [
    ("Clip Tech", "https://www.clip.tech/clippers"),
    ("Content Rewards", "https://contentrewards.com/"),
    ("Whop discover", "https://whop.com/discover/"),
]
KEYWORDS = ("gta", "grand theft auto", "gaming")
BANNED = ("leak", "play early", "piracy", "afk", "money glitch", "fake account")
RATE_RE = re.compile(r"\$\s?\d+(?:\.\d+)?\s*(?:/|per)\s*1\s?[kK]", re.I)
BUDGET_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?\s?[kKmM]?\s*(?:budget|left|remaining|total)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; clip-research)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def to_lines(raw):
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", "\n", raw)
    lines = [re.sub(r"\s+", " ", html.unescape(x)).strip() for x in raw.splitlines()]
    return [x for x in lines if x]


def find_blocks(lines):
    blocks, seen = [], set()
    for i, line in enumerate(lines):
        if any(k in line.lower() for k in KEYWORDS):
            text = " | ".join(lines[max(0, i - 3): i + 4])
            key = text[:120]
            if key in seen:
                continue
            seen.add(key)
            blocks.append(text)
    return blocks[:10]


def main():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    out = ["# Clipping campaigns", "", "Checked: " + now, ""]
    for name, url in SOURCES:
        out.append("## " + name)
        out.append("Link: " + url)
        try:
            lines = to_lines(fetch(url))
            blocks = find_blocks(lines)
            if not blocks:
                out.append("Status: page read, no GTA or gaming campaign visible (it may load its list with JavaScript).")
            for b in blocks:
                low = b.lower()
                bad = [w for w in BANNED if w in low]
                if bad:
                    out.append("- SKIPPED (" + bad[0] + " content not allowed): " + b[:160])
                    continue
                rate = RATE_RE.search(b)
                budget = BUDGET_RE.search(b)
                out.append("- Rate per 1K views: " + (rate.group(0) if rate else "unknown"))
                out.append("  Budget: " + (budget.group(0) if budget else "unknown"))
                out.append("  Text on page: " + b[:300])
        except Exception as exc:  # never break the workflow
            out.append("Status: could not read this page (" + type(exc).__name__ + ").")
        out.append("")
    out.append("Rules behind logins or Discord are not read here. Check each campaign's rules before posting.")
    text = "\n".join(out) + "\n"
    with open("campaigns.md", "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("campaigns.py failed: " + repr(exc))
        sys.exit(0)
