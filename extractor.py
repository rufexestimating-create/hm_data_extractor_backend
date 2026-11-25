"""
Core extraction engine — visits a single business profile page and pulls out
business name, phone, email, address, license number, website, and social links.
Specifically optimized for Houzz, YellowPages, Yelp, and generic directories.
"""

import html
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import config
import smart_directory
from website_crawler import decode_cloudflare_email, clean_emails

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
}

SKIP_DOMAINS = (
    "houzz.com", "yelp.com", "yellowpages.com", "google.com", "apple.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "pinterest.com", "tiktok.com", "w3.org", "schema.org"
)


def extract_emails(soup: BeautifulSoup, text: str) -> list[str]:
    """Find emails in mailto links, Cloudflare obfuscation, and text."""
    emails = []

    # 1. From mailto links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            emails.append(email)

    # 2. Cloudflare tags
    for cf in soup.find_all(attrs={"data-cfemail": True}):
        dec = decode_cloudflare_email(cf["data-cfemail"])
        if dec:
            emails.append(dec)

    # 3. Text regex
    emails.extend(config.EMAIL_PATTERN.findall(text))

    return clean_emails(emails)


def extract_phones(soup: BeautifulSoup, text: str) -> list[str]:
    """Find phone numbers in page text and tel: links."""
    phones = set()

    # tel: links are most reliable
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            raw = href[4:].split("?")[0].strip()
            cleaned = re.sub(r'[^\d+]', '', raw)
            if 10 <= len(cleaned.replace('+', '')) <= 15:
                phones.add(raw)

    # Page text regex
    for pattern in config.PHONE_PATTERNS:
        for match in pattern.findall(text):
            cleaned = re.sub(r'[^\d+]', '', match)
            if 10 <= len(cleaned.replace('+', '')) <= 15:
                phones.add(match.strip())

    return sorted(phones)


