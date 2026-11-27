"""
smart_directory.py - general engine that reads ANY business directory.

No per-site code needed. It looks at the page structure the way a person does:

  LIST PAGE
    1. Find the repeated blocks (cards / rows / list items) - one block = one company.
    2. In each block: company name, its profile link (inside the directory),
       and its website link if the block already shows one.
  PROFILE PAGE
    3. Score every outside link on the page and keep the one that is really the
       company's own website: labelled "Website", text looks like a domain,
       domain matches the company name, globe icon, JSON-LD, etc.
       Social sites, maps, the directory itself, ads, header/footer links never win.
  NEXT PAGE
    4. rel=next, "Next" / "»" buttons, page numbers, ?page=N, /page/N/ ...

Every function only reads HTML; fetching is done by browser.PageFetcher.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlencode, urlunparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

_MULTI_PART_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au",
    "org.au", "edu.au", "gov.au", "co.nz", "org.nz", "net.nz", "co.za", "com.br", "com.mx",
    "com.ar", "co.in", "net.in", "org.in", "co.jp", "com.sg", "com.my", "com.ph", "com.pk",
    "com.cn", "com.hk", "com.tw", "co.kr", "com.tr", "co.il", "com.sa", "com.ng", "com.eg",
    "com.co", "com.pe", "com.ec", "com.vn", "co.id", "co.th", "com.qa", "com.kw", "ae.org",
}


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def reg_domain(host_or_url: str) -> str:
    """'members.biahawaii.org' -> 'biahawaii.org', 'www.abc.co.uk' -> 'abc.co.uk'."""
    host = host_or_url
    if "/" in host:
        host = host_of(host)
    host = (host or "").lower().split(":")[0].strip(".")
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# Never a company's own website (registrable domains).
NOT_A_WEBSITE = {
    # social / messaging
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "youtu.be", "pinterest.com", "tiktok.com", "snapchat.com", "threads.net",
    "tumblr.com", "flickr.com", "vimeo.com", "reddit.com", "nextdoor.com", "whatsapp.com",
    "wa.me", "t.me", "telegram.me", "telegram.org", "discord.gg", "discord.com", "medium.com",
    # maps
    "google.com", "goo.gl", "g.page", "g.co", "bing.com", "mapquest.com", "waze.com",
    "apple.com", "openstreetmap.org", "here.com",
    # review sites & other directories
    "yelp.com", "houzz.com", "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com",
    "bbb.org", "yellowpages.com", "superpages.com", "manta.com", "buildzoom.com", "porch.com",
    "tripadvisor.com", "glassdoor.com", "indeed.com", "crunchbase.com", "bizapedia.com",
    "opencorporates.com", "foursquare.com", "citysearch.com", "chamberofcommerce.com",
    "hotfrog.com", "merchantcircle.com", "zoominfo.com", "dnb.com", "homeguide.com",
    "trustpilot.com", "birdeye.com", "guildquality.com", "expertise.com", "bark.com",
    # directory software / vendors / services
    "growthzone.com", "growthzoneapp.com", "chambermaster.com", "micronetonline.com",
    "wliinc.com", "weblinkinternational.com", "wildapricot.com", "memberclicks.net",
    "yourmembership.com", "membershipworks.com", "glueup.com", "novi.ams", "noviams.com",
    "constantcontact.com", "list-manage.com", "mailchimp.com", "eventbrite.com", "issuu.com",
    "calendly.com", "zoom.us", "paypal.com", "stripe.com", "square.site", "squareup.com",
    # tech / cdn / trackers
    "w3.org", "schema.org", "wordpress.org", "gravatar.com", "cloudflare.com", "googleapis.com",
    "gstatic.com", "doubleclick.net", "googletagmanager.com", "google-analytics.com",
    "addthis.com", "sharethis.com", "adobe.com", "microsoft.com", "office.com", "live.com",
    "jquery.com", "bootstrapcdn.com", "fontawesome.com", "typekit.net", "cloudfront.net",
    "azureedge.net", "amazonaws.com", "akamaihd.net", "recaptcha.net", "hcaptcha.com",
    "wix.com", "squarespace.com", "godaddy.com", "weebly.com", "wordpress.com", "shopify.com",
    "bit.ly", "tinyurl.com", "ow.ly", "linktr.ee", "dropbox.com", "box.com", "github.com",
}
# Builders where a company's site lives on a sub-domain (abc.wixsite.com is a real website)
HOSTED_SITE_DOMAINS = {"wixsite.com", "wordpress.com", "weebly.com", "squarespace.com",
                       "godaddysites.com", "business.site", "webflow.io", "square.site",
                       "jimdosite.com", "webs.com", "site123.me", "carrd.co", "wix.com"}

FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net",
    "att.net", "sbcglobal.net", "verizon.net", "bellsouth.net", "cox.net", "charter.net",
    "earthlink.net", "protonmail.com", "proton.me", "gmx.com", "mail.com", "zoho.com",
    "yandex.com", "rocketmail.com", "frontier.com", "optonline.net", "windstream.net",
    "centurylink.net", "q.com", "roadrunner.com", "rr.com", "twc.com", "juno.com",
}

_FILE_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|bmp|docx?|xlsx?|pptx?|zip|rar|mp4|mp3|mov|avi|ics|vcf|css|js|ico)$", re.I)
_DOMAIN_TEXT = re.compile(r"^(https?://)?(www\d?\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)*\.[a-z]{2,24}(:\d+)?(/\S*)?$", re.I)
_WWW_IN_TEXT = re.compile(r"(?<![@\w.])((?:https?://)?www\d?\.[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s<>\"')]*)?)", re.I)
_URL_IN_TEXT = re.compile(r"(?<![@\w.])(https?://[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s<>\"')]*)?)", re.I)
_WEBSITE_WORD = re.compile(
    r"\b(web\s?-?site|web\s?page|home\s?page|visit\s+(?:our\s+|the\s+|my\s+)?(?:web\s?)?site|visit\s+web"
    r"|site\s+web|sitio\s+web|company\s+site|official\s+site|go\s+to\s+(?:web)?site|webseite|url)\b", re.I)
_WEBSITE_LABEL = re.compile(r"^\W*(web\s?-?site|web\s?-?site\s+address|web\s?page|home\s?page|website\s+url|url|web|internet|site)\s*[:\-]?\W*$", re.I)
_REDIRECT_KEYS = ("url", "u", "q", "target", "dest", "destination", "redirect", "redirect_url",
                  "redirect_uri", "redirecturl", "link", "goto", "to", "out", "href", "site",
                  "website", "r", "l", "next", "returnurl", "uddg")
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "sms:", "data:", "fax:", "callto:", "skype:", "#")
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_ADDRESS = re.compile(r"\b\d{1,6}\s+\w[\w .'-]{2,40}\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|way|ct|court|pkwy|parkway|hwy|highway|suite|ste|pl|place|cir|circle)\b|\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", re.I)

_NAV_WORDS = {
    "home", "about", "about us", "contact", "contact us", "login", "log in", "sign in", "sign up",
    "register", "join", "join now", "events", "news", "blog", "search", "advanced search",
    "directory", "members", "membership", "menu", "more", "next", "previous", "prev", "back",
    "privacy policy", "terms", "faq", "help", "careers", "jobs", "donate", "shop", "cart",
}
_GENERIC_LINK_TEXT = re.compile(
    r"^(learn more|read more|more info(rmation)?|more details|details|view( full)?( listing| profile| details| more| business| company| member)?|"
    r"see (more|details|profile)|profile|visit( (our )?(web\s?site|site))?|website|web site|view website|"
    r"go to (web)?site|contact( us| business| now)?|call( now)?|email( us)?|send (a )?message|directions|get directions|map|"
    r"view (on )?(google )?map(s)?|reviews?|write a review|\d+ reviews?|no reviews|claim( this)?( listing| business)?|"
    r"share|save|print|request (a )?quote|get (a )?quote|hire|book( now)?|open|click here|here|"
    r"view listing|listing|details »|more »|›|»|\+|logo)$", re.I)

_NAME_STOP = {
    "inc", "llc", "ltd", "co", "corp", "corporation", "company", "the", "and", "of", "a", "an",
    "l", "p", "lp", "llp", "pllc", "pc", "pa", "dba", "group", "services", "service", "usa",
    "us", "america", "american", "enterprises", "enterprise", "international", "solutions",
    "general", "contractor", "contractors", "contracting", "construction", "constructions",
    "builders", "builder", "building", "buildings", "homes", "home", "roofing", "remodeling",
    "design", "designs", "development", "restoration", "renovation", "renovations", "custom",
    "residential", "commercial", "industries", "industrial", "properties", "property", "mgmt",
    "management", "systems", "texas", "tx", "of", "at", "by", "for", "in", "on", "pros", "pro",
}


def _norm_space(s: str) -> str:
    return " ".join((s or "").split())


# ---------------------------------------------------------------------------
# URL cleaning
# ---------------------------------------------------------------------------

def _fix_scheme(href: str) -> str:
    h = href.strip().strip("'\"").replace("\\/", "/")
    h = re.sub(r"^(https?)[:;]?/{1,3}(?=[a-z0-9])", lambda m: m.group(1) + "://", h, flags=re.I) \
        if re.match(r"^https?[:;]?/{1,3}[a-z0-9]", h, re.I) and not re.match(r"^https?://", h, re.I) else h
    h = re.sub(r"^(https?):(?!//)", r"\1://", h, flags=re.I)
    return h


def unwrap_redirect(url: str, depth: int = 0) -> str:
    """'/out?url=https%3A%2F%2Fabc.com' or google '/url?q=...' -> 'https://abc.com'."""
    if depth > 2:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    qs = parse_qs(p.query)
    for k in _REDIRECT_KEYS:
        for key in (k, k.upper(), k.capitalize()):
            if key in qs:
                val = unquote(qs[key][0]).strip()
                if re.match(r"^(https?://|www\.)", val, re.I):
                    if val.lower().startswith("www."):
                        val = "http://" + val
                    return unwrap_redirect(val, depth + 1)
    # path-embedded: /redirect/https://abc.com or /go/http%3A%2F%2Fabc.com
    m = re.search(r"/(https?(?::|%3A)(?://|%2F%2F).+)$", p.path + (("?" + p.query) if p.query else ""), re.I)
    if m and m.start() > 0:
        val = unquote(m.group(1))
        return unwrap_redirect(val, depth + 1)
    return url


def clean_href(href: str, base_url: str) -> str | None:
    """Resolve an href to an absolute http(s) URL; None for mailto/tel/javascript/#."""
    if not href:
        return None
    h = href.strip()
    low = h.lower()
    if not h or low.startswith(_SKIP_SCHEMES):
        return None
    if "@" in h.split("?")[0] and not re.match(r"^https?://", h, re.I):
        return None          # 'info@abc.com' without mailto: = an e-mail, not a page
    h = _fix_scheme(h)
    low = h.lower()
    # href="www.abc.com" / "abc.com" without http -> it is NOT a page of the directory
    if not low.startswith(("http://", "https://", "/", "?", "./", "../")) and \
            re.match(r"^(www\d?\.)?[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.(com|net|org|biz|info|us|co|io|ca|uk|au|pro|build|construction|contractors|builders|homes|design|llc|services|company|group|[a-z]{2})(/|$|\?)", low):
        h = "http://" + h
    try:
        url = urljoin(base_url, h)
    except ValueError:
        return None
    if not url.lower().startswith(("http://", "https://")):
        return None
    url = unwrap_redirect(url)
    try:
        if "@" in (urlparse(url).netloc or ""):
            return None
    except ValueError:
        return None
    return url.strip()


