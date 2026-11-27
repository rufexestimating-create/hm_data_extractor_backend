"""
Records Lookup (San Antonio Accela portal) - record number -> company, email, website.

No browser needed. Each record number is searched directly and fetched in parallel.

Usage:
    python records_lookup.py LIC-STA-REN25-14901468 LIC-HIC-LIC26-16900829
    python records_lookup.py --file numbers.txt
    python records_lookup.py                      (then paste numbers, empty line to start)
"""

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from rich import box
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()

SEARCH_URL = "https://aca-prod.accela.com/COSA/Cap/GlobalSearchResults.aspx"
OUTPUT_DIR = "output"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
SKIP_EMAIL = ("sanantonio.gov", "accela.com", "noreply", "no-reply", "donotreply")
SKIP_HOSTS = ("sanantonio.gov", "accela", "google", "facebook", "twitter", "instagram",
              "linkedin", "youtube", "microsoft", "w3.org", "adobe")
RECORD_RE = re.compile(r"^[A-Za-z]{2,6}(-[A-Za-z0-9]+){2,}$")


def parse_detail(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    lines = [ln for ln in soup.get_text("\n", strip=True).split("\n") if ln]

    def after(label: str) -> str:
        for i, ln in enumerate(lines):
            if ln.rstrip(":").strip().lower() == label and i + 1 < len(lines):
                val = lines[i + 1]
                return "" if val.endswith(":") else val
        return ""

    company = after("company name")
    if not company:
        for i, ln in enumerate(lines):
            if ln.lower().startswith("company name/business owner"):
                for val in lines[i + 1:i + 4]:
                    if val not in ("Organization", "Individual") and not val.endswith(":"):
                        company = val
                        break
                break

    emails = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            emails.append(a["href"][7:].split("?")[0].strip())
    emails += EMAIL_RE.findall(soup.get_text(" "))
    clean = []
    for e in emails:
        low = e.lower()
        if EMAIL_RE.fullmatch(e) and not any(s in low for s in SKIP_EMAIL) and low not in clean:
            clean.append(low)

    website = after("website") or after("web site")
    if not website:
        for a in soup.find_all("a", href=True):
            h = a["href"]
            host = urlparse(h).netloc.lower()
            if h.startswith("http") and host and not any(s in host for s in SKIP_HOSTS):
                website = h
                break

    return {"company": company, "email": "; ".join(clean), "website": website}


def find_detail_link(html: str, rec: str, base: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if "CapDetail" in a["href"] and a.get_text(strip=True).upper() == rec.upper():
            return urljoin(base, a["href"])
    return ""


async def lookup(session: aiohttp.ClientSession, sem: asyncio.Semaphore, rec: str) -> dict:
    row = {"record": rec, "company": "", "email": "", "website": "", "status": "", "url": ""}
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(SEARCH_URL, params={"isNewQuery": "yes", "QueryText": rec}) as r:
                    html, final = await r.text(), str(r.url)
                if "CapDetail.aspx" not in final:
                    link = find_detail_link(html, rec, final)
                    if not link:
                        row["status"] = "Not found"
                        return row
                    async with session.get(link) as r2:
                        html, final = await r2.text(), str(r2.url)
                row.update(parse_detail(html, final))
                row["url"] = final
                row["status"] = "OK"
                return row
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(0.5 * (attempt + 1))
    row["status"] = "Error"
    return row


async def run(records: list[str], workers: int):
    sem = asyncio.Semaphore(workers)
    results: list[dict] = []
    timeout = aiohttp.ClientTimeout(total=40)
    conn = aiohttp.TCPConnector(limit=workers + 5, ttl_dns_cache=300)
    start = time.time()
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=timeout, connector=conn) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                      console=console) as progress:
            task = progress.add_task("Records check ho rahe hain", total=len(records))

            async def one(rec):
                results.append(await lookup(session, sem, rec))
                progress.advance(task)

            await asyncio.gather(*(one(r) for r in records))
    await asyncio.sleep(0.25)

    order = {r: i for i, r in enumerate(records)}
    results.sort(key=lambda r: order[r["record"]])

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"records_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Record Number", "Company Name", "Email", "Website", "Status", "Detail URL"])
        for r in results:
            w.writerow([r["record"], r["company"], r["email"], r["website"], r["status"], r["url"]])

    tbl = Table(box=box.ROUNDED, show_lines=True)
    for col in ("Record", "Company", "Email", "Website", "Status"):
        tbl.add_column(col, max_width=34)
    for r in results[:40]:
        tbl.add_row(r["record"], r["company"] or "-", r["email"] or "-", r["website"] or "-", r["status"])
    console.print(tbl)
    with_email = sum(1 for r in results if r["email"])
    console.print(f"\n{len(results)} records, {with_email} mein email mila, {time.time() - start:.1f}s")
    console.print(f"Saved: {os.path.abspath(path)}")


def read_records(args) -> list[str]:
    recs = list(args.records)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            recs += f.read().split()
    if not recs:
        console.print("Record numbers paste karo (ek line mein ek). Khatam hone par khali line + Enter:")
        while True:
            line = sys.stdin.readline()
            if not line or not line.strip():
                break
            recs += line.split()
    recs = [r.strip().strip(",;") for r in recs]
    return list(dict.fromkeys(r for r in recs if RECORD_RE.match(r)))


def main():
    p = argparse.ArgumentParser(description="Record number -> company / email / website")
    p.add_argument("records", nargs="*", help="Record numbers")
    p.add_argument("--file", "-f", help="Text file with record numbers")
    p.add_argument("--workers", type=int, default=10, help="Parallel requests (default 10)")
    args = p.parse_args()
    records = read_records(args)
    if not records:
        console.print("[red]Koi valid record number nahi mila.[/red]")
        return
    asyncio.run(run(records, args.workers))


if __name__ == "__main__":
    main()
