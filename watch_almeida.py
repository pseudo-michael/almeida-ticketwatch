import os, re, json, asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
from playwright.async_api import async_playwright, Frame

EVENT_URL = os.getenv("ALMEIDA_URL", "https://ticketing.almeida.co.uk/events/7992")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36 TicketWatch"
)

WEEKDAYS = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MONTHS   = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December)"
TIME     = r"\b\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?\b"
DATE     = rf"\b\d{{1,2}}\s+(?:{MONTHS})(?:\s+\d{{4}})?\b"
PERF_RE  = re.compile(rf"{WEEKDAYS}.*?{DATE}.*?{TIME}|{DATE}.*?{TIME}|{WEEKDAYS}.*?{TIME}", re.I)

NEG_WORDS = ("sold out", "unavailable", "returns only", "not on sale")
POS_HINTS = ("book", "select", "choose seats", "reserve", "purchase", "available")

@dataclass
class Perf:
    text: str
    status: str
    href: Optional[str] = None

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def classify_status(text: str, has_book_link: bool) -> str:
    low = text.lower()
    if any(w in low for w in NEG_WORDS):
        return "Sold out"
    if "limited" in low or "few" in low:
        return "Limited"
    if has_book_link or any(h in low for h in POS_HINTS):
        return "Available"
    if re.search(r"\b£\d+", low):
        return "Available"
    return "Unknown"

def dedup(items: List[Perf]) -> List[Perf]:
    out, seen = [], set()
    for p in items:
        key = (p.text, p.status, p.href or "")
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

async def extract_from_frame(frame: Frame) -> List[Perf]:
    perfs: List[Perf] = []
    row_selectors = [
        "li.performance, li.performance-item, li.performanceListItem",
        ".performance-row, .performance, .performanceItem, .PerfRow",
        "table tr, ul li, ol li",
        "div[class*='perform'], section, article",
    ]
    for sel in row_selectors:
        rows = frame.locator(sel)
        count = await rows.count()
        for i in range(min(count, 200)):
            r = rows.nth(i)
            try:
                text = " ".join((await r.inner_text()).split())
                if not text or not PERF_RE.search(text):
                    continue
                link = None
                for lsel in ("a", "button"):
                    links = r.locator(lsel)
                    for j in range(min(await links.count(), 8)):
                        t = (await links.nth(j).inner_text() or "").strip().lower()
                        if any(h in t for h in POS_HINTS):
                            href = await links.nth(j).get_attribute("href")
                            if href and href.startswith("/"):
                                href = f"https://ticketing.almeida.co.uk{href}"
                            link = href or ""
                            break
                    if link:
                        break
                perfs.append(Perf(text=text, status=classify_status(text, bool(link)), href=link))
            except Exception:
                continue
    if perfs:
        return dedup(perfs)

    big_blocks = frame.locator("main, [role='main'], .content, body")
    for i in range(min(await big_blocks.count(), 5)):
        try:
            txt = await big_blocks.nth(i).inner_text()
        except Exception:
            continue
        for line in (l.strip() for l in re.split(r"[\n\r]+", txt) if l.strip()):
            if PERF_RE.search(line):
                perfs.append(Perf(text=" ".join(line.split()), status=classify_status(line, False)))
    return dedup(perfs)

async def fetch_all() -> List[Perf]:
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT)
        page = await ctx.new_page()
        await page.goto(EVENT_URL, wait_until="networkidle", timeout=180_000)
        perfs: List[Perf] = []
        for f in page.frames:
            try:
                perfs.extend(await extract_from_frame(f))
            except Exception:
                continue
        await browser.close()
        return dedup(perfs)

def write_summary(perfs: List[Perf]):
    """Writes a full markdown table to the GitHub run summary."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = []
    lines.append(f"## Ticket Availability\n")
    lines.append(f"**URL:** {EVENT_URL}\n")
    lines.append(f"**Checked at:** {now_utc()}\n")
    lines.append("")
    if not perfs:
        lines.append("_No performance rows found._")
    else:
        lines.append("| Performance | Status | Link |")
        lines.append("|-------------|---------|------|")
        for p in perfs:
            link = p.href or ""
            text = p.text.replace("|", "‖")  # escape any pipe chars
            lines.append(f"| {text} | {p.status} | {link} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def notify(perfs: List[Perf]):
    wh = os.getenv("SLACK_WEBHOOK")
    if wh:
        avail = [p for p in perfs if p.status != "Sold out"]
        if not avail:
            return
        import requests
        msg = "*Almeida ticketwatch:* available or limited dates:\n" + "\n".join(
            f"• {p.text} ({p.status})" for p in avail
        )
        try:
            requests.post(wh, json={"text": msg}, timeout=10)
        except Exception as e:
            print("Slack notify error:", e)

async def main():
    perfs = await fetch_all()
    print(json.dumps(
        {"url": EVENT_URL, "checked_at": now_utc(), "performances": [p.__dict__ for p in perfs]},
        ensure_ascii=False, indent=2
    ))
    write_summary(perfs)
    notify(perfs)

if __name__ == "__main__":
    asyncio.run(main())