def is_bad_site_host(host: str) -> bool:
    """True for hosts that can never be a company's own website."""
    if not host or "." not in host:
        return True
    rd = reg_domain(host)
    if rd in HOSTED_SITE_DOMAINS:
        return host.removeprefix("www.") == rd   # wix.com itself bad, abc.wixsite.com fine
    if rd in NOT_A_WEBSITE:
        return True
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return True
    return False


def valid_website(url: str) -> bool:
    """A real company website: has a proper domain ('http://' alone or facebook.com is not)."""
    url = (url or "").strip()
    if not url:
        return False
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    h = host_of(url)
    return bool(h) and bool(re.search(r"\.[a-z]{2,24}$", h)) and not is_bad_site_host(h)


def normalize_website(url: str) -> str:
    """Keep the directory's exact link, only tidy obvious junk (spaces, trailing '#')."""
    url = url.strip().rstrip("#").strip()
    return url


# ---------------------------------------------------------------------------
# Page-chrome detection (header / footer / nav / sidebar / ads)
# ---------------------------------------------------------------------------

_CHROME_TOKENS = {"footer", "header", "nav", "navbar", "navigation", "menu", "sidebar", "site-footer",
                  "site-header", "masthead", "breadcrumb", "breadcrumbs", "topbar", "top-bar",
                  "bottom-bar", "copyright", "colophon", "mega-menu", "main-menu", "main-nav",
                  "primary-menu", "footer-widgets", "site-nav", "global-nav", "utility-nav"}
_AD_RE = re.compile(r"(^|[\s_-])(ad|ads|advert|advertisement|advertising|sponsor|sponsors|sponsored|banner|banners|promo|promotion|adsbygoogle|dfp|gpt-ad|wcbanner|wcbanners)([\s_-]|$)", re.I)


def _tokens(el: Tag) -> list[str]:
    cls = el.get("class") or []
    if isinstance(cls, str):
        cls = cls.split()
    out = [c.lower() for c in cls]
    if el.get("id"):
        out.append(str(el.get("id")).lower())
    return out


def in_chrome(el: Tag) -> bool:
    """Is this element inside the site's header/footer/nav/sidebar?"""
    node = el
    inside_item = False
    while node is not None and isinstance(node, Tag) and node.name not in ("body", "html", "[document]"):
        name = node.name
        if name == "main" or (node.get("role") or "").lower() == "main":
            return False          # real page content (even if broken HTML put it inside <header>)
        if name in ("article", "li", "tr") and node is not el:
            inside_item = True
        if name in ("nav", "aside"):
            return True
        if name in ("header", "footer") and not inside_item:
            return True
        role = (node.get("role") or "").lower()
        if role in ("navigation", "banner", "contentinfo", "complementary"):
            return True
        for t in _tokens(node):
            if t in _CHROME_TOKENS or t.startswith(("footer", "site-footer", "site-header", "navbar")):
                return True
        node = node.parent
    return False


def in_ad(el: Tag) -> bool:
    node = el
    for _ in range(6):
        if node is None or not isinstance(node, Tag):
            break
        if any(_AD_RE.search(t) for t in _tokens(node)):
            return True
        node = node.parent
    rel = el.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return "sponsored" in [r.lower() for r in rel]


# ---------------------------------------------------------------------------
# Company-name <-> domain matching
# ---------------------------------------------------------------------------

def _name_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return [w for w in words if w not in _NAME_STOP and len(w) >= 3]


