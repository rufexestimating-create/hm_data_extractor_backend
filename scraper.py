"""
HM Smart Scraper — Uses YOUR real Chrome browser.
===================================================
How it works:
  1. Opens your real Chrome (not a fake bot browser)
  2. You solve any Cloudflare/CAPTCHA if it appears
  3. Script auto-detects when page loads and scrapes ALL data
  4. Saves to CSV + Excel

This beats Cloudflare because it's YOUR real browser with your cookies.

Usage:
    python scraper.py "https://any-directory-url.com"
    python scraper.py "https://members.texasbuilders.org/builder-directory/findstartswith"
    python scraper.py "https://www.yellowpages.com/los-angeles-ca/plumbers"
"""

import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import time

from playwright.async_api import async_playwright
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.panel import Panel
from rich.table import Table
from rich import box

def _suppress(u):
    msg = str(u.exc_value) if u.exc_value else ""
    if "closed pipe" in msg or "unclosed transport" in str(u.object):
        return
    sys.__unraisablehook__(u)

sys.unraisablehook = _suppress

console = Console()

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9222
OUTPUT_DIR = "output"

BLOCKED_SIGNALS = [
    "just a moment", "attention required", "cloudflare",
    "checking your browser", "captcha-delivery", "datadome",
    "please verify", "are you a robot", "challenge-platform",
    "enable javascript and cookies",
]


def print_banner():
    banner = """
    ╦ ╦╔╦╗  ╔═╗╔╦╗╔═╗╦═╗╔╦╗  ╔═╗╔═╗╦═╗╔═╗╔═╗╔═╗╦═╗
    ╠═╣║║║  ╚═╗║║║╠═╣╠╦╝ ║   ╚═╗║  ╠╦╝╠═╣╠═╝║╣ ╠╦╝
    ╩ ╩╩ ╩  ╚═╝╩ ╩╩ ╩╩╚═ ╩   ╚═╝╚═╝╩╚═╩ ╩╩  ╚═╝╩╚═
    """
    console.print(Panel(banner, title="[bold blue]HM Design Group[/bold blue]",
                        subtitle="Real Chrome Scraper — Beats Cloudflare", style="blue"))


def is_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    check = html[:3000].lower()
    return any(sig in check for sig in BLOCKED_SIGNALS)


def launch_chrome_debug():
    """Launch Chrome with remote debugging enabled."""
    console.print("[bold yellow]Launching your real Chrome browser...[/bold yellow]")

    user_data = os.path.join(os.environ.get("TEMP", "."), "hm-chrome-profile")
    os.makedirs(user_data, exist_ok=True)

    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return proc


async def connect_to_chrome():
    """Connect to running Chrome via CDP."""
    pw = await async_playwright().start()
    for attempt in range(10):
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            return pw, browser
        except Exception:
            await asyncio.sleep(1)
    return pw, None


async def wait_for_real_page(page, timeout=120):
    """Wait until the page has real content (not Cloudflare/CAPTCHA)."""
    start = time.time()
    while time.time() - start < timeout:
        html = await page.content()
        title = await page.title()

        if not is_blocked(html) and len(html) > 2000:
            return html

        if time.time() - start > 5 and time.time() - start < 6:
            console.print("[cyan]  Waiting for page to load...[/cyan]")
        if time.time() - start > 15 and time.time() - start < 16:
            console.print("[yellow]  If you see a CAPTCHA/Cloudflare check, please solve it in Chrome[/yellow]")

        await asyncio.sleep(1)

    return None


