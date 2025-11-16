#!/usr/bin/env python3
import os, re, json, asyncio, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple
from playwright.async_api import async_playwright, Frame

EVENT_URL = os.getenv("ALMEIDA_URL", "https://ticketing.almeida.co.uk/events/7992")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36 TicketWatch"
)

# ---------- Models ----------
@dataclass
class Perf:
    date: str   # e.g. "Saturday 15 November 2025"
    time: str   # e.g. "7:30PM" or "19:30"
    status: str
    href: str = ""
    raw: str = ""   # original row text for debugging

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Patterns & parsing ----------

WEEKDAYS = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
MONTHS   = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)"
TIME_RE  = r"(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)"
# weekday optional, commas optional
DATE_RE  = rf"(?:(?P<weekday>{WEEKDAYS})\s+)?(?P<day>\d{{1,2}})\s+(?P<month>{MONTHS})\s+(?P<year>\d{{4}})"
ROW_RE   = re.compile(rf"{DATE_RE}\s*,?\s*{TIME_RE}", re.I)

NEG_WORDS = ("sold out", "unavailable", "returns only")
NOT_ON_SALE_WORDS = ("not on sale", "tickets not on sale")
POS_HINTS = ("book", "select", "choose seats", "reserve", "purchase", "available", "limited")

MONTH_NUM = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

def parse_row_text(text: str) -> Tuple[str, str]:
    m = ROW_RE.search(text)
    if not m:
        return "", ""
    weekday = (m.group("weekday") or "").strip()
    day     = m.group("day")
    month   = m.group("month")
    year    = m.group("year")
    date_str = f"{(weekday + ' ') if weekday else ''}{day} {month} {year}".strip()
    time_str = m.group("time").upper().replace(" ", "")
    return date_str, time_str

def classify_status(text: str, has_book: bool) -> str:
    low = text.lower()
    if any(w in low for w in NOT_ON_SALE_WORDS):
        return "Not on sale"
    if "limited" in low:
        return "Limited"
    if any(w in low for w in NEG_WORDS):
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

def perf_sort_key(p: Perf):
    """
    Turn (p.date, p.time) into a real (year, month, day, hour, minute) tuple.
    Falls back to string sort if something is weird.
    """
    try:
        # parse date
        parts = p.date.split()
        # e.g. "Saturday 15 November 2025" or "15 November 2025"
        if len(parts) == 4:
            # weekday day month year
            _, day, month, year = parts
        elif len(parts) == 3:
            # day month year
            day, month, year = parts
        else:
            raise ValueError("unexpected date format")
        day_i = int(day)
        month_i = MONTH_NUM[month.lower()]
        year_i = int(year)

        # parse time: "7:30PM" or "19:30"
        t = p.time.replace(" ", "")
        m = re.match(r"(\d{1,2}):(\d{2})(AM|PM)?", t, re.I)
        if not m:
            raise ValueError("unexpected time format")
        hour = int(m.group(1))
        minute = int(m.group(2))
        ap = m.group(3)
        if ap:
            ap = ap.upper()
            if ap == "PM" and hour != 12:
                hour += 12
            if ap == "AM" and hour == 12:
                hour = 0
        return (year_i, month_i, day_i, hour, minute)
    except Exception:
        # shove unknowns to the end, but keep stable order
        return (9999, 12, 31, 23, 59)

# ---------- Extraction ----------

ROW_SELECTORS = [
    "li.performance, li.performance-item, li.performanceListItem",
    ".performance-row, .performance, .performanceItem, .PerfRow",
    "table tr",
    "ul li, ol li",
]

async def extract_from_frame(frame: Frame) -> List[Perf]:
    perfs: List[Perf] = []

    # Pass 1: structured rows
    for sel in ROW_SELECTORS:
        rows = frame.locator(sel)
        count = await rows.count()
        for i in range(min(count, 300)):
            r = rows.nth(i)
            try:
                txt = " ".join((await r.inner_text()).split())
                if not txt:
                    continue

                # booking-ish link
                link = ""
                for lsel in ("a", "button"):
                    links = r.locator(lsel)
                    lcount = await links.count()
                    for j in range(min(lcount, 10)):
                        t = (await links.nth(j).inner_text() or "").lower()
                        if any(h in t for h in ("book", "select", "purchase", "choose", "tickets")):
                            href = await links.nth(j).get_attribute("href")
                            if href:
                                link = "https://ticketing.almeida.co.uk"+href if href.startswith("/") else href
                                break
                    if link:
                        break

                date_str, time_str = parse_row_text(txt)
                if not date_str and not time_str:
                    continue

                status = classify_status(txt, bool(link))
                perfs.append(Perf(date=date_str, time=time_str, status=status, href=link, raw=txt))
            except Exception:
                pass

    if perfs:
        return dedup(perfs)

    # Pass 2 (fallback): only if nothing found
    small_blocks = frame.locator("section, article, .content, main, [role='main']")
    for i in range(min(await small_blocks.count(), 8)):
        try:
            txt = await small_blocks.nth(i).inner_text()
        except Exception:
            continue
        for line in (l.strip() for l in re.split(r"[\n\r]+| {2,}|\t|\u00a0", txt) if l.strip()):
            date_str, time_str = parse_row_text(line)
            if not date_str and not time_str:
                continue
            status = classify_status(line, False)
            perfs.append(Perf(date=date_str, time=time_str, status=status, href="", raw=line))

    return dedup(perfs)

# ---------- Rendering ----------

def render_text_table(perfs: List[Perf]) -> str:
    if not perfs:
        return "No performance rows found."
    perfs_sorted = sorted(perfs, key=perf_sort_key)
    w_date = max(10, min(max(len(p.date) for p in perfs_sorted), 32))
    w_time = 8
    w_stat = max(6, min(max(len(p.status) for p in perfs_sorted), 12))
    header = f"{'Date'.ljust(w_date)}  {'Time'.ljust(w_time)}  {'Status'.ljust(w_stat)}  Link"
    sep = "-"*w_date + "  " + "-"*w_time + "  " + "-"*w_stat + "  " + "-"*30
    lines = [header, sep]
    for p in perfs_sorted:
        lines.append(f"{p.date.ljust(w_date)}  {p.time.ljust(w_time)}  {p.status.ljust(w_stat)}  {p.href}")
    return "\n".join(lines)

def write_summary(perfs: List[Perf]):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    perfs_sorted = sorted(perfs, key=perf_sort_key)
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
            lines.append(f"| {p.date} | {p.time} | {p.status} | {link} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

# ---------- Main ----------

async def fetch_all() -> List[Perf]:
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 2200},
        )
        page = await ctx.new_page()
        await page.goto(EVENT_URL, wait_until="networkidle", timeout=180_000)

        # Optional debug screenshot: set SAVE_SCREENSHOT=1 in env
        if os.getenv("SAVE_SCREENSHOT") == "1":
            try:
                await page.screenshot(path="almeida_page.png", full_page=True)
            except Exception:
                pass

        perfs: List[Perf] = []
        for f in page.frames:
            try:
                perfs.extend(await extract_from_frame(f))
            except Exception:
                pass

        await browser.close()
        return dedup(perfs)

async def main():
    perfs = await fetch_all()

    # 1) Terminal table
    print(render_text_table(perfs))

    # 2) JSON output for logs/artifact
    print("\nJSON:", json.dumps(
        {"url": EVENT_URL, "checked_at": now_utc(), "performances": [p.__dict__ for p in perfs]},
        ensure_ascii=False, indent=2
    ))

    # 3) Run summary table
    write_summary(perfs)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