def name_matches_host(name: str, url_or_host: str) -> int:
    """0 = no relation, 1 = weak (generic words / initials), 2 = strong (distinctive word)."""
    if not name:
        return 0
    host = host_of(url_or_host) if "/" in url_or_host else url_or_host
    label = reg_domain(host).split(".")[0]
    if reg_domain(host) in HOSTED_SITE_DOMAINS:        # abc.wixsite.com/name
        label = host.split(".")[0] + (urlparse(url_or_host).path.replace("/", "") if "/" in url_or_host else "")
    label = label.replace("-", "").lower()
    if not label:
        return 0
    toks = _name_tokens(name)
    if any(t in label for t in toks if len(t) >= 4) or any(t == label for t in toks):
        return 2
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " "))
    joined = "".join(words)
    if len(label) >= 5 and (label in joined or joined.startswith(label[:6])):
        return 2
    initials = "".join(w[0] for w in words if w not in ("the", "and", "of"))
    if len(initials) >= 2 and label.startswith(initials[:max(2, min(len(initials), 4))]):
        return 1
    short = [w for w in words if 2 <= len(w) <= 3 and w not in ("the", "and", "inc", "llc", "co")]
    if any(label.startswith(w) for w in short):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Website scoring (shared by cards and profile pages)
# ---------------------------------------------------------------------------

def _context_label(a: Tag) -> str:
    """Text right before / around the link, e.g. 'Website:' in '<b>Website:</b> <a>..'."""
    texts = []
    own = _norm_space(a.get_text(" "))
    par = a.parent
    if isinstance(par, Tag):
        t = _norm_space(par.get_text(" "))
        if own and own in t:
            t = t.replace(own, " ", 1)
        texts.append(_norm_space(t)[:40])
        prev = par.find_previous_sibling()
        if prev is not None:
            texts.append(_norm_space(prev.get_text(" "))[:40])
        gp = par.parent
        if isinstance(gp, Tag):
            t2 = _norm_space(gp.get_text(" "))
            if own and own in t2:
                t2 = t2.replace(own, " ", 1)
            texts.append(_norm_space(t2)[:40])
            pp = gp.find_previous_sibling()
            if pp is not None:
                texts.append(_norm_space(pp.get_text(" "))[:40])
    prev = a.find_previous_sibling()
    if prev is not None:
        texts.append(_norm_space(prev.get_text(" "))[:40])
    return " | ".join(texts)


def _attr_blob(a: Tag) -> str:
    parts = [a.get("title") or "", a.get("aria-label") or "", " ".join(_tokens(a)),
             a.get("data-label") or "", a.get("itemprop") or "", a.get("data-type") or "",
             a.get("data-track") or "", a.get("data-action") or "", a.get("data-ga-label") or "",
             a.get("data-event-label") or "", a.get("data-gtm") or ""]
    for img in a.find_all(["img", "i", "span", "svg", "use"], limit=6):
        parts.append(img.get("alt") or "")
        parts.append(img.get("title") or "")
        parts.append(" ".join(_tokens(img)))
        parts.append(img.get("xlink:href") or img.get("href") or "")
    par = a.parent
    if isinstance(par, Tag):
        parts.append(" ".join(_tokens(par)))
        parts.append(par.get("itemprop") or "")
    return " ".join(p for p in parts if p).lower()


def score_website_link(a: Tag, url: str, name_hint: str = "", chrome_hosts: set | None = None,
                       dir_domain: str = "", check_chrome: bool = True) -> tuple[int, list[str]] | None:
    """
    How sure are we that `a` (pointing to `url`) is the company's own website?
    Returns (score, reasons) or None when it can never be the website.
    """
    host = host_of(url)
    if not host or is_bad_site_host(host):
        return None
    if dir_domain and reg_domain(host) == dir_domain:
        return None
    path = urlparse(url).path
    if _FILE_EXT.search(path):
        return None
    if re.search(r"(share|sharer|intent/tweet|login|signin|signup|subscribe|unsubscribe|/cdn-cgi/)", url, re.I):
        return None

    score, why = 5, []
    text = _norm_space(a.get_text(" "))
    blob = _attr_blob(a)
    if _WEBSITE_WORD.search(text):
        score += 50; why.append("text says website")
    elif _WEBSITE_WORD.search(blob) or re.search(r"(^|[\s_-])(website|web-site|homepage|url|weblink|web-link|site-link|globe|fa-globe|icon-web|icon-globe|world|earth|external-link|fa-external-link|link-ext)([\s_-]|$)", blob):
        score += 40; why.append("attribute says website")
    if text and _DOMAIN_TEXT.match(text.replace(" ", "")) and "@" not in text:
        score += 40; why.append("text is a domain")
        t_host = reg_domain(re.sub(r"^https?://", "", text.lower()).split("/")[0])
        if t_host and t_host == reg_domain(host):
            score += 10
    ctx = _context_label(a)
    if any(_WEBSITE_LABEL.match(c) for c in ctx.split(" | ") if c):
        score += 40; why.append("labelled Website")
    elif re.search(r"\b(web\s?site|homepage)\s*:", ctx, re.I):
        score += 25; why.append("near 'Website:'")
    nm = name_matches_host(name_hint, url)
    if nm == 2:
        score += 30; why.append("domain matches name")
    elif nm == 1:
        score += 12; why.append("domain ~ name")
    if (a.get("itemprop") or "").lower() in ("url", "sameas"):
        score += 15; why.append("itemprop url")
    if text and name_hint and _norm_space(text).lower() == _norm_space(name_hint).lower():
        score += 15; why.append("company name links out")
    if in_ad(a):
        score -= 60; why.append("ad/sponsor")
    if check_chrome and in_chrome(a):
        score -= 45; why.append("header/footer/nav")
    if chrome_hosts and reg_domain(host) in chrome_hosts and nm < 2:
        score -= 60; why.append("appears on every page")
    if re.search(r"(facebook|instagram|twitter|linkedin|youtube|yelp|houzz|google)", text, re.I):
        score -= 40
    return score, why


def _onclick_url(el: Tag) -> str | None:
    for attr in ("data-href", "data-url", "data-link", "data-website", "data-target-url", "data-site"):
        v = el.get(attr)
        if v and re.match(r"^(https?://|www\.)", v.strip(), re.I):
            return v.strip()
    oc = el.get("onclick") or ""
    m = re.search(r"(?:window\.open|location(?:\.href)?\s*=|open\w*)\s*\(?\s*['\"]((?:https?://|www\.)[^'\"]+)['\"]", oc, re.I)
    if m:
        return m.group(1)
    return None


def iter_links(root: Tag, base_url: str):
    """(element, absolute_url) for every clickable thing: <a href>, data-href, onclick."""
    for el in root.find_all(["a", "button", "div", "span", "area", "li"]):
        url = None
        if el.name in ("a", "area") and el.get("href"):
            url = clean_href(el["href"], base_url)
            if url is None:
                alt = _onclick_url(el)
                url = clean_href(alt, base_url) if alt else None
        else:
            alt = _onclick_url(el)
            if alt:
                url = clean_href(alt, base_url)
        if url:
            yield el, url


# ---------------------------------------------------------------------------
# JSON-LD helpers
# ---------------------------------------------------------------------------

def _json_ld_items(soup: BeautifulSoup) -> list[dict]:
    out = []
    for sc in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = sc.string or sc.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw.strip()))
            except Exception:
                continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                for k in ("@graph", "itemListElement", "item", "mainEntity", "about", "provider", "author", "publisher"):
                    v = d.get(k)
                    if isinstance(v, (list, dict)):
                        stack.append(v)
    return out


def _ld_types(d: dict) -> str:
    t = d.get("@type", "")
    return " ".join(t) if isinstance(t, list) else str(t)


_BUSINESS_TYPE = re.compile(r"(Business|Organization|Contractor|Service|Store|Company|Corporation|Agent|Professional|Plumber|Electrician|Roofing|HomeAndConstruction|GeneralContractor|Place)", re.I)


# ---------------------------------------------------------------------------
# PROFILE PAGE -> website
# ---------------------------------------------------------------------------

