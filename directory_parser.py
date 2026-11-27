"""
Directory page parser — given a directory listing page, find all the
individual business / listing links to visit.

Supports generic directories plus specific parsers for popular sites.
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Site-specific parsers
# ---------------------------------------------------------------------------

def _parse_houzz(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract individual professional/contractor profile links from Houzz."""
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        # Houzz contractor profile URLs always contain 'pf~' or 'pfvwus-'
        if ("-pf~" in href or "pfvwus-" in href or "~" in href) and "/professionals/" in href:
            # Exclude category, filter, and taxonomy links
            skip_houzz = (
                "probr0-bo", "probr1-bo", "/education", "hznb", 
                "/discussions", "/edit", "/user/", "/services"
            )
            if not any(s in href.lower() for s in skip_houzz):
                clean_link = href.split("?")[0].split("#")[0]
                links.append(clean_link)
    return list(dict.fromkeys(links))


def _parse_yelp(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract business links from a Yelp search results page."""
    links = []
    for a in soup.select('a[href*="/biz/"]'):
        href = urljoin(base_url, a["href"])
        # Only full biz pages, not photos/reviews sub-paths
        path = urlparse(href).path
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "biz":
            links.append(href.split("?")[0])
    return list(dict.fromkeys(links))  # dedupe, preserve order


def _parse_yellowpages(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract business links from YellowPages."""
    links = []
    for a in soup.select('a.business-name'):
        href = urljoin(base_url, a.get("href", ""))
        if href:
            links.append(href.split("?")[0])
    return list(dict.fromkeys(links))


def _parse_constructionusa(soup: BeautifulSoup, base_url: str) -> list[str]:
    """theconstructionusa.com: every company card links to /company/<slug>."""
    host = urlparse(base_url).netloc.lower()
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("?")[0].split("#")[0]
        p = urlparse(href)
        if p.netloc.lower() == host and p.path.startswith("/company/") and len(p.path) > len("/company/"):
            links.append(href)
    return list(dict.fromkeys(links))


def _parse_geodirectory(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    WordPress GeoDirectory sites (e.g. exchange.construction): each company is a
    `.geodir-post` card inside `.geodir-loop-container`; take the card's link to
    its own profile page (skip category/tag links and '#reviews' anchors).
    """
    host = urlparse(base_url).netloc.lower()
    list_path = urlparse(base_url).path.rstrip("/")
    links = []
    for card in soup.select(".geodir-loop-container .geodir-post"):
        for a in card.find_all("a", href=True):
            href = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
            p = urlparse(href)
            path = p.path.rstrip("/")
            if (p.netloc.lower() == host and path and path != list_path
                    and not any(s in path for s in ("/category/", "/tag/", "/author/", "/page/"))):
                links.append(href)
                break
    return list(dict.fromkeys(links))


def _parse_google_maps(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract business links from a Google Maps search result — limited."""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/maps/place/" in href:
            links.append(urljoin(base_url, href))
    return list(dict.fromkeys(links))


# ---------------------------------------------------------------------------
# Generic parser
# ---------------------------------------------------------------------------

def _parse_generic(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Best-effort: collect links that look like individual listing pages.
    Heuristic: links inside repeated container elements (cards / list items)
    that point to pages on the same domain.
    """
    base_domain = urlparse(base_url).netloc

    # Strategy 1: find repeated card-like containers
    candidate_links: list[str] = []

    # Look for common listing container patterns
    containers = soup.select(
        '[class*="listing"], [class*="result"], [class*="card"], '
        '[class*="business"], [class*="item"], [class*="entry"], '
        '[class*="profile"], [class*="member"], [class*="directory"]'
    )

    if containers:
        for container in containers:
            first_link = container.find("a", href=True)
            if first_link:
                href = urljoin(base_url, first_link["href"])
                parsed = urlparse(href)
                # Same domain and not just an anchor
                if parsed.netloc == base_domain and parsed.path not in ("/", ""):
                    candidate_links.append(href.split("#")[0])

    # Strategy 2: if few containers found, look at all internal links
    if len(candidate_links) < 3:
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            parsed = urlparse(href)
            if parsed.netloc == base_domain and parsed.path not in ("/", ""):
                # Skip obvious non-listing links
                skip_patterns = (
                    "/login", "/signup", "/register", "/cart", "/checkout",
                    "/about", "/contact", "/privacy", "/terms", "/faq",
                    "/search", "/category", "/tag", ".css", ".js", ".png",
                    ".jpg", ".pdf", "#",
                )
                if not any(p in href.lower() for p in skip_patterns):
                    candidate_links.append(href.split("#")[0])

    return list(dict.fromkeys(candidate_links))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SITE_PARSERS = {
    "houzz.com": _parse_houzz,
    "yelp.com": _parse_yelp,
    "yellowpages.com": _parse_yellowpages,
    "google.com/maps": _parse_google_maps,
    "theconstructionusa.com": _parse_constructionusa,
}

# Sites whose list pages change only through JavaScript (clicking a page
# number); the URL ?p=2 is ignored by the server. Value = CSS selector of the
# page-number links, each carrying data-page="N".
CLICK_PAGINATION = {
    "theconstructionusa.com": ".lc-pagination__lien",
}


def click_pagination_selector(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return CLICK_PAGINATION.get(host)


def find_listing_links(html: str, page_url: str) -> list[str]:
    """
    Parse a directory page and return a list of individual listing URLs.
    Automatically picks the right parser based on the domain.
    """
    soup = BeautifulSoup(html, "lxml")
    domain = urlparse(page_url).netloc.lower().replace("www.", "")
    path = urlparse(page_url).path

    for site_key, parser_fn in SITE_PARSERS.items():
        if site_key in (domain + path):
            return parser_fn(soup, page_url)

    if soup.select_one(".geodir-loop-container .geodir-post"):
        return _parse_geodirectory(soup, page_url)

    return _parse_generic(soup, page_url)


HOUZZ_PAGE_SIZE = 15


def houzz_page_url(url: str, steps_ahead: int) -> str:
    """Same Houzz URL, moved `steps_ahead` pages forward (fi += 15 per page)."""
    from urllib.parse import parse_qs, urlencode, urlunparse
    p = urlparse(url)
    qs = parse_qs(p.query)
    try:
        fi = int(qs.get("fi", ["0"])[0])
    except ValueError:
        fi = 0
    qs["fi"] = [str(fi + HOUZZ_PAGE_SIZE * steps_ahead)]
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


_WEBSITE_LABEL = re.compile(r"^(visit\s+)?(our\s+)?web\s?site\b", re.I)
_STATE_RE = re.compile(r",\s*([A-Z]{2})\s+\d{5}")


def _is_contact_link(a, page_host: str) -> bool:
    href = a["href"].strip()
    low = href.lower()
    if low.startswith(("mailto:", "tel:")):
        return True
    return (href.startswith("http") and bool(_WEBSITE_LABEL.match(a.get_text(" ", strip=True)))
            and urlparse(href).netloc.lower() != page_host)


def extract_cards(html: str, page_url: str) -> list[dict]:
    """
    Fast path: read company cards straight from a directory LIST page
    (company name + its 'Website' link), without opening any profile page.

    A card = the biggest container holding exactly one heading plus a
    tel:/mailto:/'Website' link. Returns fast-mode style records, or [] when
    the page doesn't look like a card list.
    """
    soup = BeautifulSoup(html, "lxml")
    page_host = urlparse(page_url).netloc.lower()
    records: list[dict] = []
    signatures: list[tuple] = []

    for h in soup.find_all(["h2", "h3", "h4"]):
        name = h.get_text(" ", strip=True)
        if not name or len(name) < 2:
            continue

        # Smallest container around the heading that also holds a contact link,
        # and never one that swallows a second heading (= a neighbouring card)
        card = None
        node = h
        while node.parent is not None and node.parent.name not in ("body", "html"):
            node = node.parent
            if len(node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])) > 1:
                break
            if any(_is_contact_link(a, page_host) for a in node.find_all("a", href=True)):
                card = node
                break
        if card is None:
            continue

        website, emails, phone, profile = "", [], "", ""
        for a in card.find_all("a", href=True):
            href = a["href"].strip()
            label = a.get_text(" ", strip=True)
            low = href.lower()
            if low.startswith("mailto:"):
                em = href[7:].split("?")[0].strip()
                if em and em not in emails:
                    emails.append(em)
            elif low.startswith("tel:"):
                phone = phone or label or href[4:]
            elif href.startswith("http") and _WEBSITE_LABEL.match(label):
                host = urlparse(href).netloc.lower()
                if host and host != page_host and not website:
                    website = href
            elif not profile and href.startswith("http") and urlparse(href).netloc.lower() == page_host:
                profile = href

        if not (website or emails or phone):
            continue  # heading without contact info (e.g. a letter divider)

        m = _STATE_RE.search(card.get_text(" ", strip=True))
        signatures.append((card.name, tuple(sorted(card.get("class", [])))))
        records.append({
            "name": name,
            "page_url": profile or page_url,
            "website": website,
            "emails": emails,
            "state": m.group(1) if m else "",
            "phone": phone,
            "license": "",
        })

    # Keep only the repeated card type (drops footer "Get in touch" blocks etc.)
    if signatures:
        common = max(set(signatures), key=signatures.count)
        records = [r for r, s in zip(records, signatures) if s == common]

    # Only trust card mode if it looks like a real list with websites
    if len(records) < 3 or not any(r["website"] for r in records):
        return []
    return records


def find_next_page(html: str, current_url: str, listings_count: int = 0, strict: bool = False) -> str | None:
    """
    Find the URL of the next page in a directory listing (e.g. Page 1 -> 2 -> 3... to the end).
    Supports Houzz (fi=15, 30, ...), Yelp, YellowPages, and generic pagination layouts.
    strict=True skips the 'active menu item' guess (which can jump to an unrelated
    nav page such as the next menu entry).
    """
    from urllib.parse import parse_qs, urlencode, urlunparse

    # Houzz: keep the user's exact URL (location filter like ~r_4736286 lives in
    # the path) and just step fi=15, 30, 45... Stop when a page adds nothing new.
    if "houzz.com" in current_url:
        return houzz_page_url(current_url, 1) if listings_count > 0 else None

    soup = BeautifulSoup(html, "lxml")
    parsed_curr = urlparse(current_url)

    # 1. Standard HTML rel="next"
    for a in soup.find_all("a", href=True):
        rel = a.get("rel", [])
        rel_list = rel if isinstance(rel, list) else [rel]
        if any("next" in str(r).lower() for r in rel_list):
            next_url = urljoin(current_url, a["href"])
            if next_url != current_url:
                return next_url

    # 2. aria-label containing 'next' (e.g. aria-label="Next Page" or aria-label="Next")
    for a in soup.find_all("a", href=True):
        aria = (a.get("aria-label") or "").strip().lower()
        if "next" in aria:
            cls_str = " ".join(a.get("class", [])).lower()
            if "disabled" not in cls_str and not a.get("disabled"):
                next_url = urljoin(current_url, a["href"])
                if next_url != current_url:
                    return next_url

    # 3. Anchor text like "Next", "Next >", "Next >>", ">", "»"
    for a in soup.find_all("a", href=True):
        txt = a.get_text(strip=True).lower()
        cls_str = " ".join(a.get("class", [])).lower()
        if txt in ("next", "next >", "next >>", "next »", ">", "»") or ("next" in cls_str and "page" in cls_str):
            if "disabled" not in cls_str and not a.get("disabled"):
                next_url = urljoin(current_url, a["href"])
                if next_url != current_url:
                    return next_url

    # 4. Check active pagination item's next sibling
    active_el = None if strict else soup.select_one('.active, .current, [aria-current="page"], .hz-pagination-link--active')
    if active_el:
        parent = active_el.parent
        if parent:
            next_sibling = parent.find_next_sibling()
            if next_sibling:
                next_a = next_sibling.find("a", href=True)
                if next_a:
                    return urljoin(current_url, next_a["href"])

    # 5. Houzz-specific: detect links with higher fi= parameter
    if "houzz.com" in current_url:
        qs = parse_qs(parsed_curr.query)
        current_fi = int(qs.get("fi", [0])[0]) if "fi" in qs else 0

        candidate_fis = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "fi=" in href:
                p_qs = parse_qs(urlparse(href).query)
                if "fi" in p_qs:
                    try:
                        val = int(p_qs["fi"][0])
                        if val > current_fi:
                            candidate_fis.append((val, urljoin(current_url, href)))
                    except ValueError:
                        pass

        if candidate_fis:
            candidate_fis.sort(key=lambda x: x[0])
            return candidate_fis[0][1]

        # Houzz fallback: If current page yielded listings (e.g. >= 8), calculate next fi
        if listings_count >= 8:
            next_fi = current_fi + 15
            qs["fi"] = [str(next_fi)]
            new_query = urlencode(qs, doseq=True)
            return urlunparse(parsed_curr._replace(query=new_query))

    # 6. Generic numbered pagination (?page=2, ?p=2)
    qs = parse_qs(parsed_curr.query)
    for p_key in ("page", "p", "pg"):
        if p_key in qs:
            try:
                cur_num = int(qs[p_key][0])
                next_num = cur_num + 1
                for a in soup.find_all("a", href=True):
                    if a.get_text(strip=True) == str(next_num):
                        return urljoin(current_url, a["href"])
                if listings_count >= 8:
                    qs[p_key] = [str(next_num)]
                    new_query = urlencode(qs, doseq=True)
                    return urlunparse(parsed_curr._replace(query=new_query))
            except ValueError:
                pass

    return None
