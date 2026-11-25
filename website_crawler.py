"""
Deep Website Crawler — visits contractor's external website (and /contact page)
to extract direct business email addresses and additional contact details.
Runs asynchronously with high concurrency (e.g. 10 parallel websites).
"""

import asyncio
import re
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

import config

# Common email junk to filter out
JUNK_EMAIL_SUBSTRINGS = (
    "example.com", "domain.com", "yoursite.com", "wixpress.com",
    "sentry.io", "w3.org", "schema.org", "wordpress.org",
    "bootstrap", "cloudflare", "gravatar", "placeholder",
)
# File names that look like emails (logo@2x.png) — checked only at the END
JUNK_EMAIL_ENDINGS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".css", ".js",
)

CONTACT_PATH_KEYWORDS = (
    "contact", "about", "reach-us", "get-in-touch", "connect", "location"
)


def decode_cloudflare_email(cf_hex: str) -> str:
    """Decode Cloudflare hex-obfuscated email address."""
    try:
        k = int(cf_hex[:2], 16)
        decoded = "".join(chr(int(cf_hex[i:i+2], 16) ^ k) for i in range(2, len(cf_hex), 2))
        return decoded.strip()
    except Exception:
        return ""


def clean_emails(raw_emails: list[str]) -> list[str]:
    """Deduplicate and clean email list, filtering out assets and generic template emails."""
    valid = []
    seen = set()
    for e in raw_emails:
        e = e.strip().lower().rstrip(".")
        if not config.EMAIL_PATTERN.fullmatch(e):
            continue
        if any(junk in e for junk in JUNK_EMAIL_SUBSTRINGS) or e.endswith(JUNK_EMAIL_ENDINGS):
            continue
        if e not in seen:
            seen.add(e)
            valid.append(e)
    return valid


def parse_page_emails(soup: BeautifulSoup, html: str) -> list[str]:
    """Extract emails from mailto links, Cloudflare obfuscation, and regex in text/HTML."""
    emails = []

    # 1. mailto: links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            raw = href[7:].split("?")[0].strip()
            emails.append(raw)

    # 2. Cloudflare email protection tags
    for cf in soup.find_all(attrs={"data-cfemail": True}):
        dec = decode_cloudflare_email(cf["data-cfemail"])
        if dec:
            emails.append(dec)

    for cf in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        hex_str = cf["href"].split("#")[-1]
        dec = decode_cloudflare_email(hex_str)
        if dec:
            emails.append(dec)

    # 3. Regex on visible page text
    text = soup.get_text(separator=" ")
    emails.extend(config.EMAIL_PATTERN.findall(text))

    return clean_emails(emails)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def find_contact_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Find the website's own Contact page link(s), best first:
    real 'Contact' links beat 'Get in touch', which beat 'About'.
    Links to another domain count only if they are clearly the same business
    (e.g. www.site.com -> site.com).
    """
    base_host = _host(base_url)
    scored: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full_url = urljoin(base_url, href).split("#")[0]
        if _host(full_url) != base_host:
            continue
        path = urlparse(full_url).path.lower()
        text = a.get_text(" ", strip=True).lower()

        score = 0
        if "contact" in path or "contact" in text:
            score = 3
        elif any(k in path or k in text.replace(" ", "-") for k in ("get-in-touch", "reach-us", "connect")):
            score = 2
        elif "about" in path or "about" in text:
            score = 1
        if score and full_url.rstrip("/") != base_url.rstrip("/"):
            scored[full_url] = max(score, scored.get(full_url, 0))

    return sorted(scored, key=lambda u: -scored[u])[:3]


def rank_emails(emails: list[str], website: str) -> list[str]:
    """Emails on the business's own domain first; drop website-builder/system emails."""
    site = _host(website)
    platform = ("wix.com", "squarespace.com", "godaddy.com", "weebly.com", "sentry",
                "duda.co", "mysite.com", "email.com", "test.com")
    keep = [e for e in emails if not any(p in e.split("@")[-1] for p in platform)]
    own = [e for e in keep if site and e.split("@")[-1].removeprefix("www.").endswith(site)]
    return own + [e for e in keep if e not in own]