def extract_profile(html: str, page_url: str, name_hint: str = "", chrome_hosts: set | None = None) -> dict:
    """
    Read one company's page inside the directory.
    Returns {name, website, score, why, candidates:[(score,url)], emails, phone, internal_website}
    `internal_website` = a 'Website' button that points back into the directory
    (redirect page) - the caller should open it to see where it lands.
    """
    soup = BeautifulSoup(html, "lxml")
    dir_domain = reg_domain(page_url)
    ld = _json_ld_items(soup)

    name = name_hint or ""
    ld_sites = []
    for d in ld:
        if _BUSINESS_TYPE.search(_ld_types(d)) and not re.search(r"(WebSite|WebPage|BreadcrumbList|SearchAction)", _ld_types(d)):
            nm = d.get("name")
            if isinstance(nm, str) and nm.strip() and not name:
                name = _norm_space(_html_unescape(nm))
            for k in ("url", "sameAs", "website", "mainEntityOfPage"):
                v = d.get(k)
                for u in (v if isinstance(v, list) else [v]):
                    if isinstance(u, str) and u.strip():
                        cu = clean_href(u, page_url)
                        if cu:
                            ld_sites.append((k, cu))
    if not name:
        name = _profile_name(soup, dir_domain)

    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()

    # Blocks of OTHER companies on this page ('Similar pros', 'Nearby businesses' ...):
    # their 'Visit Website' buttons belong to those companies, not to this one.
    zone_ids = set()
    try:
        for _sc, _parent, members, _info in _find_groups(soup, page_url, dir_domain, {}):
            for m in members:
                for root in [m.el] + m.extra:
                    zone_ids.add(id(root))
    except Exception:
        zone_ids = set()

    cands: dict[str, list] = {}     # url -> [score, why, order]
    internal_site = None
    order = 0
    for el, url in iter_links(soup, page_url):
        order += 1
        host = host_of(url)
        if reg_domain(host) == dir_domain:
            # a 'Website' button that goes through the directory's own redirect page
            if internal_site is None and el.name in ("a", "button"):
                t = _norm_space(el.get_text(" "))
                if (_WEBSITE_WORD.search(t) or re.search(r"(^|[\s_-])(website|weblink)([\s_-]|$)", _attr_blob(el))) \
                        and not in_chrome(el) and re.search(r"(redirect|out|go|visit|website|link|click|track|exit|external)", urlparse(url).path + urlparse(url).query, re.I):
                    internal_site = url
            continue
        res = score_website_link(el, url, name, chrome_hosts, dir_domain)
        if res is None:
            continue
        sc, why = res
        if zone_ids and _inside(el, zone_ids) and name_matches_host(name, url) < 2:
            sc -= 60
            why = why + ["inside a list of other companies"]
        key = url
        if key in cands:
            if sc > cands[key][0]:
                cands[key][0], cands[key][1] = sc, why
            cands[key][0] += 3   # linked twice (logo + button) -> a bit more sure
        else:
            cands[key] = [sc, why, order]

    for k, u in ld_sites:
        h = host_of(u)
        if not h or is_bad_site_host(h) or reg_domain(h) == dir_domain:
            continue
        sc = 45 + (25 if name_matches_host(name, u) == 2 else 0)
        if chrome_hosts and reg_domain(h) in chrome_hosts and name_matches_host(name, u) < 2:
            sc -= 60
        if u in cands:
            cands[u][0] += 25; cands[u][1] = cands[u][1] + ["JSON-LD " + k]
        else:
            cands[u] = [sc, ["JSON-LD " + k], 0]

    # Website written as plain text: "Website: www.abc.com"
    body_text = soup.get_text("\n")
    for m in re.finditer(r"(?:web\s?site|homepage|web)\s*[:\-]\s*((?:https?://)?(?:www\d?\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}(?:/[^\s<>\"']*)?)",
                         body_text, re.I):
        u = clean_href(m.group(1) if m.group(1).lower().startswith("http") else "http://" + m.group(1), page_url)
        if u and not is_bad_site_host(host_of(u)) and reg_domain(u) != dir_domain:
            if not any(reg_domain(c) == reg_domain(u) for c in cands):
                sc = 60 + (20 if name_matches_host(name, u) == 2 else 0)
                if chrome_hosts and reg_domain(u) in chrome_hosts and name_matches_host(name, u) < 2:
                    sc -= 60
                cands[u] = [sc, ["text 'Website: ...'"], 10**6]

    ranked = sorted(cands.items(), key=lambda kv: (-kv[1][0], kv[1][2]))
    # Same site found several ways (button + JSON-LD + text) = more sure; report the
    # directory's own button link when there is one (the exact link the user would click).
    by_dom: dict[str, list] = {}
    for u, v in ranked:
        by_dom.setdefault(reg_domain(u), []).append((u, v))
    best_url, best = "", [0, [], 0]
    for dom, items in by_dom.items():
        top = max(v[0] for _, v in items)
        positives = sum(1 for _, v in items if v[0] >= 30)
        dom_score = top + (10 if positives >= 2 else 0)
        anchors = [(u, v) for u, v in items if 0 < v[2] < 10**6 and v[0] >= 30]
        u, v = (max(anchors, key=lambda x: (x[1][0], -x[1][2])) if anchors else items[0])
        if dom_score > best[0] or (dom_score == best[0] and best_url and v[2] < best[2]):
            best_url, best = u, [dom_score, sorted(set(why for _, vv in items for why in vv[1])), v[2]]
    website = best_url if best[0] >= 30 else ""
    # Two different strong candidates that disagree and neither matches the name -> keep the
    # one higher on the page only if it is clearly stronger.
    return {
        "name": name,
        "website": normalize_website(website) if website else "",
        "score": best[0] if website else 0,
        "why": best[1] if website else [],
        "candidates": [(v[0], u) for u, v in ranked[:6]],
        "internal_website": internal_site,
        "emails": _emails(soup),
        "phone": _first_phone(soup),
    }


def _inside(el: Tag, ids: set) -> bool:
    node = el
    while node is not None:
        if id(node) in ids:
            return True
        node = node.parent
    return False


def _html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def _profile_name(soup: BeautifulSoup, dir_domain: str) -> str:
    brand = dir_domain.split(".")[0]
    h1s = [h for h in soup.find_all("h1") if not in_chrome(h)] or soup.find_all("h1")
    for h in h1s:
        t = _norm_space(h.get_text(" "))
        if 2 <= len(t) <= 120 and brand not in t.lower().replace(" ", ""):
            return t
    og = soup.find("meta", property="og:title")
    for raw in ([og.get("content")] if og and og.get("content") else []) + ([soup.title.string] if soup.title and soup.title.string else []):
        cand = re.split(r"\s+[|–—·•-]\s+", _norm_space(raw))[0]
        if cand and brand not in cand.lower().replace(" ", ""):
            return cand
    return ""


def _emails(soup: BeautifulSoup) -> list[str]:
    out = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("mailto:"):
            e = unquote(h[7:].split("?")[0]).strip().lower()
            if "@" in e and e not in out:
                out.append(e)
    return out


def _first_phone(soup: BeautifulSoup) -> str:
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.lower().startswith("tel:"):
            t = _norm_space(a.get_text(" ")) or h[4:]
            if len(re.sub(r"\D", "", t)) >= 7:
                return t
    return ""


def website_from_email(emails: list[str], dir_domain: str = "") -> str:
    """Last resort: business e-mail info@abcbuilders.com -> https://abcbuilders.com."""
    for e in emails:
        dom = e.split("@")[-1].strip().lower().rstrip(".")
        if not dom or "." not in dom:
            continue
        rd = reg_domain(dom)
        if rd in FREE_EMAIL_DOMAINS or rd in NOT_A_WEBSITE or rd == dir_domain:
            continue
        if any(x in rd for x in ("mail.", "email.", "yahoo.", "hotmail.", "outlook.")):
            continue
        return "https://" + dom
    return ""