def extract_social_links(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """Find social media profile links (never the directory's own, e.g. facebook.com/houzz)."""
    socials: dict[str, str] = {}
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    dir_name = host.split(".")[0] if host else ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if dir_name and re.search(r"[/@]%s(?:[/?#.]|$)" % re.escape(dir_name), href.lower()):
            continue
        # Site footer = the directory's own socials, not the business's
        if a.find_parent(lambda t: t.name == "footer" or any(
                "footer" in c.lower() for c in (t.get("class") or []))):
            continue
        for platform, pattern in config.SOCIAL_PATTERNS.items():
            if platform not in socials and pattern.search(href):
                socials[platform] = href
                break
    return socials


def parse_labeled_details(soup: BeautifulSoup) -> dict:
    """
    Extract structured fields from label/value pairs commonly found in 
    directories (such as Houzz 'Business Details' block).
    """
    details = {}
    text_lines = [line.strip() for line in soup.get_text("\n").split("\n") if line.strip()]

    for i, line in enumerate(text_lines):
        norm = line.lower().rstrip(":")
        if norm in ("business name", "company name") and i + 1 < len(text_lines):
            val = text_lines[i + 1]
            if not val.endswith(":") and len(val) > 1:
                details["name"] = val

        elif norm in ("phone number", "phone", "telephone") and i + 1 < len(text_lines):
            val = text_lines[i + 1]
            if not val.endswith(":") and any(c.isdigit() for c in val):
                details["phone"] = val

        elif norm in ("website", "web site") and i + 1 < len(text_lines):
            val = text_lines[i + 1]
            if not val.endswith(":") and ("." in val or "http" in val):
                details["website"] = val

        elif norm in ("address", "location") and i + 1 < len(text_lines):
            val = text_lines[i + 1]
            if not val.endswith(":") and len(val) > 5:
                details["address"] = val

        elif norm in ("license number", "license #", "license", "lic #") and i + 1 < len(text_lines):
            val = text_lines[i + 1]
            if not val.endswith(":") and len(val) > 2:
                details["license"] = val

    return details


def parse_json_ld(soup: BeautifulSoup) -> dict:
    """Extract business info from Schema.org JSON-LD scripts."""
    result = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            content = script.string or script.get_text()
            if not content:
                continue
            data = json.loads(content)
            items = data if isinstance(data, list) else [data]

            for item in items:
                if not isinstance(item, dict):
                    continue

                # Type check
                item_type = item.get("@type", "")
                if any(k in str(item_type) for k in ("Business", "Organization", "Contractor", "LocalBusiness", "ProfessionalService")):
                    if item.get("name") and not result.get("name"):
                        result["name"] = item["name"]

                    if item.get("telephone") and not result.get("phone"):
                        result["phone"] = item["telephone"]

                    # External website URL
                    url_val = item.get("url") or item.get("sameAs")
                    if isinstance(url_val, str) and not result.get("website"):
                        parsed = urlparse(url_val)
                        if parsed.netloc and not any(skip in parsed.netloc.lower() for skip in SKIP_DOMAINS):
                            result["website"] = url_val

                    # Address
                    addr = item.get("address")
                    if isinstance(addr, dict) and not result.get("address"):
                        parts = [
                            addr.get("streetAddress", ""),
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                            addr.get("postalCode", ""),
                        ]
                        full = ", ".join(p for p in parts if p)
                        if full:
                            result["address"] = full
                            if addr.get("addressRegion") in US_STATES:
                                result["state"] = addr.get("addressRegion")
                    elif isinstance(addr, str) and addr and not result.get("address"):
                        result["address"] = addr

                    if item.get("email") and not result.get("email"):
                        result["email"] = item["email"]
        except Exception:
            continue
    return result


def extract_business_name(soup: BeautifulSoup, labeled_data: dict, json_ld: dict) -> str:
    """Accurately determine the real business name (never the directory site name)."""
    # 1. From labeled 'Business Name' (e.g. Houzz Business Details)
    if labeled_data.get("name"):
        return labeled_data["name"]

    # 2. From Schema.org JSON-LD (often HTML-escaped, e.g. 'Kitchen &amp; Bath')
    if json_ld.get("name"):
        return html.unescape(json_ld["name"]).strip()

    # 3. Main <h1> heading on the listing page
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        name = h1.get_text(strip=True)
        # Avoid generic titles
        if len(name) > 2 and "houzz" not in name.lower() and "best 15" not in name.lower():
            return name

    # 4. <meta property="og:title">
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        content = og_title["content"].strip()
        parts = re.split(r'\s*[|–—·•-]\s*', content)
        candidate = parts[0].strip()
        if candidate and "houzz" not in candidate.lower():
            return candidate

    # 5. <title> tag
    if soup.title and soup.title.string:
        parts = re.split(r'\s*[|–—·•]\s*', soup.title.string)
        candidate = parts[0].strip()
        if candidate and "houzz" not in candidate.lower():
            return candidate

    return ""


def extract_website_url(soup: BeautifulSoup, page_url: str, labeled_data: dict, json_ld: dict) -> str:
    """Find the contractor's real external website (never the directory URL)."""
    # 1. Labeled website (e.g. 'Website: www.example.com')
    if labeled_data.get("website"):
        web = labeled_data["website"].strip()
        if not web.startswith("http://") and not web.startswith("https://"):
            web = "https://" + web
        return web

    # 2. Schema.org JSON-LD url
    if json_ld.get("website"):
        return json_ld["website"]

    current_host = urlparse(page_url).netloc.lower()

    # 3. Explicit "Website" button/link (e.g. 'Website ↗') — beats sponsor banners
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        label = a.get_text(" ", strip=True).lower()
        if href.startswith("http") and re.match(r"^(visit\s+)?(our\s+)?web\s?site\b", label):
            host = urlparse(href).netloc.lower()
            if host and host != current_host and not any(skip in host for skip in SKIP_DOMAINS):
                return href

    # 4. Look for outbound links pointing away from directory

    # Check for direct anchor with external URL or domain-like text
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True).lower()

        # Some sites put the URL directly in text (e.g. 'www.company.com')
        if text.startswith("www.") and "." in text:
            return "https://" + text

        if href.startswith("http"):
            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if host and host != current_host and not any(skip in host for skip in SKIP_DOMAINS):
                # Exclude share links, maps, tracking
                if not any(k in href.lower() for k in ("share", "intent", "login", "signup")):
                    return href

    return ""


