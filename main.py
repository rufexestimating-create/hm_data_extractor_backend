"""
HM Data Extractor — Main entry point.

Usage:
    python main.py <directory_url> [--browser] [--output filename] [--max N]

Examples:
    python main.py "https://www.yelp.com/search?find_desc=restaurants&find_loc=New+York"
    python main.py "https://www.yellowpages.com/los-angeles-ca/plumbers" --browser
    python main.py "https://example-directory.com/listings" --output my_data --max 20
"""

import argparse
import asyncio
import signal
import sys
import os
import time

def _suppress_playwright_cleanup_errors(unraisable):
    msg = str(unraisable.exc_value) if unraisable.exc_value else ""
    if "closed pipe" in msg or "unclosed transport" in str(unraisable.object):
        return
    sys.__unraisablehook__(unraisable)

sys.unraisablehook = _suppress_playwright_cleanup_errors

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.panel import Panel
from rich import box

import config
from browser import PageFetcher
from directory_parser import find_listing_links, find_next_page
import smart_directory as sd
from extractor import extract_all
from exporter import save_csv, save_excel, IncrementalCSVWriter
from website_crawler import enrich_records_with_websites


console = Console()


def print_banner():
    banner = """
    ╦ ╦╔╦╗  ╔╦╗╔═╗╔╦╗╔═╗  ╔═╗═╗ ╦╔╦╗╦═╗╔═╗╔═╗╔╦╗╔═╗╦═╗
    ╠═╣║║║   ║║╠═╣ ║ ╠═╣  ║╣ ╔╩╦╝ ║ ╠╦╝╠═╣║   ║ ║ ║╠╦╝
    ╩ ╩╩ ╩  ═╩╝╩ ╩ ╩ ╩ ╩  ╚═╝╩ ╚═ ╩ ╩╚═╩ ╩╚═╝ ╩ ╚═╝╩╚═
    """
    console.print(Panel(banner, title="[bold blue]HM Design Group[/bold blue]",
                        subtitle="Directory Data Extractor", style="blue"))