# ---------------------------------------------------------------------------
# LIST PAGE -> company records
# ---------------------------------------------------------------------------

def _path_template(url: str) -> tuple:
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        q = tuple(sorted(parse_qs(p.query).keys()))
        return ("/", 0, q)
    norm = [re.sub(r"\d+", "#", s.lower()) if re.fullmatch(r"[\d-]+", s) else s.lower() for s in segs[:-1]]
    q = tuple(sorted(k for k in parse_qs(p.query).keys())) if len(segs) <= 1 else ()
    return ("/".join(norm), len(segs), q)


def _loose_template(url: str) -> tuple:
    p = urlparse(url)
    segs = [s.lower() for s in p.path.split("/") if s]
    return (segs[0] if segs else "", len(segs))


def _url_key(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    # keep the query only when the path itself is not unique (listing.php?id=5)
    q = p.query
    if q and (len([s for s in path.split("/") if s]) <= 1 or re.search(r"\.(php|aspx?|cfm|jsp)$", path, re.I)):
        qs = parse_qs(q)
        keep = {k: v for k, v in qs.items() if not re.match(r"^(utm_|ref|src|source|fbclid|gclid)", k, re.I)}
        return path + "?" + urlencode(sorted(keep.items()), doseq=True)
    return path


class _Member:
    __slots__ = ("el", "extra", "internal", "external", "text", "has_tel", "has_mail", "has_heading",
                 "has_img", "has_addr", "primary", "n_int_same_tmpl", "names")

    def __init__(self, el, extra=()):
        self.el = el
        self.extra = list(extra)


def _member_info(m: _Member, base_url: str, dir_domain: str, link_cache: dict) -> None:
    els = [m.el] + m.extra
    internal, external = [], []
    texts = []
    has_tel = has_mail = has_heading = has_img = False
    for root in els:
        texts.append(root.get_text(" "))
        if root.name in ("h1", "h2", "h3", "h4", "h5", "h6") or root.find(["h1", "h2", "h3", "h4", "h5", "h6"]):
            has_heading = True
        if root.name == "img" or root.find("img"):
            has_img = True
        anchors = [root] if root.name == "a" else []
        anchors += root.find_all("a")
        for a in anchors:
            href = a.get("href") or ""
            low = href.strip().lower()
            if low.startswith("tel:"):
                has_tel = True
                continue
            if low.startswith("mailto:"):
                has_mail = True
                continue
            key = id(a)
            if key not in link_cache:
                u = clean_href(href, base_url) if href else None
                if u is None:
                    alt = _onclick_url(a)
                    u = clean_href(alt, base_url) if alt else None
                link_cache[key] = u
            u = link_cache[key]
            if not u:
                continue
            h = host_of(u)
            if reg_domain(h) == dir_domain:
                internal.append((a, u))
            else:
                external.append((a, u))
    text = _norm_space(" ".join(texts))
    m.text = text
    m.internal = internal
    m.external = external
    m.has_tel = has_tel or bool(_PHONE.search(text))
    m.has_mail = has_mail
    m.has_heading = has_heading
    m.has_img = has_img
    m.has_addr = bool(_ADDRESS.search(text))


def _choose_primary(m: _Member, base_url: str) -> None:
    """The member's main internal link (its profile page)."""
    page_key = _url_key(base_url)
    cnt = Counter()
    first = {}
    for a, u in m.internal:
        k = _url_key(u)
        if k == page_key or k in ("/", ""):
            continue
        if re.search(r"(^|/)(category|categories|tag|tags|page|author|search|login|register|join|cart|events?|news|blog|contact|about|privacy|terms)(/|$)", k, re.I) \
                and not re.search(r"(member|company|listing|business|profile|biz|pro|contractor|directory/details)", k, re.I):
            continue
        if "/cdn-cgi/" in k:
            continue
        if "#" in (a.get("href") or "") and not urlparse(u).path.strip("/"):
            continue
        cnt[k] += 1
        first.setdefault(k, u)
    if cnt:
        k = max(cnt, key=lambda x: (cnt[x], -list(first).index(x)))
        m.primary = first[k].split("#")[0]
    else:
        m.primary = None
    tmpls = Counter(_path_template(u) for k, u in first.items())
    m.n_int_same_tmpl = max(tmpls.values()) if tmpls else 0


def _is_navish(m: _Member) -> bool:
    t = m.text.lower().strip()
    return (len(t) < 40 and (t in _NAV_WORDS or len(t.split()) <= 2) and not m.has_tel and not m.has_addr
            and not m.external)


def _link_text_len(m: _Member) -> int:
    n = 0
    for a, _ in m.internal + m.external:
        n += len(_norm_space(a.get_text(" ")))
    return n


def _text_bits(m: _Member) -> int:
    n = 0
    for root in [m.el] + m.extra:
        for t in root.stripped_strings:
            if len(t) >= 2 and re.search(r"[A-Za-z0-9]", t):
                n += 1
                if n >= 6:
                    return n
    return n


def _group_score(members: list[_Member], base_url: str, dir_domain: str) -> tuple[float, dict]:
    good = [m for m in members if (m.primary or m.external) and len(re.sub(r"[^A-Za-z]", "", m.text)) >= 3]
    n = len(good)
    if n < 3:
        return 0, {}
    keys = [m.primary or ("ext:" + reg_domain(m.external[0][1])) for m in good]
    uniq = len(set(keys)) / n
    if uniq < 0.6:
        return 0, {}
    multi = sum(1 for m in good if m.n_int_same_tmpl >= 2) / n
    if multi > 0.5:
        return 0, {}
    tm = Counter(_path_template(m.primary) for m in good if m.primary)
    tmpl, tcount = (tm.most_common(1)[0] if tm else (None, 0))
    lm = Counter(_loose_template(m.primary) for m in good if m.primary)
    lcount = lm.most_common(1)[0][1] if lm else 0
    t_cons = max(tcount / n, 0.8 * lcount / n)
    ext_site = sum(1 for m in good if any(score_website_link(a, u, "", None, dir_domain, check_chrome=False) for a, u in m.external)) / n
    f_tel = sum(m.has_tel for m in good) / n
    f_addr = sum(m.has_addr for m in good) / n
    f_head = sum(m.has_heading for m in good) / n
    f_img = sum(m.has_img for m in good) / n
    avg_len = sum(len(m.text) for m in good) / n
    # more than just a name: separate bits of text (rating, city, phone, description ...)
    f_extra = sum(1 for m in good if _text_bits(m) >= 3) / n
    nav = sum(_is_navish(m) for m in good) / n
    chrome = sum(in_chrome(m.el) for m in good) / n
    rich = (1 + 1.2 * ext_site + 0.8 * f_tel + 0.8 * f_addr + 0.4 * f_head + 0.3 * f_img
            + (0.5 if avg_len > 40 else 0) + 1.0 * t_cons + 0.8 * f_extra)
    if not tmpl and ext_site < 0.5:
        rich *= 0.5
    rich *= (1 - 0.8 * nav) * (1 - 0.85 * chrome)
    if avg_len > 1500:
        rich *= 0.3          # huge blocks = whole page sections, not cards
    info = {"template": tmpl, "n": n, "uniq": round(uniq, 2), "ext": round(ext_site, 2),
            "tel": round(f_tel, 2), "addr": round(f_addr, 2), "nav": round(nav, 2), "extra": round(f_extra, 2),
            "chrome": round(chrome, 2), "tcons": round(t_cons, 2), "avg_len": int(avg_len)}
    # a real company list: not a menu, not page chrome, and the blocks are alike
    signal = max(f_extra, f_tel, f_addr, ext_site)
    info["ok"] = (nav < 0.5 and chrome < 0.5 and (t_cons >= 0.6 or ext_site >= 0.5)
                  and (signal >= 0.3 or n >= 8))
    if n < 5:
        # a tiny group must really look like companies (phone / address), not a hub or footer
        info["ok"] = info["ok"] and max(f_tel, f_addr) >= 0.5
    return n * rich, info


def _flat_members(parent: Tag, members: list[Tag]) -> list[_Member]:
    """Members that are just headings in a flat list: attach following siblings until the next one."""
    out = []
    mem_ids = {id(m) for m in members}
    kids = [c for c in parent.children if isinstance(c, Tag)]
    cur = None
    for c in kids:
        if id(c) in mem_ids:
            cur = _Member(c)
            out.append(cur)
        elif cur is not None:
            cur.extra.append(c)
    return out


def _record_name(m: _Member) -> str:
    cands = []
    els = [m.el] + m.extra
    # 1. text of links to the profile page
    if m.primary:
        pk = _url_key(m.primary)
        for a, u in m.internal:
            if _url_key(u) == pk:
                t = _norm_space(a.get_text(" "))
                if t and not _GENERIC_LINK_TEXT.match(t) and 2 <= len(t) <= 150 and not _PHONE.fullmatch(t):
                    cands.append((3, t))
    # 2. headings
    for root in els:
        hs = [root] if root.name in ("h1", "h2", "h3", "h4", "h5", "h6") else []
        hs += root.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        for h in hs:
            t = _norm_space(h.get_text(" "))
            if t and not _GENERIC_LINK_TEXT.match(t) and 2 <= len(t) <= 150:
                cands.append((4, t))
                break
    # 3. class contains name/title
    for root in els:
        for el in root.find_all(class_=re.compile(r"(name|title|company|business|heading)", re.I), limit=4):
            t = _norm_space(el.get_text(" "))
            if t and not _GENERIC_LINK_TEXT.match(t) and 2 <= len(t) <= 150 and not _PHONE.search(t):
                cands.append((2, t))
                break
    # 4. external link whose text is the name (web directories)
    for a, u in m.external:
        t = _norm_space(a.get_text(" "))
        if t and not _GENERIC_LINK_TEXT.match(t) and not _DOMAIN_TEXT.match(t.replace(" ", "")) and 2 <= len(t) <= 120:
            cands.append((1, t))
    # 5. strong / b / img alt
    for root in els:
        for el in root.find_all(["strong", "b"], limit=2):
            t = _norm_space(el.get_text(" "))
            if t and 2 <= len(t) <= 120 and not _GENERIC_LINK_TEXT.match(t):
                cands.append((1, t))
        for img in root.find_all("img", alt=True, limit=2):
            t = _norm_space(img["alt"])
            if t and 2 <= len(t) <= 120 and not re.search(r"(logo|image|photo|icon|placeholder)$", t, re.I):
                cands.append((0, re.sub(r"\s*(logo|image)$", "", t, flags=re.I)))
    if not cands:
        first = _norm_space(m.text)[:100]
        return first
    best = max(cands, key=lambda c: (c[0], -abs(len(c[1]) - 30) / 100))
    name = best[1]
    return clean_name(name)


_JUNK_NAME = re.compile(
    r"^(?:(?:log ?in|log ?out|sign ?(?:in|up|out)|register|copyright|privacy( policy)?|terms( of use)?|home|"
    r"contact( us)?|about( us)?|menu|search|join( now| us)?|next|previous|more|details)\W*$"
    r"|powered by\b|skip to\b|©|view (?:our )?(?:full )?(?:membership )?directory\b|our (?:supporting )?members\b|"
    r"find a (?:member|contractor|builder)\b|become a member\b|member login\b|page \d+\b)", re.I)


def clean_name(name: str) -> str:
    name = _norm_space(name)
    name = re.sub(r"\s*(Learn More|View (Listing|Profile|Details)|Read More|Visit Website|New on \w+)\s*$", "", name, flags=re.I)
    name = re.sub(r"[\s»›→>|:\-–—•·]+$", "", name)
    name = re.sub(r"^[\s«‹<|:\-•·]+", "", name)
    return name.strip()


def _record_website(m: _Member, name: str, dir_domain: str, chrome_hosts: set | None) -> tuple[str, int, list]:
    best = ("", 0, [])
    for a, u in m.external:
        res = score_website_link(a, u, name, chrome_hosts, dir_domain, check_chrome=False)
        if not res:
            continue
        sc, why = res
        if sc > best[1]:
            best = (u, sc, why)
    if best[1] < 30:
        # plain-text www.abc.com inside the card
        txt = m.text
        for rx in (_WWW_IN_TEXT, _URL_IN_TEXT):
            for mm in rx.finditer(txt):
                raw = mm.group(1).rstrip(".,;)")
                u = raw if raw.lower().startswith("http") else "http://" + raw
                h = host_of(u)
                if h and not is_bad_site_host(h) and reg_domain(h) != dir_domain:
                    return (u, 55, ["www text in card"])
    return best if best[1] >= 30 else ("", best[1], best[2])


def _find_groups(soup: BeautifulSoup, page_url: str, dir_domain: str, link_cache: dict) -> list:
    """All blocks of repeated company cards on the page: [(score, parent, members, info)]."""
    groups = []
    for parent in soup.find_all(True):
        kids = [c for c in parent.children if isinstance(c, Tag)]
        if len(kids) < 3:
            continue
        by_tag = defaultdict(list)
        for c in kids:
            by_tag[c.name].append(c)
        for tag, els in by_tag.items():
            if len(els) < 3 or tag in ("option", "br", "hr", "img", "input", "meta", "link", "source", "td", "th", "col"):
                continue
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "b", "p", "span", "dt"):
                members = _flat_members(parent, els)      # flat list: heading + following siblings
            else:
                members = [_Member(e) for e in els]
            for m in members:
                _member_info(m, page_url, dir_domain, link_cache)
                _choose_primary(m, page_url)
            sc, info = _group_score(members, page_url, dir_domain)
            if sc > 0 and info.get("ok"):
                groups.append((sc, parent, members, info))
    return groups