async def find_all_links_on_page(page) -> list[dict]:
    """Extract all business/listing links from the current page."""
    return await page.evaluate("""() => {
        const baseUrl = window.location.origin;
        const currentHost = window.location.hostname;
        const links = [];
        const seen = new Set();

        document.querySelectorAll('a[href]').forEach(a => {
            const href = a.href;
            const text = a.textContent.trim();

            if (!href || href === '#' || href.startsWith('javascript:')) return;
            if (seen.has(href)) return;

            // Skip obvious non-listing links
            const lower = href.toLowerCase();
            if (lower.includes('/login') || lower.includes('/signup') ||
                lower.includes('/cart') || lower.includes('/checkout') ||
                lower.includes('/privacy') || lower.includes('/terms') ||
                lower.includes('.css') || lower.includes('.js') ||
                lower.includes('.png') || lower.includes('.jpg') ||
                lower.includes('facebook.com') || lower.includes('twitter.com') ||
                lower.includes('instagram.com') || lower.includes('linkedin.com') ||
                lower.includes('youtube.com') || lower.includes('google.com/maps') ||
                lower.includes('mailto:') || lower.includes('tel:')) return;

            // Must be same domain
            try {
                const url = new URL(href);
                if (url.hostname !== currentHost) return;
                if (url.pathname === '/' || url.pathname === '') return;
            } catch(e) { return; }

            // Check if inside a listing-like container
            const parent = a.closest(
                '[class*="listing"], [class*="result"], [class*="card"], ' +
                '[class*="business"], [class*="item"], [class*="entry"], ' +
                '[class*="member"], [class*="directory"], [class*="profile"], ' +
                'table tr, li, .row, article'
            );

            if (parent || text.length > 3) {
                seen.add(href);
                links.push({ href, text: text.substring(0, 100), hasParent: !!parent });
            }
        });

        // Sort: links inside listing containers first
        links.sort((a, b) => (b.hasParent ? 1 : 0) - (a.hasParent ? 1 : 0));
        return links;
    }""")


async def extract_data_from_page(page) -> dict:
    """Extract business data from a single detail page."""
    return await page.evaluate("""() => {
        const data = { company: '', website: '', email: '', state: '', phone: '', address: '' };

        // Company name
        const ogName = document.querySelector('meta[property="og:site_name"]');
        const ogTitle = document.querySelector('meta[property="og:title"]');
        const h1 = document.querySelector('h1');
        data.company = (ogName?.content || ogTitle?.content || h1?.textContent || document.title || '').trim();
        // Clean "Company | Tagline" pattern
        data.company = data.company.split(/[|–—·•]/)[0].trim();

        // Email
        const mailto = document.querySelector('a[href^="mailto:"]');
        if (mailto) data.email = mailto.href.replace('mailto:', '').split('?')[0];
        if (!data.email) {
            const text = document.body.textContent;
            const m = text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+[.][a-zA-Z]{2,}/);
            if (m) data.email = m[0];
        }

        // Website - external link not to the directory itself
        const dirHost = window.location.hostname;
        for (const a of document.querySelectorAll('a[href^="http"]')) {
            try {
                const url = new URL(a.href);
                if (url.hostname !== dirHost &&
                    !['facebook','twitter','instagram','linkedin','youtube','google','yelp'].some(s => url.hostname.includes(s))) {
                    data.website = a.href;
                    break;
                }
            } catch(e) {}
        }

        // Phone
        const telLink = document.querySelector('a[href^="tel:"]');
        if (telLink) {
            data.phone = telLink.href.replace('tel:', '').replace(/[^0-9+()-]/g, '');
        } else {
            const phoneMatch = document.body.textContent.match(/[(]?\\d{3}[)]?[\\s.-]\\d{3}[\\s.-]\\d{4}/);
            if (phoneMatch) data.phone = phoneMatch[0];
        }

        // Address / State
        const addrEl = document.querySelector('[itemprop="address"], [class*="address"], .adr');
        if (addrEl) data.address = addrEl.textContent.trim().replace(/\\s+/g, ' ');

        // State from address or JSON-LD
        const stateMatch = (data.address || document.body.textContent).match(/,\\s*([A-Z]{2})\\s+\\d{5}/);
        if (stateMatch) data.state = stateMatch[1];

        // JSON-LD fallback
        for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
                const ld = JSON.parse(script.textContent);
                const items = Array.isArray(ld) ? ld : [ld];
                for (const item of items) {
                    const addr = item.address;
                    if (addr && typeof addr === 'object') {
                        if (!data.state && addr.addressRegion) data.state = addr.addressRegion;
                        if (!data.address && addr.streetAddress) {
                            data.address = [addr.streetAddress, addr.addressLocality, addr.addressRegion, addr.postalCode].filter(Boolean).join(', ');
                        }
                    }
                    if (!data.website && item.url) data.website = item.url;
                    if (!data.phone && item.telephone) data.phone = item.telephone;
                    if (!data.email && item.email) data.email = item.email;
                }
            } catch(e) {}
        }

        return data;
    }""")


