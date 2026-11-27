"""
Browser engine — true async HTTP with aiohttp for maximum speed.
Falls back to Playwright for JS-heavy sites.
20 pages at the same time, no thread pool bottleneck.
Smart detection: auto-detects blocked/Cloudflare pages and retries with browser.
"""

import asyncio
import re
from urllib.parse import urlparse, urljoin

import aiohttp
from playwright.async_api import async_playwright, Browser, BrowserContext

import config


# Images / fonts / video aren't needed for data extraction — skip them so the browser is fast
_SKIP_RESOURCES = {"image", "media", "font"}

# Titles of bot-check / block pages (a real page never has these as its title)
BLOCKED_TITLES = (
    "just a moment", "attention required", "access denied", "client challenge", "one moment, please",
    "please wait", "verifying you are human", "security check", "are you a robot", "you have been blocked",
    "403 forbidden", "pardon our interruption", "request unsuccessful", "checking your browser",
    "ddos-guard", "bot verification", "human verification", "unavailable in your region",
    "access to this page has been denied",
)
# Markers that only challenge pages carry
BLOCKED_SIGNALS = [
    "cf-browser-verification", "/cdn-cgi/challenge-platform", "challenge-platform", "captcha-delivery.com",
    "checking your browser", "enable javascript and cookies to continue", "sorry, you have been blocked",
    "please verify you are a human", "request is being verified", "px-captcha", "datadome",
    "one moment, please", "client challenge",
]


def _is_blocked(html: str) -> bool:
    """
    Is this a bot-check / block page instead of the real content?
    (Old check flagged every normal page that merely loads a Cloudflare/reCAPTCHA
    script, so good pages went to the slow browser or failed.)
    """
    if not html or len(html) < 500:
        return True
    low = html[:6000].lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = " ".join(m.group(1).split()) if m else ""
    if any(t in title for t in BLOCKED_TITLES):
        return True
    # challenge pages are small; a normal page may mention 'cloudflare' or 'recaptcha' in a script
    if len(html) < 25000 and any(sig in low for sig in BLOCKED_SIGNALS):
        return True
    return False


# Signature of the page's links: changes when a new page of companies is shown
_LINKS_SIG_JS = ("() => { const a = [...document.querySelectorAll('a[href]')].map(x => x.href);"
                 " return a.length + '|' + a.slice(-40).join('|'); }")

# Find the control that shows the next companies and mark it with data-hm-next="1"
_CLICK_NEXT_JS = r"""(n) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  const cls = e => String((e.className && e.className.baseVal !== undefined) ? e.className.baseVal : (e.className || '')) + ' ' + (e.id || '');
  const inPager = e => { for (let p = e, i = 0; p && i < 6; p = p.parentElement, i++) {
      const s = (cls(p) + ' ' + (p.getAttribute ? (p.getAttribute('aria-label') || '') + ' ' + (p.getAttribute('role') || '') : '')).toLowerCase();
      if (/(pagination|paging|pager|page-numbers|pagenav|page-nav|nav-links|paginate|pages)/.test(s)) return true;
      if (i <= 3 && (p.textContent || '').length < 300) {
        let nums = 0;
        for (const k of p.querySelectorAll('a, button, span, td, li')) {
          if (/^\d{1,4}$/.test((k.textContent || '').trim())) { nums++; if (nums >= 2) return true; }
        }
      } }
    return false; };
  const off = e => /disabled/i.test(cls(e) + ' ' + (e.parentElement ? cls(e.parentElement) : '')) || e.getAttribute('aria-disabled') === 'true' || e.disabled;
  const leaf = e => !e.querySelector('a, button');
  const slider = e => { for (let p = e, i = 0; p && i < 6; p = p.parentElement, i++)
      if (/(slick|swiper|carousel|slider|owl-|glide|splide|flickity|lightbox|gallery|datepicker|calendar|tooltip|onboarding|tour|modal|dialog|wizard|stepper|popover|cookie)/i.test(cls(p))) return true;
    return false; };
  const all = [...document.querySelectorAll('a, button, [role=button], li, span')].filter(e => vis(e) && leaf(e) && !off(e) && !slider(e));
  const nextWord = e => /^(next|next page|next ›|next »|next >|next →)$/i.test(txt(e)) ||
      /(^|\W)next(\W|$)/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '') + ' ' + cls(e));
  const nextSym = e => /^(›|»|>|→|>>)$/.test(txt(e));
  let el = all.find(e => inPager(e) && txt(e) === String(n + 1));
  if (!el) el = all.find(e => inPager(e) && (nextWord(e) || nextSym(e)));
  if (!el) el = all.find(e => (e.tagName === 'A' || e.tagName === 'BUTTON') && nextWord(e));
  if (!el) el = all.find(e => /^(load|show|view|see)\s+more(\s+(results|listings|members|companies|businesses|pros))?$/i.test(txt(e)) ||
      /^more (results|listings|members|companies)$/i.test(txt(e)));
  if (!el) return 'none';
  document.querySelectorAll('[data-hm-next]').forEach(e => e.removeAttribute('data-hm-next'));
  el.setAttribute('data-hm-next', '1');
  el.scrollIntoView({block: 'center'});
  return 'clicked:' + txt(el).slice(0, 20);
}"""