def analyze_list(html: str, page_url: str, chrome_hosts: set | None = None) -> dict:
    """
    Find the company records on a directory list page.
    Returns {"records": [{name, profile_url, website, website_score, phone, emails}],
             "template": profile-url pattern, "chrome_hosts": outside links that are
             not inside any record (header/footer/sponsors), "debug": {...}}
    """
    soup = BeautifulSoup(html, "lxml")
    dir_domain = reg_domain(page_url)
    ld_records = _ld_item_list(soup, page_url, dir_domain)
    for t in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        t.decompose()

    link_cache: dict = {}
    groups = _find_groups(soup, page_url, dir_domain, link_cache)
    if not groups and not ld_records:
        return {"records": [], "template": None, "chrome_hosts": set(), "debug": {"groups": 0}}

    groups.sort(key=lambda g: -g[0])
    chosen = []
    if groups:
        top = groups[0]
        chosen.append(top)
        tmpl = top[3].get("template")
        # other blocks with the same kind of profile links (e.g. 'Featured' + normal list)
        for g in groups[1:]:
            if g[0] < 0.25 * top[0]:
                continue
            if tmpl and g[3].get("template") == tmpl and not _overlaps(g, chosen):
                chosen.append(g)

    records, seen = [], {}
    record_nodes = []
    for _, parent, members, info in chosen:
        g_tmpl = info.get("template")
        prim = [m.primary for m in members if m.primary]
        strict_share = (sum(1 for u in prim if _path_template(u) == g_tmpl) / len(prim)) if prim else 0
        g_loose = Counter(_loose_template(u) for u in prim).most_common(1)
        g_loose = g_loose[0][0] if g_loose else None
        for m in members:
            if not (m.primary or m.external):
                continue
            if m.primary and not m.external:
                if m.n_int_same_tmpl >= 3:
                    continue      # a bar of many links (A-Z letters, page numbers), not a company
                odd = (g_tmpl and strict_share >= 0.6 and _path_template(m.primary) != g_tmpl) or \
                      (g_tmpl and strict_share < 0.6 and _loose_template(m.primary) != g_loose)
                if odd and not (_text_bits(m) >= 3 or m.has_tel or m.has_addr):
                    continue      # different link pattern and nothing company-like inside
            name = _record_name(m)
            if _JUNK_NAME.match(name or ""):
                continue
            web, wsc, why = _record_website(m, name, dir_domain, chrome_hosts)
            key = (m.primary or "").split("#")[0] or ("ext:" + reg_domain(web) if web else "name:" + name.lower())
            if not name:
                continue
            record_nodes.append(m)
            if key in seen:
                # same company in a second block (e.g. 'Top picks' + full list): fill what was missing
                old = seen[key]
                if web and wsc > old["website_score"]:
                    old.update(website=web, website_score=wsc, website_why=why)
                continue
            emails = []
            phone = ""
            for root in [m.el] + m.extra:
                for a in ([root] if root.name == "a" else []) + root.find_all("a", href=True):
                    h = (a.get("href") or "").strip()
                    bare = re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", h)       # 'info@abc.com' without mailto:
                    if h.lower().startswith("mailto:") or bare:
                        e = unquote(h[7:] if not bare else h).split("?")[0].strip().lower()
                        if "@" in e and e not in emails:
                            emails.append(e)
                    elif h.lower().startswith("tel:") and not phone:
                        phone = _norm_space(a.get_text(" ")) or h[4:]
            if not phone:
                mm = _PHONE.search(m.text)
                phone = mm.group(0) if mm else ""
            st = _STATE_ZIP.search(m.text)
            rec = {
                "name": name,
                "profile_url": m.primary or "",
                "website": web,
                "website_score": wsc,
                "website_why": why,
                "phone": phone,
                "emails": emails,
                "state": st.group(1) if st and st.group(1) in _US_STATES else "",
            }
            seen[key] = rec
            records.append(rec)

    if ld_records and len(ld_records) > len(records):
        records = ld_records

    # Outside links that are not part of any record = the directory's own chrome
    rec_ids = set()
    for m in record_nodes:
        for root in [m.el] + m.extra:
            rec_ids.add(id(root))
    chrome = Counter()
    for a in soup.find_all("a", href=True):
        u = link_cache.get(id(a)) or clean_href(a["href"], page_url)
        if not u:
            continue
        h = host_of(u)
        if reg_domain(h) == dir_domain or is_bad_site_host(h):
            continue
        node, inside = a, False
        while node is not None:
            if id(node) in rec_ids:
                inside = True
                break
            node = node.parent
        if not inside:
            chrome[reg_domain(h)] += 1
    rec_domains = {reg_domain(r["website"]) for r in records if r["website"]}
    chrome_set = {d for d in chrome if d not in rec_domains}

    return {
        "records": records,
        "template": chosen[0][3].get("template") if chosen else None,
        "chrome_hosts": chrome_set,
        "debug": {"groups": len(groups), "chosen": [g[3] for g in chosen],
                  "top": [(round(g[0], 1), g[1].name, " ".join(_tokens(g[1]))[:40], g[3]) for g in groups[:4]]},
    }