_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _get(session: aiohttp.ClientSession, url: str, browser_fetch=None) -> tuple[str, str, bool]:
    """
    GET a page -> (final_url, html, blocked). blocked=True means the site is up
    but refuses automatic visitors (bot check). Retries network/DNS hiccups, then falls back
    to the real browser when the site blocks plain HTTP (bot check).
    """
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[8:])
    elif url.startswith("http://"):
        candidates.append("https://" + url[7:])

    blocked = False
    for attempt in range(2):           # 2nd round = after a pause (DNS can fail once, then work)
        for u in candidates:
            try:
                async with session.get(u, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=12), ssl=False) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="ignore")
                        if html and not _looks_blocked(html):
                            return str(resp.url), html, False
                        blocked = True   # answered with a bot check -> browser needed
                    elif resp.status in (403, 429, 503):
                        blocked = True
                    elif resp.status in (404, 410):
                        return "", "", False   # page doesn't exist
                    break                # server answered: don't try the other scheme
            except Exception:
                continue                 # DNS / connection / timeout -> try next
        if blocked:
            break
        await asyncio.sleep(2)

    if browser_fetch:
        for attempt in range(2):
            try:
                html = await browser_fetch(url)
                if html:
                    return url, html, False
            except Exception:
                pass
            if not blocked:
                break                    # site never answered at all: one browser try is enough
    return "", "", blocked


def _looks_blocked(html: str) -> bool:
    head = html[:3000].lower()
    return len(html) < 500 or any(s in head for s in (
        "just a moment", "client challenge", "checking your browser", "one moment, please",
        "attention required", "sorry, you have been blocked", "please verify you are a human"))


async def crawl_single_website(session: aiohttp.ClientSession, website_url: str, browser_fetch=None) -> dict:
    """
    Opens the business's website, finds its Contact page, and collects emails
    from the homepage + contact page.
    Returns: {"emails": [...], "contact_page": str, "status": str}
    status: "" = OK, else a short reason the site couldn't be read.
    """
    if not website_url:
        return {"emails": [], "contact_page": "", "status": "no website"}

    url = website_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    final_url, html, blocked = await _get(session, url, browser_fetch)
    if not html:
        status = "website blocks bots - open manually" if blocked else "website not opening"
        return {"emails": [], "contact_page": "", "status": status}

    soup = BeautifulSoup(html, "lxml")
    emails = parse_page_emails(soup, html)
    contact_links = find_contact_links(soup, final_url or url)
    contact_page = ""

    # Open the contact page(s): this is where the business email usually is.
    # contact_page = the best real Contact link (opened OK), even if the email
    # was found on another page.
    for c_url in contact_links:
        _, c_html, _ = await _get(session, c_url, browser_fetch)
        if not c_html:
            continue
        contact_page = contact_page or c_url
        c_emails = parse_page_emails(BeautifulSoup(c_html, "lxml"), c_html)
        if c_emails:
            emails.extend(c_emails)
            break

    return {
        "emails": rank_emails(clean_emails(emails), final_url or url),
        "contact_page": contact_page,
        "status": "",
    }


async def enrich_records_with_websites(
    records: list[dict],
    concurrency: int = 10,
    progress_callback=None,
    browser_fetch=None,
) -> list[dict]:
    """
    Takes records extracted from a directory (Houzz, etc.) and opens each
    business website (up to `concurrency` at a time) to get its Contact page
    URL and email(s). The original 'Website' link is never changed.
    browser_fetch: optional async fn(url)->html used when a site blocks plain HTTP.
    """
    sem = asyncio.Semaphore(concurrency)
    browser_sem = asyncio.Semaphore(3)   # real-browser fallback is heavier: max 3 tabs
    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=concurrency + 5, ssl=False, ttl_dns_cache=300)

    async def limited_browser(u):
        async with browser_sem:
            return await browser_fetch(u)

    total = len(records)
    completed = 0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def enrich_one(record: dict):
            nonlocal completed
            website = record.get("website")

            # If we don't have an external website, skip deep crawl
            if not website or "houzz.com" in website.lower() or "yellowpages.com" in website.lower():
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, record, [])
                return record

            async with sem:
                try:
                    res = await crawl_single_website(
                        session, website, limited_browser if browser_fetch else None)
                    if res["emails"]:
                        existing = record.get("emails", [])
                        record["emails"] = list(dict.fromkeys(res["emails"] + existing))
                    record["contact_page"] = res["contact_page"]
                    record["website_status"] = res["status"]
                except Exception:
                    record["website_status"] = "website not opening"

            completed += 1
            if progress_callback:
                progress_callback(completed, total, record, record.get("emails", []))
            return record

        tasks = [enrich_one(r) for r in records]
        return await asyncio.gather(*tasks)
