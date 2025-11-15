#!/usr/bin/env python3
"""
watch_almeida.py
- Opens the Almeida ticketing event page with Playwright (Firefox)
- Scans all frames/iframes for performance rows
- Extracts a clean table: Date | Time | Status | Link
- Prints a tidy text table to stdout (for local runs)
- Writes a Markdown table to GitHub Actions run summary (GITHUB_STEP_SUMMARY)
Optional env:
  ALMEIDA_URL       - override event URL
  SAVE_SCREENSHOT   - "1" to save almeida_page.png (debug)
"""

import os, re, json, asyncio, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from playwright.async_api import async_playwright, Frame

EVENT_URL = os.getenv("ALMEIDA_URL", "https://ticketing.almeida.co.uk/events/7992")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36 TicketWatch"
)

# ----- Parsing helpers -------------------------------------------------------

@dataclass
class Perf:
    date: str
    time: str
    status: str
    href: str = ""
    raw: str = ""   # original row text (for debugging)

# Patterns to detect date/time in a single row
WEEKDAYS = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MONTHS   = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)"
TIME_RE  = r"(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)"
DATE_RE  = rf"(?P<weekday>{WEEKDAYS})\s+(?P<day>\d{{1,2}})\s+(?P<month>{MONTHS})\s+(?P<year>\d{{4}})"
ROW_RE   = re.compile(rf"{DATE_RE}\s*,?\s*{TIME_RE}", re.I)

NEG_WORDS = (
    "sold out",
    "unavailable",
    "returns only",
    "not on sale",
    "tickets not on sale",
)
POS_HINTS = (
    "book",
    "select",
    "choose seats",
    "reserve",
    "purchase",
    "available",
    "limited",
)

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_row_text(text: str) -> Tuple[str, str]:
    """Return (date_str, time_str) if the row looks like a performance line."""
    m = ROW_RE.search(text)
    if not m:
        return "", ""
    date_str = f"{m.group('weekday')} {m.group('day')} {m.group('month')} {m.group('year')}"
    time_str = m.group('time').upper().replace(" ", "")
    return date_str, time_str

def classify_status(text: str, has_book: bool) -> str:
    low = text.lower()
    if "limited" in low:
        return "Limited"
    if any(w in low for w in NEG_WORDS):
        # If you prefer to show "Not on sale" distinctly, replace with custom mapping here.
        return "Sold out"
    if has_book or any(h in low for h in POS_HINTS):
        return "Available"
    return "Unknown"

def dedup(items: List[Perf]) -> List[Perf]:
    out, seen = [], set()
    for p in items:
        key = (p.date, p.time, p.status, p.href)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

# ----- Extraction ------------------------------------------------------------

ROW_SELECTORS = [
    # Try structured list rows first
    "li.performance, li.performance-item, li.performanceListItem",
    ".performance-row, .performance, .performanceItem, .PerfRow",
    "table tr",
    "ul li, ol li",
]