async def scrape_directory_listing(page) -> list[dict]:
    """Extract all data directly from a directory listing page (table/cards)."""
    return await page.evaluate("""() => {
        const records = [];

        // Strategy 1: Tables
        for (const table of document.querySelectorAll('table')) {
            const headers = Array.from(table.querySelectorAll('th, thead td')).map(h => h.textContent.trim().toLowerCase());
            for (const row of table.querySelectorAll('tbody tr, tr:not(:first-child)')) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 2) continue;
                const rec = { company:'', website:'', email:'', state:'', phone:'', address:'' };

                cells.forEach((cell, i) => {
                    const h = headers[i] || '';
                    const text = cell.textContent.trim();
                    const link = cell.querySelector('a');
                    const mailto = cell.querySelector('a[href^="mailto:"]');

                    if (h.match(/company|name|business|builder|member/i) || i === 0) {
                        if (!rec.company) rec.company = text;
                        if (link && !link.href.includes('mailto:')) rec.detailUrl = link.href;
                    }
                    if (h.match(/website|web|url|site/i) || (link && link.href.match(/^https?:/) && !link.href.includes(location.hostname))) {
                        rec.website = link ? link.href : text;
                    }
                    if (h.match(/email|e-mail/i) || mailto) {
                        rec.email = mailto ? mailto.href.replace('mailto:','').split('?')[0] : (text.includes('@') ? text : '');
                    }
                    if (h.match(/state|st$/i)) rec.state = text;
                    if (h.match(/phone|tel/i)) rec.phone = text;
                    if (h.match(/city|location|address/i)) rec.address = text;
                });

                // Auto-detect from content if no headers
                if (!rec.email) {
                    const em = row.textContent.match(/[\\w.+-]+@[\\w.-]+\\.[a-z]{2,}/i);
                    if (em) rec.email = em[0];
                }
                if (!rec.phone) {
                    const ph = row.textContent.match(/\\(?\\d{3}\\)?[\\s.-]\\d{3}[\\s.-]\\d{4}/);
                    if (ph) rec.phone = ph[0];
                }

                if (rec.company) records.push(rec);
            }
        }

        // Strategy 2: Card/list layouts
        if (records.length === 0) {
            const cards = document.querySelectorAll(
                '[class*="listing"], [class*="result"], [class*="member"], ' +
                '[class*="builder"], [class*="card"]:not(nav *), [class*="directory-item"], ' +
                '.list-group-item, article, [class*="gz-"]'
            );
            for (const card of cards) {
                const rec = { company:'', website:'', email:'', state:'', phone:'', address:'' };
                const nameEl = card.querySelector('h2, h3, h4, h5, strong a, .name, [class*="name"], [class*="title"]');
                if (nameEl) {
                    rec.company = nameEl.textContent.trim();
                    const link = nameEl.closest('a') || nameEl.querySelector('a');
                    if (link) rec.detailUrl = link.href;
                }
                const mailto = card.querySelector('a[href^="mailto:"]');
                if (mailto) rec.email = mailto.href.replace('mailto:','').split('?')[0];
                for (const a of card.querySelectorAll('a[href^="http"]')) {
                    if (!a.href.includes(location.hostname) && !a.href.includes('mailto:') &&
                        !['facebook','twitter','instagram','linkedin','youtube'].some(s => a.href.includes(s))) {
                        rec.website = a.href; break;
                    }
                }
                const ph = card.textContent.match(/\\(?\\d{3}\\)?[\\s.-]\\d{3}[\\s.-]\\d{4}/);
                if (ph) rec.phone = ph[0];
                const st = card.textContent.match(/\\b(TX|CA|NY|FL|IL|PA|OH|GA|NC|MI|NJ|VA|WA|AZ|MA|TN|IN|MO|MD|WI|CO|MN|SC|AL|LA|KY|OR|OK|CT|UT|IA|NV|AR|MS|KS|NM|NE|ID|WV|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WY|DC)\\b/);
                if (st) rec.state = st[1];
                if (rec.company && rec.company.length > 1) records.push(rec);
            }
        }

        return records;
    }""")


