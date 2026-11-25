"""
Tests for smart_directory (no internet needed).
Run:  python tests/test_smart_directory.py

Each case is a small page built the way real directories build them; the engine
must return the company's own website, or nothing when the page has none.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import smart_directory as sd  # noqa: E402

DIR = "https://www.somedirectory.org/members/alpha-builders-123"
FOOTER = """
<footer class="site-footer">
  <a href="https://www.bigsponsor.com">Our Sponsor</a>
  <a href="https://www.facebook.com/somedirectory">Facebook</a>
  <a href="https://www.growthzone.com">Powered by GrowthZone</a>
</footer>"""
SIMILAR = """
<section class="similar"><h2>Similar companies</h2><div class="cards">
  <div class="card"><h3><a href="/members/beta-roofing-1">Beta Roofing</a></h3><p>12 Main St, Austin, TX 78701</p>
     <a href="tel:5125550101">(512) 555-0101</a> <a href="https://betaroofing.com">Visit Website</a></div>
  <div class="card"><h3><a href="/members/gamma-homes-2">Gamma Homes</a></h3><p>99 Oak Ave, Austin, TX 78702</p>
     <a href="tel:5125550102">(512) 555-0102</a> <a href="https://gammahomes.com">Visit Website</a></div>
  <div class="card"><h3><a href="/members/delta-build-3">Delta Build</a></h3><p>5 Elm Rd, Austin, TX 78703</p>
     <a href="tel:5125550103">(512) 555-0103</a> <a href="https://deltabuild.com">Visit Website</a></div>
</div></section>"""


def page(body: str) -> str:
    return f"""<html><head><title>Alpha Builders | Some Directory</title></head><body>
<header class="site-header"><nav><a href="/">Home</a><a href="/about">About</a>
<a href="https://www.chamberpartner.org">Partner</a></nav></header>
<main><h1>Alpha Builders</h1>{body}</main>{SIMILAR if 'NO_SIMILAR' not in body else ''}{FOOTER}</body></html>"""


CASES = [
    # (description, html body, expected website ('' = none))
    ("labelled button", '<a class="btn" href="https://alphabuilders.com">Visit Website</a>', "https://alphabuilders.com"),
    ("href without http", '<b>Website:</b> <a href="www.alphabuilders.com">www.alphabuilders.com</a>', "http://www.alphabuilders.com"),
    ("plain text only", '<p>Phone: 555-1234</p><p>Website: alphabuilders.com</p>', "http://alphabuilders.com"),
    ("dt/dd label", '<dl><dt>Phone</dt><dd>555</dd><dt>Website</dt><dd><a href="https://ab-tx.com/">ab-tx.com</a></dd></dl>', "https://ab-tx.com/"),
    ("redirect wrapper", '<a href="/out?url=https%3A%2F%2Falphabuilders.com%2F&id=9">Website</a>', "https://alphabuilders.com/"),
    ("onclick button", "<button onclick=\"window.open('https://alphabuilders.com')\">Visit Website</button>", "https://alphabuilders.com"),
    ("globe icon only", '<a href="https://alphabuilders.net"><i class="fa fa-globe"></i></a>', "https://alphabuilders.net"),
    ("maps + website", '<a href="https://maps.google.com/?q=1+Main">Get Directions</a> <a href="https://alpha-b.com">alpha-b.com</a>', "https://alpha-b.com"),
    ("only facebook", '<a href="https://www.facebook.com/alphabuilders">Facebook</a>', ""),
    ("no website, similar cos have", "<p>Call us: 555-1111</p>", ""),
    ("own site + similar cos", '<a href="https://alphabuilders.com">Website</a>', "https://alphabuilders.com"),
    ("e-mail written as href", '<a href="info@alphabuilders.com">info@alphabuilders.com</a> NO_SIMILAR', ""),
    ("JSON-LD only", '<script type="application/ld+json">{"@type":"GeneralContractor","name":"Alpha Builders",'
                     '"url":"https://www.alphabuilders.com"}</script>', "https://www.alphabuilders.com"),
]


def run():
    bad = 0
    for desc, body, want in CASES:
        r = sd.extract_profile(page(body), DIR, "Alpha Builders", {"bigsponsor.com", "chamberpartner.org"})
        got = r["website"]
        ok = got == want
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} {desc:32} -> {got!r}" + ("" if ok else f"   (expected {want!r}, cands {r['candidates'][:3]})"))
    r = sd.extract_profile(page('<a class="btn" href="/go/123">Visit Website</a>'), DIR, "Alpha Builders")
    ok = r["internal_website"] == "https://www.somedirectory.org/go/123" and not r["website"]
    bad += not ok
    print(f"{'OK ' if ok else 'BAD'} {'internal redirect button':32} -> {r['internal_website']!r}")

    # List page: cards, a card-level website, the A-Z bar and page numbers
    lst = """<html><body><main><div class="az"><a href="/members/char/a">A</a><a href="/members/char/b">B</a>
    <a href="/members/char/c">C</a></div><div class="results">""" + "".join(
        f"""<div class="item"><h3><a href="/members/co-{i}">Company {n}</a></h3><p>{i} Main St, Dallas, TX 7520{i}</p>
        <a href="tel:21455501{i:02d}">call</a>{'<a href="https://company' + n.lower() + '.com">Website</a>' if i % 2 else ''}</div>"""
        for i, n in enumerate(["Alpha", "Bravo", "Charlie", "Delta", "Echo"], 1)) + """</div>
    <div class="pgsel"><span>1</span> <a href="/members?pg=2">2</a> <a href="/members?pg=3">3</a> <a href="/members?pg=2">&raquo;</a></div>
    </main></body></html>"""
    res = sd.analyze_list(lst, "https://www.somedirectory.org/members")
    names = [x["name"] for x in res["records"]]
    webs = [x["website"] for x in res["records"]]
    nxt = sd.find_next_page(lst, "https://www.somedirectory.org/members", len(names))[0]
    ok = (names == ["Company Alpha", "Company Bravo", "Company Charlie", "Company Delta", "Company Echo"]
          and webs == ["https://companyalpha.com", "", "https://companycharlie.com", "", "https://companyecho.com"]
          and nxt == "https://www.somedirectory.org/members?pg=2")
    bad += not ok
    print(f"{'OK ' if ok else 'BAD'} {'list page':32} -> {len(names)} companies, next={nxt}"
          + ("" if ok else f"\n    names={names}\n    webs={webs}"))

    print(f"\n{'ALL PASSED' if not bad else str(bad) + ' FAILED'}")
    return bad


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