def extract_address(soup: BeautifulSoup, labeled_data: dict, json_ld: dict) -> str:
    """Extract street address."""
    if labeled_data.get("address"):
        return labeled_data["address"]
    if json_ld.get("address"):
        return json_ld["address"]

    addr_el = soup.find(attrs={"itemprop": "address"})
    if addr_el:
        return " ".join(addr_el.get_text(separator=" ").split())

    for cls in ["address", "addr", "street-address", "location", "adr"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            txt = " ".join(el.get_text(separator=" ").split())
            if len(txt) > 5:
                return txt
    return ""


def extract_state_from_address(address_str: str) -> str:
    """Extract US state code from address string."""
    if not address_str:
        return ""
    m = re.search(r'\b([A-Z]{2})\b(?:\s+\d{5})?', address_str)
    if m and m.group(1) in US_STATES:
        return m.group(1)
    return ""


def extract_houzz_profile(html: str, page_url: str) -> dict | None:
    """
    Houzz profile: read the pro's own JSON block (matched by the URL slug, so a
    'similar pros' block can never leak in). 'rawDomain' is exactly the
    Website shown under Business Details. Returns None if not found.
    """
    m = re.search(r"/([^/]+)-pfvwus-pf~", page_url)
    if not m:
        return None
    hit = re.search(r'"titleSlug":"%s"' % re.escape(m.group(1)), html)
    if not hit:
        return None

    back = html[max(0, hit.start() - 2500):hit.start()]
    fwd = html[hit.end():hit.end() + 6000]
    cut = re.search(r'"titleSlug":|"user":\{', fwd)  # stop before the next pro's data
    if cut:
        fwd = fwd[:cut.start()]

    def _s(pattern, text):
        found = re.findall(pattern, text)
        return json.loads(f'"{found[-1]}"') if found else ""

    website = _s(r'"rawDomain":"((?:[^"\\]|\\.)*)"', fwd)
    if website and not website.startswith(("http://", "https://")):
        website = "http://" + website
    location = _s(r'"location":"((?:[^"\\]|\\.)*)"', fwd)
    parts = [p.strip() for p in location.split(",")]
    state = parts[1] if len(parts) >= 2 else ""
    if len(state) > 2 and state.title() in US_STATE_NAMES:
        state = US_STATE_NAMES[state.title()]

    return {
        "name": _s(r'"displayName":"((?:[^"\\]|\\.)*)"', back),
        "website": website,
        "phone": _s(r'"formattedPhone":"((?:[^"\\]|\\.)*)"', fwd),
        "location": location,
        "state": state,
    }


US_STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "District Of Columbia": "DC",
}


def extract_constructionusa_profile(html: str) -> dict:
    """
    theconstructionusa.com company page: read the 'Contact information' box
    (Website / Email / Phone / Address rows). Website = the exact link under the
    'Website' label, or '' when the company has none.
    """
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    out = {"name": h1.get_text(" ", strip=True) if h1 else "",
           "website": "", "email": "", "phone": "", "address": "", "state": ""}
    flag = soup.select_one("img.lc-drapeau")
    if flag and flag.parent:
        m = re.search(r",\s*([A-Z]{2})\b", flag.parent.get_text(" ", strip=True))
        if m and m.group(1) in US_STATES:
            out["state"] = m.group(1)
    for label in soup.select(".lc-coord__label"):
        key = label.get_text(" ", strip=True).lower()
        body = label.parent
        a = body.find("a", href=True)
        if key == "website" and a and a["href"].strip().startswith("http"):
            out["website"] = a["href"].strip()
        elif key == "email" and a:
            out["email"] = a["href"].removeprefix("mailto:").split("?")[0].strip() or a.get_text(strip=True)
        elif key == "phone":
            out["phone"] = (a or body).get_text(" ", strip=True).removeprefix("Phone").strip()
        elif key == "address":
            out["address"] = " ".join(body.get_text(" ", strip=True).split()[1:])
    return out