async def extract_from_frame(frame: Frame) -> List[Perf]:
    perfs: List[Perf] = []

    # Pass 1: structured rows only (avoids giant blobs)
    for sel in ROW_SELECTORS:
        rows = frame.locator(sel)
        count = await rows.count()
        for i in range(min(count, 300)):
            r = rows.nth(i)
            try:
                txt = " ".join((await r.inner_text()).split())
                if not txt:
                    continue

                # Booking-ish link inside the row
                link = ""
                for lsel in ("a", "button"):
                    links = r.locator(lsel)
                    lcount = await links.count()
                    for j in range(min(lcount, 10)):
                        t = (await links.nth(j).inner_text() or "").lower()
                        if any(h in t for h in ("book", "select", "purchase", "choose")):
                            href = await links.nth(j).get_attribute("href")
                            if href:
                                link = "https://ticketing.almeida.co.uk"+href if href.startswith("/") else href
                                break
                    if link:
                        break

                date_str, time_str = parse_row_text(txt)
                if not date_str and not time_str:
                    continue  # skip non-performance list items

                status = classify_status(txt, bool(link))
                perfs.append(Perf(date=date_str, time=time_str, status=status, href=link, raw=txt))
            except:
                pass

    if perfs:
        return dedup(perfs)

    # Pass 2 (fallback): only if nothing found above
    # Scan smaller content blocks (not whole <body>) to avoid "all dates in one cell"
    small_blocks = frame.locator("section, article, .content, main, [role='main']")
    for i in range(min(await small_blocks.count(), 8)):
        try:
            txt = await small_blocks.nth(i).inner_text()
        except:
            continue
        for line in (l.strip() for l in re.split(r"[\n\r]+| {2,}|\t|\u00a0", txt) if l.strip()):
            date_str, time_str = parse_row_text(line)
            if not date_str and not time_str:
                continue
            status = classify_status(line, False)
            perfs.append(Perf(date=date_str, time=time_str, status=status, href="", raw=line))

    return dedup(perfs)

# ----- Rendering -------------------------------------------------------------

def render_text_table(perfs: List[Perf]) -> str:
    if not perfs:
        return "No performance rows found."
    # Pretty fixed-width layout
    w_date = max(4, min(max(len(p.date) for p in perfs), 30))
    w_time = max(4, 8)
    w_stat = max(6, min(max(len(p.status) for p in perfs), 12))
    header = f"{'Date'.ljust(w_date)}  {'Time'.ljust(w_time)}  {'Status'.ljust(w_stat)}  Link"
    sep = "-"*w_date + "  " + "-"*w_time + "  " + "-"*w_stat + "  " + "-"*30
    lines = [header, sep]
    for p in perfs:
        lines.append(f"{p.date.ljust(w_date)}  {p.time.ljust(w_time)}  {p.status.ljust(w_stat)}  {p.href}")
    return "\n".join(lines)

def write_summary(perfs: List[Perf]):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    # Sort by date then time (as strings; good enough for display)
    perfs_sorted = sorted(perfs, key=lambda p: (p.date, p.time))
    lines = []
    lines.append("## Ticket Availability\n")
    lines.append(f"**URL:** {EVENT_URL}\n")
    lines.append(f"**Checked at:** {now_utc()}\n")
    lines.append("")
    if not perfs_sorted:
        lines.append("_No performance rows found._")
    else:
        lines.append("| Date | Time | Status | Link |")
        lines.append("|------|------|--------|------|")
        for p in perfs_sorted:
            link = p.href or ""
            # Escape pipes if any appear in text
            d = p.date.replace("|", "‖")
            t = p.time.replace("|", "‖")
            s = p.status.replace("|", "‖")
            lines.append(f"| {d} | {t} | {s} | {link} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

# ----- Main ------------------------------------------------------------------

async def fetch_all() -> List[Perf]:
    async with async_playwright() as pw:
        # Use Firefox – this worked best against the site
        browser = await pw.firefox.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width":1280,"height":2200})
        page = await ctx.new_page()
        await page.goto(EVENT_URL, wait_until="networkidle", timeout=180_000)

        # Optional screenshot for debugging in Actions
        if os.getenv("SAVE_SCREENSHOT") == "1":
            try:
                await page.screenshot(path="almeida_page.png", full_page=True)
            except:
                pass

        perfs: List[Perf] = []
        for f in page.frames:
            try:
                perfs.extend(await extract_from_frame(f))
            except:
                pass

        await browser.close()
        return dedup(perfs)

async def main():
    perfs = await fetch_all()

    # 1) Print a tidy text table for local runs
    print(render_text_table(perfs))
    print("\nJSON:", json.dumps(
        {"url": EVENT_URL, "checked_at": now_utc(), "performances": [p.__dict__ for p in perfs]},
        ensure_ascii=False, indent=2
    ))

    # 2) Write a clean Markdown table to the GitHub run summary
    write_summary(perfs)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
