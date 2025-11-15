# watch_almeida.py
# Requires: playwright (runner installs browsers)
import os, re, json, asyncio
from datetime import datetime
from playwright.async_api import async_playwright

EVENT_URL = "https://ticketing.almeida.co.uk/events/7992"
USER_AGENT = "Mozilla/5.0 (TicketWatch; contact: mikeyaskins@gmail.com)"

# Pattern that matches a typical performance line like:
# "Wed 05 Feb 2025 19:30" or "Wed 5 Feb 7:30pm"
WEEKDAYS = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
DATE_PAT  = r"(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9})"
TIME_PAT  = r"(\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?)"
LINE_RE   = re.compile(rf"{WEEKDAYS}.*?{DATE_PAT}.*?{TIME_PAT}", re.IGNORECASE)

def clean_spaces(s: str) -> str:
    return " ".join(s.split())

async def fetch_available():
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        ctx = await browser.new_context(user_agent=USER_AGENT)
        page = await ctx.new_page()
        # A long timeout so we wait out any slow responses
        await page.goto(EVENT_URL, wait_until="networkidle", timeout=120_000)

        # Grab visible text from likely containers; fallback to body text.
        candidates = []
        for sel in ["section", "main", "[role='main']", ".event", ".events", ".performances", "body"]:
            loc = page.locator(sel)
            if await loc.count() > 0:
                for i in range(await loc.count()):
                    t = await loc.nth(i).inner_text()
                    candidates.append(t)

        fulltext = clean_spaces("\n".join(candidates))

        # Split into logical lines so we can evaluate individual rows
        raw_lines = [clean_spaces(x) for x in fulltext.split("\n") if x.strip()]

        # Keep the ones that look like performance rows and are not sold out
        available = []
        for line in raw_lines:
            if "sold out" in line.lower():
                continue
            if LINE_RE.search(line):
                available.append(line)

        # Try to capture a link for booking if present
        # (Some sites put a "Book" link per row; we attempt to collect anchors nearby)
        links = []
        for a in await page.locator("a").all():
            try:
                href = await a.get_attribute("href")
                txt  = clean_spaces((await a.inner_text()) or "")
            except:
                continue
            if not href:
                continue
            if any(k in txt.lower() for k in ["book", "tickets", "select", "choose"]) or "performance" in (href.lower()):
                if href.startswith("/"):
                    href = "https://ticketing.almeida.co.uk" + href
                links.append({"text": txt, "href": href})

        return available, links

def write_summary(available):
    summary = []
    summary.append(f"## Ticket availability for '{EVENT_URL}'")
    summary.append(f"_Checked at {datetime.utcnow().isoformat()}Z_")
    if available:
        summary.append("")
        summary.append("**Available (not marked Sold out):**")
        for line in available:
            summary.append(f"- {line}")
    else:
        summary.append("")
        summary.append("No available dates found (everything appears Sold out).")
    return "\n".join(summary)

async def main():
    avail, links = await fetch_available()

    # Print machine-readable JSON (useful for logs or downstream)
    print(json.dumps({"available": avail, "links": links}, ensure_ascii=False, indent=2))

    # Also write a GitHub Actions step summary if env var is present
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(write_summary(avail) + "\n")

    # Optional Slack webhook (set SLACK_WEBHOOK in repo secrets)
    webhook = os.getenv("SLACK_WEBHOOK")
    if webhook and avail:
        import requests
        text = "*Almeida ticketwatch:* available dates detected:\n" + "\n".join(f"• {x}" for x in avail)
        try:
            requests.post(webhook, json={"text": text}, timeout=10)
        except Exception as e:
            print("Slack notify error:", e)

if __name__ == "__main__":
    asyncio.run(main())