def _overlaps(g, chosen) -> bool:
    for c in chosen:
        p1, p2 = g[1], c[1]
        node = p1
        while node is not None:
            if node is p2:
                return True
            node = node.parent
        node = p2
        while node is not None:
            if node is p1:
                return True
            node = node.parent
    return False


def _ld_item_list(soup: BeautifulSoup, page_url: str, dir_domain: str) -> list[dict]:
    """schema.org ItemList of businesses on the list page (some directories ship one)."""
    out, seen = [], set()
    for d in _json_ld_items(soup):
        if not _BUSINESS_TYPE.search(_ld_types(d)) or re.search(r"(WebSite|WebPage|Breadcrumb)", _ld_types(d)):
            continue
        name = d.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        prof, web = "", ""
        for k in ("url", "@id", "mainEntityOfPage", "sameAs"):
            v = d.get(k)
            for u in (v if isinstance(v, list) else [v]):
                if not isinstance(u, str):
                    continue
                cu = clean_href(u, page_url)
                if not cu:
                    continue
                if reg_domain(cu) == dir_domain:
                    if _url_key(cu) != _url_key(page_url):
                        prof = prof or cu.split("#")[0]
                elif not is_bad_site_host(host_of(cu)):
                    web = web or cu
        key = prof or web or name
        if key in seen or not (prof or web):
            continue
        seen.add(key)
        out.append({"name": _norm_space(_html_unescape(name)), "profile_url": prof, "website": web,
                    "website_score": 60 if web else 0, "website_why": ["JSON-LD list"] if web else [],
                    "phone": d.get("telephone") or "", "emails": []})
    return out if len(out) >= 3 else []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

_NEXT_TEXT = re.compile(r"^\s*(next|next page|next\s*[>›»→]+|[>›»→]|>>|»»|›|more results|older|siguiente|suivant|weiter|nächste)\s*$", re.I)
_PAGE_PARAMS = ("page", "p", "pg", "paged", "pagenum", "pageno", "page_no", "pagenumber", "currentpage", "pn", "pageindex")
_OFFSET_PARAMS = ("start", "offset", "fi", "from", "skip", "first", "s")


def _pagination_box(el: Tag) -> bool:
    """Is `el` inside a pagination bar? By its class/label, or by its shape: a small block
    holding two or more links that are just page numbers (1 2 3 ...)."""
    node = el
    for depth in range(5):
        if node is None or not isinstance(node, Tag):
            return False
        blob = " ".join(_tokens(node)) + " " + (node.get("aria-label") or "").lower() + " " + (node.get("role") or "")
        if re.search(r"(pagination|paging|pager|page-numbers|pagenav|page-nav|nav-links|wp-pagenavi|paginate|pages)", blob):
            return True
        if depth <= 3 and node.name not in ("body", "html", "main"):
            nums = 0
            for x in node.find_all(["a", "button", "span", "li"], limit=60):
                if re.fullmatch(r"\d{1,4}", _norm_space(x.get_text(" "))):
                    nums += 1
                    if nums >= 2:
                        break
            if nums >= 2 and len(_norm_space(node.get_text(" "))) < 250:
                return True
        node = node.parent
    return False


_SLIDER_RE = re.compile(r"(slick|swiper|carousel|slider|owl-|glide|splide|flickity|lightbox|gallery|datepicker|calendar|tooltip|onboarding|tour|modal|dialog|wizard|stepper|popover|cookie)", re.I)


def _in_slider(el: Tag) -> bool:
    node = el
    for _ in range(6):
        if node is None or not isinstance(node, Tag):
            return False
        if node.name in ("body", "html", "main", "header", "footer"):
            return False
        if any(_SLIDER_RE.search(t) for t in _tokens(node)):
            return True
        node = node.parent
    return False


def _disabled(el: Tag) -> bool:
    blob = " ".join(_tokens(el))
    par = el.parent if isinstance(el.parent, Tag) else None
    pblob = " ".join(_tokens(par)) if par else ""
    return ("disabled" in blob or "disabled" in pblob or el.get("disabled") is not None
            or (el.get("aria-disabled") or "").lower() == "true")