async def run(url: str, use_browser: bool, output_name: str, max_listings: int | None, fast: bool = False, max_pages: int | None = None):
    """Main extraction pipeline with full multi-page pagination."""

    print_banner()
    console.print(f"\n[bold cyan]Target URL:[/bold cyan] {url}")
    if fast:
        console.print(f"[bold green]FAST MODE:[/bold green] extracting only name, website, email, state\n")
    else:
        console.print()

    # Step 1: Discover all companies across ALL pages (1 to end)
    console.print("[bold yellow]Step 1:[/bold yellow] Crawling directory pages (1 to end)...")

    cards: list[dict] = []            # one per company: name, profile_url, website (if on the list)
    seen_keys = set()
    seen_pages = set()
    chrome: set = set()               # outside sites on every page (sponsors, footer)
    current_page_url = url
    page_num = 1
    is_houzz = "houzz.com" in url

    results: list[dict] = []
    errors: list[str] = []
    csv_writer = None

    def _save_and_exit(sig, frame):
        """Ctrl+C: save whatever data we have collected so far."""
        if csv_writer:
            csv_writer.close()
        if results:
            console.print(f"\n\n[bold yellow]⚠ Stopped![/bold yellow] Saving {len(results)} records...")
            csv_path = save_csv(results, output_name)
            xlsx_path = save_excel(results, output_name)
            console.print(f"[bold green]✓[/bold green] CSV saved:   [cyan]{csv_path}[/cyan]")
            console.print(f"[bold green]✓[/bold green] Excel saved: [cyan]{xlsx_path}[/cyan]")
        else:
            console.print("\n[dim]No data collected yet.[/dim]")
        sys.exit(0)

    prev_handler = signal.signal(signal.SIGINT, _save_and_exit)

    async with PageFetcher(use_browser=use_browser) as fetcher:
        while current_page_url and current_page_url not in seen_pages:
            seen_pages.add(current_page_url)
            console.print(f"  [cyan]Loading Page {page_num}:[/cyan] {current_page_url}")

            dir_html = await fetcher.fetch(current_page_url)
            if not dir_html and not use_browser:
                console.print(f"  [yellow]Page {page_num} blocked — retrying with browser...[/yellow]")
                dir_html = await fetcher.fetch_browser(current_page_url)

            if not dir_html:
                console.print(f"  [red]Failed to load Page {page_num}. Ending pagination.[/red]")
                break

            if is_houzz:
                page_cards = [{"name": "", "profile_url": l, "website": ""}
                              for l in find_listing_links(dir_html, current_page_url)]
            else:
                res = sd.analyze_list(dir_html, current_page_url, chrome)
                if not res["records"] and not use_browser:
                    bhtml = await fetcher.fetch_browser(current_page_url)     # JavaScript list
                    if bhtml:
                        res2 = sd.analyze_list(bhtml, current_page_url, chrome)
                        if res2["records"]:
                            res, dir_html = res2, bhtml
                chrome |= res["chrome_hosts"]
                page_cards = res["records"]

            added_this_page = 0
            for c in page_cards:
                key = c["profile_url"] or ("name:" + (c.get("name") or "").lower())
                if key not in seen_keys:
                    seen_keys.add(key)
                    cards.append(c)
                    added_this_page += 1

            console.print(f"  [green]✓ Page {page_num}:[/green] Found {added_this_page} new companies (Total collected: {len(cards)})")

            # If no listings were found on a subsequent page, stop
            if not added_this_page and page_num > 1:
                console.print(f"  [dim]No new listings found on Page {page_num}. Ending pagination.[/dim]")
                break

            # If max_listings reached
            if max_listings and len(cards) >= max_listings:
                cards = cards[:max_listings]
                console.print(f"  [green]✓ Reached max listings limit ({max_listings}).[/green]")
                break

            # If max_pages reached
            if max_pages and page_num >= max_pages:
                console.print(f"  [green]✓ Reached max pages limit ({max_pages}).[/green]")
                break

            # Look for next page
            if is_houzz:
                next_url = find_next_page(dir_html, current_page_url, listings_count=added_this_page)
            else:
                next_url = sd.find_next_page(dir_html, current_page_url, added_this_page)[0]
            if next_url and next_url not in seen_pages:
                current_page_url = next_url
                page_num += 1
                await asyncio.sleep(config.DELAY_BETWEEN_REQUESTS)
            else:
                console.print(f"  [bold green]✓ Reached the last page (Page {page_num}).[/bold green]")
                break

        if not cards:
            console.print("[bold red]✗ No companies found (paste the page that LISTS the companies).[/bold red]")
            sys.exit(1)

        console.print(f"\n[bold green]✓[/bold green] Found [cyan]{len(cards)}[/cyan] companies across [cyan]{page_num}[/cyan] page(s).\n")

        # Create incremental CSV — each record is saved to disk as it arrives
        csv_writer = IncrementalCSVWriter(output_name, fast=fast)

        # Fast Mode: a website shown on the list page is final - no need to open that company
        by_url = {c["profile_url"]: c for c in cards if c["profile_url"]}
        for c in cards:
            if (fast and c["website"]) or not c["profile_url"]:
                rec = {"name": c["name"], "page_url": c["profile_url"] or url, "website": c["website"],
                       "emails": list(c.get("emails") or []), "state": c.get("state", ""), "license": "",
                       "website_source": "listing page" if c["website"] else ""}
                if fast:
                    rec["phone"] = c.get("phone", "")
                else:
                    rec.update({"phones": [c["phone"]] if c.get("phone") else [], "address": "", "socials": {}})
                results.append(rec)
                csv_writer.append(rec)
        links = [c["profile_url"] for c in cards if c["profile_url"] and not (fast and c["website"])]

        # Step 2: Visit company pages — concurrent!
        console.print(f"[bold yellow]Step 2:[/bold yellow] Opening {len(links)} company pages — "
                      f"[cyan]{config.CONCURRENT_WORKERS} at a time[/cyan] ⚡\n")
        done_count = 0
        total = len(links)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing listings", total=total)

            def on_fetched(index, link_url, html):
                nonlocal done_count
                done_count += 1
                if html:
                    card = by_url.get(link_url, {})
                    data = extract_all(html, link_url, fast=fast, name_hint=card.get("name", ""), chrome_hosts=chrome)
                    src = "company page" if data.get("website") else ""
                    if not data.get("website") and card.get("website"):
                        data["website"], src = card["website"], "listing page"
                    if data.get("website") and not sd.valid_website(data["website"]):
                        data["website"], src = "", ""
                    data["website_source"] = src
                    results.append(data)
                    csv_writer.append(data)
                else:
                    errors.append(link_url)
                progress.update(task, completed=done_count,
                                description=f"[cyan]({done_count}/{total})[/cyan] ⚡ {config.CONCURRENT_WORKERS} parallel")

            if links:
                await fetcher.fetch_many(links, callback=on_fetched)

    # Step 3.5: Deep Crawl Contractor Websites (10 parallel workers) - not in Fast Mode
    has_websites = [r for r in results if r.get("website")]
    if has_websites and not fast:
        console.print(f"\n[bold yellow]Step 3.5:[/bold yellow] Deep-crawling [cyan]{len(has_websites)} contractor websites[/cyan] for direct emails (10 parallel) ⚡\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            crawl_task = progress.add_task("Crawling contractor websites", total=len(results))

            def on_website_done(done, total_records, rec, found_emails):
                emails_count = len(found_emails)
                name = rec.get("name") or "Contractor"
                desc = f"[cyan]({done}/{total_records})[/cyan] {name[:25]}"
                if emails_count:
                    desc += f" -> [green]✉ {emails_count} email(s)[/green]"
                progress.update(crawl_task, completed=done, description=desc)

            results = await enrich_records_with_websites(results, concurrency=10, progress_callback=on_website_done)

    # Close incremental writer and restore default Ctrl+C before final save
    if csv_writer:
        csv_writer.close()
    signal.signal(signal.SIGINT, prev_handler)

    # Step 4: Export results (final version — includes enriched emails from Step 3.5)
    console.print(f"\n[bold yellow]Step 4:[/bold yellow] Saving results...\n")

    csv_path = save_csv(results, output_name)
    xlsx_path = save_excel(results, output_name)

    console.print(f"[bold green]✓[/bold green] CSV saved:   [cyan]{csv_path}[/cyan]")
    console.print(f"[bold green]✓[/bold green] Excel saved: [cyan]{xlsx_path}[/cyan]\n")

    # Step 5: Show summary
    _print_summary(results, errors)

    return results


def _print_summary(results: list[dict], errors: list[str]):
    """Print a nice summary table of what was extracted."""
    table = Table(title="Extraction Summary", box=box.ROUNDED, show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name", style="bold", max_width=25)
    table.add_column("Website", max_width=25)
    table.add_column("Email", style="green", max_width=28)
    table.add_column("Phone", max_width=18)
    table.add_column("Address", max_width=25)
    table.add_column("License", max_width=15)

    for i, rec in enumerate(results, 1):
        phones = rec.get("phones", [])
        if isinstance(phones, str):
            phone_str = phones
        else:
            phone_str = phones[0] if phones else ""

        emails = rec.get("emails", [])

        table.add_row(
            str(i),
            rec.get("name") or "[dim]—[/dim]",
            rec.get("website") or "[dim]—[/dim]",
            emails[0] if emails else "[dim]—[/dim]",
            phone_str or "[dim]—[/dim]",
            (rec.get("address") or "—")[:25],
            rec.get("license") or "[dim]—[/dim]",
        )

    console.print(table)

    # Stats
    total = len(results)
    with_website = sum(1 for r in results if r.get("website"))
    with_email = sum(1 for r in results if r.get("emails"))
    with_phone = sum(1 for r in results if r.get("phones") or r.get("phone"))
    with_license = sum(1 for r in results if r.get("license"))

    console.print(f"\n[bold]Stats:[/bold]")
    console.print(f"  Total profiles:   [cyan]{total}[/cyan]")
    console.print(f"  With website:     [green]{with_website}[/green] ({with_website/total*100:.0f}%)" if total else "")
    console.print(f"  With email:       [green]{with_email}[/green] ({with_email/total*100:.0f}%)" if total else "")
    console.print(f"  With phone:       [green]{with_phone}[/green] ({with_phone/total*100:.0f}%)" if total else "")
    if with_license:
        console.print(f"  With license:     [green]{with_license}[/green] ({with_license/total*100:.0f}%)")

    if errors:
        console.print(f"  [red]Failed pages:     {len(errors)}[/red]")
        for err_url in errors:
            console.print(f"    [dim]• {err_url}[/dim]")

    console.print()


def main():
    parser = argparse.ArgumentParser(
        description="HM Data Extractor — Scrape business data from directory pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py "https://www.yelp.com/search?find_desc=restaurants&find_loc=New+York"
  python main.py "https://www.yellowpages.com/los-angeles-ca/plumbers" --browser
  python main.py "https://example.com/directory" --output my_data --max 10
        """,
    )
    parser.add_argument("url", help="URL of the directory page to scrape")
    parser.add_argument(
        "--browser", action="store_true",
        help=r"Use a real browser (Playwright) — slower but handles JavaScript-heavy sites",
    )
    parser.add_argument(
        "--output", "-o", default=config.DEFAULT_FILENAME,
        help=f"Output filename (without extension). Default: {config.DEFAULT_FILENAME}",
    )
    parser.add_argument(
        "--max", "-m", type=int, default=None,
        help="Max number of listings to process (default: all)",
    )
    parser.add_argument(
        "--max-pages", "-p", type=int, default=None,
        help="Max number of directory pages to scrape (default: all pages 1 to end)",
    )
    parser.add_argument(
        "--fast", "-f", action="store_true",
        help="Fast mode: extract only name, website, email, state (skips phones, address, socials)",
    )

    args = parser.parse_args()

    asyncio.run(run(args.url, args.browser, args.output, args.max, args.fast, args.max_pages))


if __name__ == "__main__":
    main()