_PARKED = ("domain is for sale", "buy this domain", "this domain may be for sale", "parked free",
           "domain parking", "sedoparking", "hugedomains", "this domain is parked",
           "godaddy.com/forsale", "domain has expired", "is for sale!")

# "Show all 123" / "View all" link (ASP.NET GridView pagers, etc.)
_SHOW_ALL_JS = r"""() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
  for (const el of document.querySelectorAll('a, button, [role=button]')) {
    if (!vis(el)) continue;
    const t = txt(el);
    if (/^(show|view|display|see)\s+all(\s+\d[\d,]*)?(\s+(results|listings|members|companies|businesses|records|entries|items|pros|contractors))?\s*$/i.test(t)) {
      document.querySelectorAll('[data-hm-showall]').forEach(e => e.removeAttribute('data-hm-showall'));
      el.setAttribute('data-hm-showall', '1');
      el.scrollIntoView({block: 'center'});
      return 'found:' + t.slice(0, 40);
    }
  }
  return 'none';
}"""


class PageFetcher:
    """
    Fetches web pages at full speed.
    - Default: aiohttp (true async, no threads, 20 parallel)
    - Auto-detects blocked pages and retries with real browser
    - Browser mode: Playwright for JS-rendered pages, waits for content to load
    """

    def __init__(self, use_browser: bool = False, workers: int | None = None, on_rate_limit=None):
        self.use_browser = use_browser
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None   # ONE shared context = cookies kept between pages
        self._playwright = None
        self.browser_hosts: set[str] = set()   # sites that block plain HTTP -> always use the browser
        self._semaphore = asyncio.Semaphore(workers or config.CONCURRENT_WORKERS)
        self._cooldown_until = 0.0
        self.on_rate_limit = on_rate_limit   # callback(seconds) when the site says 429
        self._browser_lock = asyncio.Lock()
        self._browser_sem = asyncio.Semaphore(3)   # max browser tabs at the same time (RAM)
        self._aio_session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        if self.use_browser:
            await self._start_browser()
        self._aio_session = aiohttp.ClientSession(
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
            connector=aiohttp.TCPConnector(
                limit=config.CONCURRENT_WORKERS + 5,
                limit_per_host=config.CONCURRENT_WORKERS,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            ),
        )
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _start_browser(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.HEADLESS,
        )
        # Shared context: once a site's "Client Challenge" (Houzz) is passed, the
        # cookie is reused by every next page instead of solving it again each time.
        self._context = await self._browser.new_context(user_agent=config.USER_AGENT)

        async def _block_heavy(route):
            if route.request.resource_type in _SKIP_RESOURCES:
                await route.abort()
            else:
                await route.continue_()
        await self._context.route("**/*", _block_heavy)

    async def close(self):
        if self._aio_session:
            await self._aio_session.close()
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        await asyncio.sleep(0.25)

    async def _wait_cooldown(self):
        """If the site said 'Too Many Requests', every worker pauses until the cooldown ends."""
        while True:
            left = self._cooldown_until - asyncio.get_running_loop().time()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 1.0))

    async def fetch_simple(self, url: str) -> str | None:
        """True async HTTP GET — no threads, maximum speed. Backs off on HTTP 429."""
        rate_limited = 0
        attempt = 0
        while attempt < config.MAX_RETRIES:
            await self._wait_cooldown()
            try:
                async with self._aio_session.get(url) as resp:
                    if resp.status == 429:
                        # Site is rate-limiting us: pause ALL workers, then retry this URL
                        rate_limited += 1
                        if rate_limited > len(config.RATE_LIMIT_WAITS):
                            return None
                        wait = config.RATE_LIMIT_WAITS[rate_limited - 1]
                        try:
                            wait = max(wait, int(resp.headers.get("Retry-After", "0")))
                        except ValueError:
                            pass
                        loop = asyncio.get_running_loop()
                        if self._cooldown_until < loop.time() + wait:
                            self._cooldown_until = loop.time() + wait
                            if self.on_rate_limit:
                                self.on_rate_limit(wait)
                        continue
                    attempt += 1
                    if resp.status == 200:
                        html = await resp.text()
                        if _is_blocked(html):
                            # Bot-check page (e.g. Houzz "Client Challenge"): this site
                            # needs a real browser for every page from now on.
                            self.browser_hosts.add(urlparse(url).netloc.lower())
                            return None
                        return html
                    if resp.status == 403 or resp.status == 503:
                        self.browser_hosts.add(urlparse(url).netloc.lower())
                        return None
                    if resp.status >= 500:
                        await asyncio.sleep(0.3)
                        continue
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                attempt += 1
                if attempt >= config.MAX_RETRIES:
                    return None
                await asyncio.sleep(0.3)
        return None

    async def fetch_browser(self, url: str, scroll: bool = True) -> str | None:
        """Full browser fetch — waits for JS content, then scrolls down for lazy-loaded parts."""
        async with self._browser_sem:
            return await self._fetch_browser(url, scroll)

    async def _wait_until_settled(self, page, max_ms: int = 12000):
        """JavaScript sites fill the page after it 'loads': wait until links/text stop growing."""
        probe = "() => (document.querySelectorAll('a[href]').length) + '|' + (document.body ? document.body.innerText.length : 0)"
        try:
            last, same, waited = None, 0, 0
            while waited < max_ms:
                now = await page.evaluate(probe)
                if now == last:
                    same += 1
                    if same >= 2:
                        return
                else:
                    same = 0
                last = now
                await page.wait_for_timeout(700)
                waited += 700
        except Exception:
            return

    async def _scroll_and_get(self, page) -> str | None:
        """Scroll to the bottom in steps (some sites show details/website only after scrolling)."""
        try:
            await self._wait_until_settled(page)
            last = 0
            for _ in range(6):
                h = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
                await page.evaluate("(y) => window.scrollTo(0, y)", h)
                await page.wait_for_timeout(500)
                if h == last:
                    break
                last = h
            await page.wait_for_timeout(300)
            return await page.content()
        except Exception:
            return None

    async def _fetch_browser(self, url: str, scroll: bool = True) -> str | None:
        if not self._browser:
            async with self._browser_lock:
                if not self._browser:
                    await self._start_browser()

        page = None
        try:
            page = await self._context.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=config.REQUEST_TIMEOUT * 1000 * 2)
            if resp is not None and resp.status in (404, 410):
                return None   # page doesn't exist

            # Wait for real content (bot-check pages solve themselves in a few seconds).
            # Check every second for up to 15 seconds.
            for _ in range(15):
                await page.wait_for_timeout(1000)
                try:
                    html = await page.content()
                except Exception:
                    continue   # page is reloading itself (bot check passed) — wait
                if not _is_blocked(html) and len(html) > 1000:
                    if scroll:
                        html = await self._scroll_and_get(page) or html
                    return html

            # Final attempt — grab whatever is there (but never an error page)
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
            title = (await page.title()).lower()
            if _is_blocked(html) or len(html) < 1000 or any(
                    k in title for k in ("403", "404", "forbidden", "not found", "error")):
                return None
            return html

        except Exception:
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def fetch_ex(self, url: str, prefer_browser: bool = False) -> tuple[str | None, str]:
        """Like fetch(), but also says how the page was read: 'http' or 'browser'."""
        async with self._semaphore:
            host = urlparse(url).netloc.lower()
            if prefer_browser or self.use_browser or host in self.browser_hosts:
                return await self.fetch_browser(url), "browser"
            html = await self.fetch_simple(url)
            if html is not None:
                return html, "http"
            if host in self.browser_hosts:      # the site showed a bot-check -> real browser
                return await self.fetch_browser(url), "browser"
            return None, "http"

    async def resolve_redirect(self, url: str) -> str | None:
        """Where does a directory's own 'Website' redirect link (/out/123) finally land?"""
        try:
            async with self._semaphore:
                async with self._aio_session.get(url, allow_redirects=True) as resp:
                    final = str(resp.url)
                    if urlparse(final).netloc.lower() != urlparse(url).netloc.lower():
                        return final
                    text = await resp.text(errors="ignore")
            m = (re.search(r'http-equiv=["\']?refresh["\']?[^>]*url=([^"\'>]+)', text, re.I)
                 or re.search(r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', text, re.I)
                 or re.search(r'location\.replace\(\s*["\']([^"\']+)["\']', text, re.I))
            if m:
                return urljoin(final, m.group(1).strip())
        except Exception:
            return None
        return None

    async def check_site(self, url: str) -> tuple[bool, str]:
        """Does this website open, and is it a real site (not a parked / for-sale page)?"""
        try:
            async with self._aio_session.get(url, allow_redirects=True,
                                             timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status >= 400 and resp.status not in (403, 429, 503):
                    return False, ""
                text = (await resp.text(errors="ignore"))[:200000]
        except Exception:
            return False, ""
        if any(p in text.lower() for p in _PARKED):
            return False, ""
        return True, text

    async def iter_dynamic_pages(self, url: str, start_page: int = 1, max_steps: int = 2000):
        """
        Any list that changes only through JavaScript: clicks the next page number,
        'Next' or 'Load more', or scrolls down for endless lists - like a visitor.
        Yields (page_num, html) after every step that changed the list.
        Steps before `start_page` are done but not yielded (resume).
        """
        import logging
        log = logging.getLogger("hm.dynamic")
        log.info("iter_dynamic_pages: url=%s start_page=%d max_steps=%d", url[:80], start_page, max_steps)
        if not self._browser:
            async with self._browser_lock:
                if not self._browser:
                    await self._start_browser()
        page = await self._context.new_page()
        try:
            log.info("Browser page opened, navigating...")
            await page.goto(url, wait_until="domcontentloaded", timeout=config.REQUEST_TIMEOUT * 1000 * 2)
            for _ in range(15):
                await page.wait_for_timeout(1000)
                try:
                    html = await page.content()
                except Exception:
                    continue
                if not _is_blocked(html) and len(html) > 1000:
                    break
            log.info("Page loaded, %d chars", len(html))
            await self._wait_until_settled(page)

            # Try "Show all N" shortcut (ASP.NET GridView pagers, etc.)
            show_all = await page.evaluate(_SHOW_ALL_JS)
            log.info("Show all check: %s", show_all)
            if show_all.startswith("found"):
                before = await page.evaluate(_LINKS_SIG_JS)
                try:
                    await page.click('[data-hm-showall="1"]', timeout=5000)
                except Exception:
                    try:
                        await page.evaluate("() => { const e = document.querySelector('[data-hm-showall]'); if (e) e.click(); }")
                    except Exception:
                        pass
                changed = False
                for _ in range(60):          # up to 30 s for all results to load
                    await page.wait_for_timeout(500)
                    try:
                        now = await page.evaluate(_LINKS_SIG_JS)
                    except Exception:
                        continue             # page is reloading (full postback)
                    if now != before:
                        changed = True
                        break
                if changed:
                    await self._wait_until_settled(page)
                    yield max(1, start_page), await page.content()
                    return

            num = 1
            if num >= start_page:
                log.info("Yielding page %d (initial)", num)
                yield num, await page.content()
            for step in range(max_steps):
                before = await page.evaluate(_LINKS_SIG_JS)
                how = await page.evaluate(_CLICK_NEXT_JS, num)
                log.info("Step %d: CLICK_NEXT(%d) = %s", step, num, how[:40])
                if how.startswith("clicked"):
                    try:
                        await page.click('[data-hm-next="1"]', timeout=5000)
                    except Exception:
                        log.info("Playwright click failed, trying JS click")
                        await page.evaluate("() => { const e = document.querySelector('[data-hm-next]'); if (e) e.click(); }")
                else:
                    log.info("No click target, scrolling instead")
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                changed = False
                for _ in range(40):            # up to ~20 s for the next companies to appear
                    await page.wait_for_timeout(500)
                    try:
                        now = await page.evaluate(_LINKS_SIG_JS)
                    except Exception:
                        continue
                    if now != before:
                        changed = True
                        break
                if not changed:
                    log.info("No change detected after click/scroll — stopping")
                    return
                await page.wait_for_timeout(800)
                num += 1
                if num >= start_page:
                    log.info("Yielding page %d", num)
                    yield num, await page.content()
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def iter_click_pages(self, url: str, link_selector: str, start_page: int = 1):
        """
        For list pages that only change by clicking a page number (JavaScript,
        no real URL per page): open `url` in the browser, then click page 2, 3, ...
        exactly like a visitor would. Yields (page_num, html) for each page.
        Pages before `start_page` are clicked through but not yielded (resume).
        Stops when there is no link for the next page number.
        """
        if not self._browser:
            async with self._browser_lock:
                if not self._browser:
                    await self._start_browser()

        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=config.REQUEST_TIMEOUT * 1000 * 2)
            await page.wait_for_timeout(1500)   # let the site's script attach its click handler
            num = 1
            while True:
                if num >= start_page:
                    yield num, await page.content()
                nxt = page.locator(f'{link_selector}[data-page="{num + 1}"]').first
                if await nxt.count() == 0:
                    return
                prev = await page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href).join('|')")
                await nxt.click()
                try:
                    # Wait until the clicked page is the active one and the list has changed
                    await page.wait_for_function(
                        """([sel, n, prev]) => {
                            const a = document.querySelector(sel + '[data-page="' + n + '"]');
                            const active = a && (a.classList.contains('is-active') || a.classList.contains('active')
                                                 || a.getAttribute('aria-current') === 'page');
                            const now = [...document.querySelectorAll('a[href]')].map(x => x.href).join('|');
                            return active && now !== prev;
                        }""",
                        arg=[link_selector, num + 1, prev],
                        timeout=config.REQUEST_TIMEOUT * 1000 * 2,
                    )
                except Exception:
                    return   # page didn't change -> treat as the end
                await page.wait_for_timeout(300)
                num += 1
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def fetch(self, url: str, skip_browser_fallback: bool = False) -> str | None:
        """Smart fetch — tries fast HTTP, optionally retries with browser if blocked."""
        async with self._semaphore:
            host = urlparse(url).netloc.lower()
            if self.use_browser or host in self.browser_hosts:
                return await self.fetch_browser(url)

            html = await self.fetch_simple(url)
            if html is not None:
                return html

            # skip_browser_fallback (bulk profile pages): don't waste time on the
            # browser for pages that are simply dead — but if the site blocks plain
            # HTTP (bot check), the browser is the ONLY way, so always use it then.
            if skip_browser_fallback and host not in self.browser_hosts:
                return None

            # Simple HTTP failed or blocked — try with real browser (only for single/few pages)
            try:
                return await self.fetch_browser(url)
            except Exception:
                return None

    async def fetch_many(self, urls: list[str], callback=None, skip_browser_fallback: bool = True) -> list[tuple[str, str | None]]:
        """
        Fetch multiple URLs concurrently (10 at a time).
        skip_browser_fallback=True (default for bulk): Don't waste time on browser fallback
        for each failed URL — just mark as failed and move on fast.
        callback(index, url, html) is called as each one finishes.
        """
        async def _fetch_one(index: int, url: str):
            html = await self.fetch(url, skip_browser_fallback=skip_browser_fallback)
            if callback:
                callback(index, url, html)
            return (url, html)

        tasks = [_fetch_one(i, url) for i, url in enumerate(urls)]
        return await asyncio.gather(*tasks)