def _same_listing(u1: str, u2: str) -> bool:
    p1, p2 = urlparse(u1), urlparse(u2)
    if reg_domain(p1.hostname or "") != reg_domain(p2.hostname or ""):
        return False
    a = [s for s in p1.path.split("/") if s]
    b = [s for s in p2.path.split("/") if s]
    a = [s for s in a if not re.fullmatch(r"\d+", s) and s.lower() != "page"]
    b = [s for s in b if not re.fullmatch(r"\d+", s) and s.lower() != "page"]
    if a == b:
        return True
    # e.g. /list/category/construction-191  vs  /list/category/construction-191/2
    return bool(a) and bool(b) and (a[:max(1, len(a) - 1)] == b[:max(1, len(a) - 1)])


def find_next_page(html: str, current_url: str, records_on_page: int = 0) -> tuple[str | None, str]:
    """
    Returns (next_url, how). next_url is None when there is no next page by URL.
    how = 'rel' | 'button' | 'number' | 'param' | 'js' (pagination exists but needs clicking) | ''.
    """
    soup = BeautifulSoup(html, "lxml")
    cur_key = current_url.split("#")[0]

    def ok(u):
        return u and u.split("#")[0] != cur_key and _same_listing(current_url, u)

    # 1. <link rel="next"> / <a rel="next">
    for el in soup.find_all(["link", "a"], href=True):
        rel = el.get("rel") or []
        rel = rel if isinstance(rel, list) else str(rel).split()
        if any(str(r).lower() == "next" for r in rel):
            u = clean_href(el["href"], current_url)
            if ok(u):
                return u, "rel"

    js_pagination = False
    # 2. Next buttons
    for a in soup.find_all(["a", "button", "li", "span"]):
        if a.name in ("li", "span") and a.find(["a", "button"]):
            continue
        if _in_slider(a):
            continue                     # carousel / gallery arrows are not page buttons
        txt = _norm_space(a.get_text(" "))
        label = " ".join([a.get("aria-label") or "", a.get("title") or "", " ".join(_tokens(a))]).lower()
        word = re.fullmatch(r"(next|next page|next\s*[>›»→]+|more results|older|siguiente|suivant|weiter|nächste)", txt, re.I) \
            or re.search(r"(^|[\s_-])(next|nextpage|next-page|pager-next|pagination-next|page-next)([\s_-]|$)", label) \
            or re.search(r"\bnext( page)?\b", (a.get("aria-label") or "") + " " + (a.get("title") or ""), re.I)
        symbol = bool(re.fullmatch(r"[>›»→]|>>|»»", txt))
        in_box = _pagination_box(a)
        if _disabled(a) or not (word or (symbol and in_box)):
            continue
        href = a.get("href") if a.name == "a" else None
        u = clean_href(href, current_url) if href else None
        if ok(u):
            return u, "button"
        if in_box or (word and re.search(r"next\s*(page|results)|page-next|pagination-next|pager-next", txt + " " + label, re.I)):
            js_pagination = True

    # 3. Page numbers: current+1
    cur_num = None
    for el in soup.select('[aria-current="page"], .active, .current, .selected, .is-active, [class*="current"], [class*="active"]'):
        if not _pagination_box(el):
            continue
        t = _norm_space(el.get_text(" "))
        if re.fullmatch(r"\d{1,4}", t):
            cur_num = int(t)
            break
    if cur_num is None:
        cur_num = _current_page_number(current_url) or 1
    for a in soup.find_all(["a", "button"]):
        if _norm_space(a.get_text(" ")) == str(cur_num + 1) and _pagination_box(a):
            href = a.get("href") if a.name == "a" else None
            u = clean_href(href, current_url) if href else None
            if ok(u):
                return u, "number"
            js_pagination = True

    # 4. URL already carries a page number -> +1 (stops when a page brings nothing new)
    u = bump_page_param(current_url, records_on_page)
    if u and records_on_page > 0:
        return u, "param"
    return None, ("js" if js_pagination else "")


def _current_page_number(url: str) -> int | None:
    p = urlparse(url)
    qs = parse_qs(p.query)
    for k in _PAGE_PARAMS:
        for kk in (k, k.upper(), k.capitalize()):
            if kk in qs and qs[kk][0].isdigit():
                return int(qs[kk][0])
    m = re.search(r"/page/(\d+)/?$", p.path, re.I)
    return int(m.group(1)) if m else None


def bump_page_param(url: str, step_hint: int = 0) -> str | None:
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=True)
    for k in _PAGE_PARAMS:
        for kk in (k, k.upper(), k.capitalize()):
            if kk in qs and qs[kk][0].isdigit():
                qs[kk] = [str(int(qs[kk][0]) + 1)]
                return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
    for k in _OFFSET_PARAMS:
        if k in qs and qs[k][0].isdigit() and step_hint:
            qs[k] = [str(int(qs[k][0]) + step_hint)]
            return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
    m = re.search(r"/page/(\d+)/?$", p.path, re.I)
    if m:
        new_path = p.path[:m.start(1)] + str(int(m.group(1)) + 1) + p.path[m.end(1):]
        return urlunparse(p._replace(path=new_path))
    return None


# ---------------------------------------------------------------------------
# Does this page need a real browser?
# ---------------------------------------------------------------------------

_STATE_ZIP = re.compile(r"(?:,|\s)\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_TOTAL_RES = (
    re.compile(r"\b\d[\d,]*\s*(?:-|–|to)\s*\d[\d,]*\s+of\s+(\d[\d,]*)\b", re.I),
    re.compile(r"\b\d[\d,]*\s+of\s+(\d[\d,]*)\s+(?:results|listings|members|companies|businesses|records|entries|pros|contractors|matches)\b", re.I),
    re.compile(r"\b(\d[\d,]*)\s+(?:results|listings|members|companies|businesses|matches|contractors|pros)\s+(?:found|total)\b", re.I),
    re.compile(r"\b(?:showing|total|found)\s*:?\s*(\d[\d,]*)\s+(?:results|listings|members|companies|businesses|matches|contractors)\b", re.I),
    re.compile(r"\bresults?\s+found\s*:?\s*(\d[\d,]*)\b", re.I),
)


def total_hint(html: str) -> int | None:
    """'Showing 1-20 of 345' / '20 of 150 Results' -> 345 / 150 (how many companies the list has)."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = _norm_space(soup.get_text(" "))
    best = None
    for rx in _TOTAL_RES:
        for m in rx.finditer(text):
            try:
                n = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 0 < n < 1_000_000:
                best = max(best or 0, n)
        if best:
            return best
    return None


def has_load_more(html: str) -> bool:
    """Is there a 'Load more' / 'Show more' button for the list?"""
    soup = BeautifulSoup(html, "lxml")
    for el in soup.find_all(["a", "button", "span", "div"]):
        if el.find(["a", "button", "div"]):
            continue
        t = _norm_space(el.get_text(" "))
        if re.fullmatch(r"(load|show|view|see)\s+more(\s+(results|listings|members|companies|businesses|pros))?", t, re.I):
            return True
    return False


def _visible_size(html: str) -> tuple[int, int]:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    return len(_norm_space(soup.get_text(" "))), len(soup.find_all("a", href=True))


def page_is_fuller(browser_html: str, plain_html: str) -> bool:
    """Did the browser show clearly more than the plain download (= JavaScript fills the page)?"""
    bt, bl = _visible_size(browser_html)
    pt, pl = _visible_size(plain_html)
    return bt > pt * 1.3 + 300 or bl > pl + 15


def looks_js_rendered(html: str) -> bool:
    """Page is an empty app shell / content loads by JavaScript."""
    if not html:
        return True
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    text = _norm_space(soup.get_text(" "))
    links = len(soup.find_all("a", href=True))
    low = html.lower()
    shell = any(s in low for s in ('<app-root', 'id="root"></div>', "id=\"app\"></div>", "ng-version", "data-reactroot",
                                   "__next_data__", "__nuxt", "wa-widget", "loading...", "please wait"))
    return len(text) < 600 or links < 15 or (shell and len(text) < 2500)