def extract_all(html: str, page_url: str, fast: bool = False, name_hint: str = "",
                chrome_hosts: set | None = None) -> dict:
    """
    Given raw HTML and its URL, extract all available business data.
    name_hint    = company name already read from the directory's list page.
    chrome_hosts = outside sites that appear on the directory's every page (sponsors,
                   footer) - never the company's website.
    The website comes from smart_directory's scoring engine (Website label, domain-like
    text, name match, JSON-LD ...), never from 'the first outside link'.
    """
    if "theconstructionusa.com/company/" in page_url:
        c = extract_constructionusa_profile(html)
        if c["name"]:
            rec = {
                "name": c["name"],
                "page_url": page_url,
                "website": c["website"],
                "emails": [c["email"]] if c["email"] else [],
                "state": c["state"] or extract_state_from_address(c["address"]),
                "phone": c["phone"],
                "license": "",
            }
            if not fast:
                rec["phones"] = [c["phone"]] if c["phone"] else []
                rec["address"] = c["address"]
                rec.pop("phone")
            return rec

    houzz = extract_houzz_profile(html, page_url) if "houzz.com" in page_url else None
    if houzz and fast:
        # Fast path: no full HTML parse needed
        return {
            "name": houzz["name"],
            "page_url": page_url,
            "website": houzz["website"],
            "emails": [],
            "state": houzz["state"],
            "phone": houzz["phone"],
            "license": "",
        }

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ")

    labeled_data = parse_labeled_details(soup)
    json_ld = parse_json_ld(soup)

    name = name_hint or extract_business_name(soup, labeled_data, json_ld)
    smart = {}
    if soup.select_one('[class*="geodir-field-"]'):
        # GeoDirectory profile: the "Website" field is exact; no field = no website
        a = soup.select_one(".geodir-field-website a[href]")
        website = a["href"].strip() if a and a["href"].strip().startswith("http") else ""
    elif houzz:
        website = houzz["website"]
    else:
        smart = smart_directory.extract_profile(html, page_url, name, chrome_hosts)
        website = smart["website"]
        if not name:
            name = smart["name"]
    address = extract_address(soup, labeled_data, json_ld)
    if houzz:
        # Houzz's own data is exact — never fall back to a random outbound link
        name = houzz["name"] or name
        website = houzz["website"]
        if houzz["phone"]:
            labeled_data["phone"] = houzz["phone"]
        address = address or houzz["location"]
    license_no = labeled_data.get("license", "")

    state = json_ld.get("state") or extract_state_from_address(address)

    # Phones
    phones = extract_phones(soup, text)
    if labeled_data.get("phone") and labeled_data["phone"] not in phones:
        phones.insert(0, labeled_data["phone"])
    elif json_ld.get("phone") and json_ld["phone"] not in phones:
        phones.insert(0, json_ld["phone"])
    if houzz and houzz["phone"]:
        phones = [houzz["phone"]]
        state = state or houzz["state"]

    # Emails on directory profile
    emails = extract_emails(soup, text)
    if json_ld.get("email") and json_ld["email"] not in emails:
        emails.append(json_ld["email"])

    # Drop the directory's own emails (e.g. info@nthba.com in every footer). Compare the
    # registrable domain: business.tacomachamber.org -> tacomachamber.org
    dir_domain = smart_directory.reg_domain(page_url)
    if dir_domain:
        emails = [e for e in emails if smart_directory.reg_domain(e.lower().split("@")[-1]) != dir_domain]

    # extras for the app (not exported): why this website, runner-up links, redirect button
    extra = {"_why": smart.get("why", []), "_cands": smart.get("candidates", []),
             "_internal_website": smart.get("internal_website")}

    if fast:
        return {
            "name": name,
            "page_url": page_url,
            "website": website,
            "emails": emails,
            "state": state,
            "phone": phones[0] if phones else "",
            "license": license_no,
            **extra,
        }

    return {
        "name": name,
        "page_url": page_url,
        "website": website,
        "phones": phones,
        "emails": emails,
        "address": address,
        "state": state,
        "license": license_no,
        "socials": extract_social_links(soup, page_url),
        **extra,
    }