def save_results(records: list[dict], filename: str = "scraped_data"):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    headers = ["Company Name", "Website", "Email", "State", "Phone", "Address", "Source URL"]

    csv_path = os.path.join(OUTPUT_DIR, f"{filename}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for b in records:
            writer.writerow([
                b.get("company", ""), b.get("website", ""), b.get("email", ""),
                b.get("state", ""), b.get("phone", ""), b.get("address", ""),
                b.get("detailUrl", b.get("source_url", "")),
            ])

    xlsx_path = os.path.join(OUTPUT_DIR, f"{filename}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Scraped Data"
    hfont = Font(bold=True, color="FFFFFF", size=12)
    hfill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")
    halign = Alignment(horizontal="center", vertical="center", wrap_text=True)
    border = Border(left=Side(style="thin"), right=Side(style="thin"),
                    top=Side(style="thin"), bottom=Side(style="thin"))

    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font, cell.fill, cell.alignment, cell.border = hfont, hfill, halign, border

    for r, b in enumerate(records, 2):
        vals = [b.get("company",""), b.get("website",""), b.get("email",""),
                b.get("state",""), b.get("phone",""), b.get("address",""),
                b.get("detailUrl", b.get("source_url",""))]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = border

    for c in range(1, len(headers)+1):
        ws.column_dimensions[chr(64+c)].width = 25
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(xlsx_path)

    return os.path.abspath(csv_path), os.path.abspath(xlsx_path)


async def run(url: str, visit_details: bool = False):
    print_banner()
    console.print(f"\n[bold cyan]Target:[/bold cyan] {url}\n")

    # Step 1: Launch real Chrome
    chrome_proc = launch_chrome_debug()

    # Step 2: Connect via CDP
    console.print("[bold yellow]Step 1:[/bold yellow] Connecting to Chrome...")
    pw, browser = await connect_to_chrome()

    if not browser:
        console.print("[bold red]Failed to connect to Chrome.[/bold red]")
        console.print("Make sure no other Chrome is running, then try again.")
        chrome_proc.terminate()
        await pw.stop()
        return

    console.print("[bold green]Connected to Chrome![/bold green]\n")

    try:
        # Step 3: Navigate
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        else:
            page = await browser.contexts[0].new_page() if contexts else await (await browser.new_context()).new_page()

        console.print("[bold yellow]Step 2:[/bold yellow] Loading directory page...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Step 4: Wait for real content
        html = await wait_for_real_page(page, timeout=120)
        if not html:
            console.print("[bold red]Page did not load real content in time.[/bold red]")
            console.print("Please solve the CAPTCHA/Cloudflare check and try again.")
            return

        console.print("[bold green]Page loaded successfully![/bold green]\n")

        # Step 5: Try to extract directly from listing page
        console.print("[bold yellow]Step 3:[/bold yellow] Extracting data from directory...")
        records = await scrape_directory_listing(page)

        if records:
            console.print(f"[bold green]Found {len(records)} businesses on this page![/bold green]")
        else:
            # Step 5b: Find listing links and visit detail pages
            console.print("[cyan]No table/cards found — looking for listing links...[/cyan]")
            links = await find_all_links_on_page(page)

            # Filter to links that look like listings (inside containers)
            listing_links = [l for l in links if l.get("hasParent")]
            if not listing_links:
                listing_links = links[:50]

            if not listing_links:
                console.print("[bold red]No listing links found on this page.[/bold red]")
                return

            console.print(f"[cyan]Found {len(listing_links)} links — visiting each one...[/cyan]\n")

            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                          BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                          TimeElapsedColumn(), console=console) as progress:
                task = progress.add_task("Scraping", total=len(listing_links))

                for idx, link in enumerate(listing_links):
                    try:
                        await page.goto(link["href"], wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(2000)

                        data = await extract_data_from_page(page)
                        data["source_url"] = link["href"]
                        if data.get("company"):
                            records.append(data)

                    except Exception:
                        pass

                    progress.update(task, completed=idx+1,
                                    description=f"[cyan]({idx+1}/{len(listing_links)})[/cyan] {(data.get('company',''))[:30] if data else ''}")

        # Step 6: Check for A-Z / pagination and scrape more pages
        letter_links = await page.evaluate("""() => {
            const links = {};
            document.querySelectorAll('a').forEach(a => {
                const text = a.textContent.trim();
                if (/^[A-Z]$/.test(text) || text === '0-9') {
                    const href = a.href;
                    if (href && href !== window.location.href) links[text] = href;
                }
            });
            return links;
        }""")

        if letter_links and len(letter_links) > 5:
            console.print(f"\n[cyan]Found A-Z navigation ({len(letter_links)} letters) — scraping all...[/cyan]")

            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                          BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                          TimeElapsedColumn(), console=console) as progress:
                task = progress.add_task("Letters", total=len(letter_links))

                for idx, (letter, href) in enumerate(sorted(letter_links.items())):
                    try:
                        await page.goto(href, wait_until="domcontentloaded", timeout=20000)
                        page_html = await wait_for_real_page(page, timeout=30)

                        if page_html:
                            page_records = await scrape_directory_listing(page)
                            if page_records:
                                for r in page_records:
                                    r["letter"] = letter
                                records.extend(page_records)

                            count = len(page_records) if page_records else 0
                            progress.update(task, completed=idx+1,
                                            description=f"[green]{letter}[/green] — {count} builders")
                        else:
                            progress.update(task, completed=idx+1,
                                            description=f"[red]{letter}[/red] — blocked/timeout")
                    except Exception:
                        progress.update(task, completed=idx+1,
                                        description=f"[red]{letter}[/red] — error")

        if not records:
            console.print("[bold red]No data extracted.[/bold red]")
            return

        # Deduplicate by company name
        seen = set()
        unique = []
        for r in records:
            key = r.get("company", "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(r)
        records = unique

        # Step 7: Save
        console.print(f"\n[bold yellow]Step 4:[/bold yellow] Saving {len(records)} records...")
        csv_path, xlsx_path = save_results(records)
        console.print(f"[bold green]CSV:[/bold green]   [cyan]{csv_path}[/cyan]")
        console.print(f"[bold green]Excel:[/bold green] [cyan]{xlsx_path}[/cyan]\n")

        # Summary table
        tbl = Table(title="Extraction Summary", box=box.ROUNDED, show_lines=True)
        tbl.add_column("#", style="dim", width=5)
        tbl.add_column("Company", style="bold", max_width=35)
        tbl.add_column("Email", max_width=30)
        tbl.add_column("Website", max_width=30)
        tbl.add_column("State", max_width=5)

        for i, r in enumerate(records[:25], 1):
            tbl.add_row(str(i),
                        (r.get("company","") or "-")[:35],
                        r.get("email","") or "[dim]-[/dim]",
                        (r.get("website","") or "-")[:30],
                        r.get("state","") or "[dim]-[/dim]")

        if len(records) > 25:
            tbl.add_row("...", f"[dim]+{len(records)-25} more[/dim]", "", "", "")

        console.print(tbl)

        with_email = sum(1 for r in records if r.get("email"))
        with_web = sum(1 for r in records if r.get("website"))
        console.print(f"\n[bold]Stats:[/bold]")
        console.print(f"  Total:      [cyan]{len(records)}[/cyan]")
        console.print(f"  With email: [green]{with_email}[/green] ({with_email*100//max(len(records),1)}%)")
        console.print(f"  With web:   [green]{with_web}[/green] ({with_web*100//max(len(records),1)}%)")
        console.print()

    finally:
        await pw.stop()
        try:
            chrome_proc.terminate()
        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="HM Smart Scraper — Uses your real Chrome browser")
    parser.add_argument("url", help="Directory URL to scrape")
    parser.add_argument("--no-details", action="store_true", help="Skip visiting detail pages")
    args = parser.parse_args()
    asyncio.run(run(args.url, visit_details=not args.no_details))


if __name__ == "__main__":
    main()
