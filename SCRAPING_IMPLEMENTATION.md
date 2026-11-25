# HM Data Extractor: Directory Scraping, kya implement hua

Is file mein likha hai ke directory link dene par app companies ki **Website** kaise nikalta hai.
Sab se upar naya hissa hai (2026-11-23). Neeche purane notes (2026-11-18/19) hain.

---

## 0. Naya general engine (2026-11-23)

### Masla
Pehle har nayi directory ke liye alag code likhna parta tha (Houzz, theconstructionusa, GeoDirectory).
Jo directory list mein nahi hoti, us par andaze wala parser chalta tha jo menu links ya "pehla bahar ka link" utha leta tha.

### Ab kya hota hai
Nayi file [smart_directory.py](smart_directory.py) kisi bhi directory ko page ki **banawat** se samajhti hai, site ka naam dekh kar nahi:

| Qadam | Kaise |
|---|---|
| Companies dhoondna | Page par dohraye gaye blocks (cards / rows / list items) dhoondta hai. Menu, header, footer, A-Z bar, page numbers aur "hub" pages ke links reject. |
| Har card se | Naam, profile link (directory ke andar), aur agar card par hi website ho to woh. |
| Website card par hai | Fast Mode mein wahin se le li jati hai, profile nahi khulta (tez). |
| Website andar hai | Company ka page khol kar har bahar ke link ko **score** deta hai: "Website / Visit Website" label, link ka text domain jaisa (www.abc.com), domain company ke naam se milta hai, globe icon, JSON-LD, "Website:" plain text. |
| Kabhi website nahi banta | Facebook/LinkedIn/maps, directory ka apna domain, sponsor/ads, header/footer, har page par aane wale links, "Similar companies" section mein doosri companies ke buttons. |
| Redirect link | `/out?url=https%3A%2F%2Fabc.com` ya Clutch `r.clutch.co/redirect?...&u=...` khol kar asal website. Directory ka apna `/go/123` button ho to app us link ko follow karke dekhta hai ke kahan jata hai. |
| JavaScript pages | Saada download mein companies/website na hon to browser mein kholta hai, neeche tak scroll karta hai, aur jab tak page bharna band na ho intezar karta hai. Pehle 3 pages se seekh leta hai ke is directory ko browser chahiye ya nahi. |
| Agla page | rel=next, "Next / » / ›" button, page number (2, 3 ...), `?page=N`, `/page/N/`. Agar URL se page na badle (JavaScript pagination), "Load more" ho, ya "20 of 150 results" likha ho to browser mein click / scroll karta hai. |
| Email se website | Agar directory website na dikhaye lekin business email ho (info@abcbuilders.com) to abcbuilders.com, **sirf** jab woh site khule aur usi company ki ho. [config.py](config.py) mein `INFER_WEBSITE_FROM_EMAIL = False` se band ho sakta hai. |

### CSV mein naya column
**Website Source** (aakhri column): website kahan se aayi.
Is ki values: `listing page`, `company page`, `company page (browser)`, `website button (redirect)`, `business e-mail domain (site checked)`.
Khali website = directory ne us company ki website dikhayi hi nahi.

### Purane tested raste
Houzz (rawDomain JSON) aur theconstructionusa (Contact information box + page-number click) waise hi chalte hain.
GeoDirectory sites ka Website field bhi waise hi.

### Kahan test hua (dataset)
45 directories try ki gayin. Un ke pages scratch folder mein save kar ke engine un par baar baar chalaya gaya.

| Nateeja | Kitni | Misaal |
|---|---|---|
| Engine ne companies + websites nikali | 17 | GrowthZone/ChamberMaster chambers, builders associations, Connections, Wild Apricot, Atlas, Clutch, Expertise, Jasmine, HomeGuide, Houzz, theconstructionusa, exchange.construction |
| Site ne block kiya (Cloudflare / region / 403 / 429) | 19 | GrowthZone ki nayi chambers, YellowPages, BBB, BuildZoom, Thumbtack, Angi |
| List page nahi tha (hub page / login / link band) | 9 | CMHA, ATSSA, CALSSA |

127 list/profile checks (golden), 15 synthetic tests ([tests/test_smart_directory.py](tests/test_smart_directory.py)), aur 10 directories par live run.
Live run mein koi ghalat website nahi aayi. Jo khali rahin, un ke page par website thi hi nahi (check kiya gaya).

Test chalane ke liye: `python tests/test_smart_directory.py`

### Hadood (jo ab bhi nahi hota)
- **Bot-check wali sites:** Cloudflare "Just a moment..." (GrowthZone ki nayi chambers, YellowPages, BBB) aur region-block (BuildZoom) wali sites nahi khultin. App bot-check bypass nahi karta, sirf saaf message deta hai.
- **Login wali directories:** jo directories login ke baad dikhti hain, woh nahi chaltin.
- **Website ka na hona:** agar directory kisi company ki website dikhati hi nahi (aur email se bhi pakka na ho) to khana khali rehta hai. Andaze se koi website nahi bhari jati.
- [main.py](main.py) (command-line) bhi naya engine use karta hai, lekin JavaScript-click wali pagination aur "browser chahiye ya nahi" wala seekhna sirf app ([app.py](app.py)) mein hai.
- **Directory ka apna email** ab subdomain par bhi hat jata hai (pehle `business.tacomachamber.org` wali directory ka `info@tacomachamber.org` har company ke saath aa jata tha).

---

## 1. Masla kya tha

| Site | Pehle kya ho raha tha |
|---|---|
| `theconstructionusa.com/directory/?secteur=...&region=...` | App company links ki jagah menu ke 5 links (login, register, write-a-review…) utha raha tha. "Next page" galti se `/register-my-business/` par ja raha tha. Kisi company ke andar nahi jata tha. |
| `exchange.construction/contractors/category/general-contractor/` | 55 category links (Roofing, Concrete…) aa rahe the, company links nahi. Company page par website ki jagah comment form ka text aa raha tha: `https://Save my name, email, and website...` |

Wajah: dono sites ke liye app ke paas apna parser nahi tha, to generic andaze wala parser chal raha tha.

---

## 2. Poora flow (link dene se CSV tak)

```
Directory URL
   │
   ├─ 1. Fast Mode + list page par cards (naam + Website button)?
   │      haan → wahin se naam + website (extract_cards). Andar nahi jata.
   │
   ├─ 2. List pages se company links jama karo  (_pages_stage)
   │      ├─ Houzz                      → fi=0,15,30… (_houzz_pages_stage)
   │      ├─ Click-pagination site      → browser mein 2,3,4… click (_click_pages_stage)   ← NAYA
   │      └─ Baaki sab                  → URL se agla page (find_next_page)
   │
   │      Har page par links (find_listing_links):
   │      ├─ Site ka apna parser   (Houzz, Yelp, YellowPages, theconstructionusa ← NAYA)
   │      ├─ GeoDirectory site?    → .geodir-post cards                          ← NAYA
   │      └─ Generic parser
   │
   ├─ 3. Har company page kholo, 10 parallel  (_profiles_stage → extract_all)
   │      ├─ theconstructionusa → "Contact information" box   ← NAYA
   │      ├─ GeoDirectory       → geodir-field-website        ← NAYA
   │      ├─ Houzz              → rawDomain JSON
   │      └─ Baaki              → labeled field / JSON-LD / "Website" button
   │
   └─ 4. (Sirf Full Mode) company ki website khol kar Contact page + email
          Fast Mode mein yeh step nahi hota.

   → output/extracted_data.csv + .xlsx
```

---

## 3. Kya add kiya (file ke hisaab se)

### [directory_parser.py](directory_parser.py)

| Cheez | Kaam |
|---|---|
| [`_parse_constructionusa`](directory_parser.py#L59) | Page ke sirf woh links leta hai jo `/company/<slug>` par jate hain, aur duplicate hatata hai. Har page par 24. |
| [`_parse_geodirectory`](directory_parser.py#L71) | WordPress **GeoDirectory** plugin wali sites ke liye. `.geodir-loop-container .geodir-post` card ke andar ka pehla profile link leta hai. `/category/`, `/tag/`, `/author/`, `/page/` aur `#reviews` chhor deta hai. |
| [`SITE_PARSERS`](directory_parser.py#L162) | `theconstructionusa.com` ko is list mein add kiya. |
| [`CLICK_PAGINATION`](directory_parser.py#L168) + [`click_pagination_selector`](directory_parser.py#L173) | Un sites ki list jahan page sirf number par **click** karne se badalta hai, URL se nahi. Value page-number link ka CSS selector hai, jaise `.lc-pagination__lien`. |
| [`find_listing_links`](directory_parser.py#L191) | Kisi site ka apna parser na ho aur page par GeoDirectory cards hon, to GeoDirectory parser chalta hai (domain koi bhi ho). |

### [browser.py](browser.py)

| Cheez | Kaam |
|---|---|
| [`PageFetcher.iter_click_pages`](browser.py#L229) | Page ko Playwright (headless Chrome) mein kholta hai, har page ka HTML deta hai, phir `data-page="N+1"` wala link click karta hai. Har click ke baad intezar karta hai jab tak (a) woh page number active na ho jaye aur (b) page ke links badal na jayen. Agla number na mile ya page na badle to ruk jata hai. `start_page` resume ke liye hai: pehle ke pages click hote hain, lekin dobara nahi gine jate. |

### [extractor.py](extractor.py)

| Cheez | Kaam |
|---|---|
| [`extract_constructionusa_profile`](extractor.py#L360) | "Contact information" box ki har row (`.lc-coord__label`) parhta hai. **Website** ke liye us row ke `<a>` ka asal `href` leta hai (`https://acsfixit.com/`), dikhne wala text nahi. Email `mailto:` se, Phone row se, State jhande wali line se ("Houston, TX" → TX). |
| [`extract_all`](extractor.py#L395) | URL mein `theconstructionusa.com/company/` ho to upar wala function chalta hai. Fast aur Full dono mode ka record banta hai. |
| [GeoDirectory website](extractor.py#L433) | Page par `geodir-field-*` ho to website **sirf** `.geodir-field-website a[href]` se aati hai. Yeh field na ho to website khali rehti hai, koi aur bahar ka link nahi liya jata. |
| [`extract_business_name`](extractor.py#L199) | JSON-LD wale naam se `&amp;` jaisi HTML coding hatata hai (`Kitchen &amp; Bath` → `Kitchen & Bath`). |

### [app.py](app.py)

| Cheez | Kaam |
|---|---|
| [`_pages_stage`](app.py#L549) | URL click-pagination wali site ka ho to `_click_pages_stage` chalata hai. |
| [`_click_pages_stage`](app.py#L596) | `iter_click_pages` se ek ek page leta hai, company links jama karta hai, aur har page ke baad progress save karta hai. `Max` aur `Pages` limit maanta hai. Stop dabane par jis page tak pohncha tha, Start par wahin se continue karta hai. |

---

## 4. Har site ki tafseel

### theconstructionusa.com

| | |
|---|---|
| Company link | `https://www.theconstructionusa.com/company/<naam>-<shehar>` |
| Per page | 24 |
| Pagination | URL se **nahi** chalti. `?p=2`, `&page=2`, `&paged=2`, `&pg=2`, `/page/2/` sab try kiye, har baar page 1 hi aaya. Site ki JS (`public.js`) page number click par background AJAX call (`lc_repertoire_grille`) karti hai. Woh call seedha chalane ki permission nahi mili, is liye browser mein asli button click hota hai, jaise visitor karta hai. |
| Website | Contact information → `Website` row → `<a class="lc-coord__valeur" href="...">` |
| Total (GC + Texas) | 2,346 companies, 98 pages |

### exchange.construction (GeoDirectory)

| | |
|---|---|
| Company link | `https://exchange.construction/contractors/<category>/<naam>/`. Kuch companies `renovation/` category mein bhi hoti hain, lekin woh bhi isi list mein dikhti hain. |
| Per page | 18 |
| Pagination | URL se chalti hai: `/page/2/` … `/page/45/` (`a.next.page-link`). Browser ki zaroorat nahi. |
| Website | "Online Presence" → `div.geodir-field-website a[href]` |
| Total (General Contractor) | 807 companies, 45 pages |

---

## 5. Data ke usool

1. **Website hamesha site ke apne "Website" field/button se aati hai.** Kisi aur link se andaza nahi lagaya jata.
2. **Website na ho to khana khali.** Facebook, Instagram, sponsor banner ya directory ka apna link website nahi bharta. Misaal: Brothers Construction ke page par sirf Facebook tha, to Website khali hai, aur yeh sahi hai.
3. Link ka **asal href** liya jata hai, dikhne wala text nahi.
4. Ek company ke kayi links hon (tasveer, naam, `#reviews`), to sirf ek rakha jata hai.
5. Directory ka apna email (footer wala) hata diya jata hai.

---

## 6. Test ke nateeje

**theconstructionusa** (Fast Mode, GC + Texas):
- Browser click se page 1, 2, 3, har page par 24 **nayi** companies.
- 5 companies ke CSV mein sab ki website sahi:

```
ACS Commercial Services ... LLC  https://acsfixit.com/        TX
Arion Blue                       http://www.arionblue.com     TX
AVI-SPL                          https://avispl.com           TX
Austin Tops                      https://www.austintops.com   TX
Barnett Signs                    https://www.barnettsigns.com TX
```

**exchange.construction** (Fast Mode, General Contractor):
- Page 1 aur page 2, dono par 18 nayi companies.
- 22 companies mein se 21 ki website aayi. 1 (Brothers Construction) ke page par Website button hi nahi hai.

```
W.L. McNatt & Company   https://www.wlmcnatt.com
Septagon Construction   https://www.septagon.com
Harvey Construction     https://www.harveyconstruction.com
Brothers Construction   (khali — page par sirf Facebook)
```

Test ka output alag scratch folder mein gaya tha, `output/extracted_data.csv` ko nahi chhua.

---

## 7. App kaise chalayein

| Option | Kya karein |
|---|---|
| **Fast Mode** | ✅ Tick karein, agar sirf naam + website chahiye. |
| **Use Browser (for JS-heavy sites)** | ❌ Tick na karein. Click wali pagination ke liye app browser khud khol leta hai. Tick karne se har company page bhi Chrome mein khulega, aur kaam bohat dheema ho jayega. Sirf tab use karein jab kisi site par data khali aaye ya bot-check ho. |
| **Max / Pages** | Saara data chahiye to `All`. |
| **Stop & Save** | Data save ho jata hai. Wohi URL dobara Start karein → "Yes", to wahin se continue hota hai. |

**Load:** headless Chrome ~200–300 MB RAM leta hai (tasveerein, fonts aur video band hain). Company pages seedha download hote hain, 10 ek saath. Site "Too Many Requests" kahe to app khud ruk kar intezar karta hai. Aur halka chalana ho to [config.py](config.py) mein `CONCURRENT_WORKERS` kam kar dein.

**Run chalte waqt CSV:** `output/extracted_data.csv` ko **Excel mein seedha na kholein**. Excel file lock kar deta hai aur app save nahi kar pata, to run error de kar ruk jata hai. Data `.progress` mein mehfooz rehta hai, aur dobara Start karke resume ho jata hai. Data dekhna ho to file ki copy (Ctrl+C, Ctrl+V) bana kar copy kholein, ya Notepad mein kholein.

---

## 8. Nayi directory site kaise add karein

1. **List page ka HTML save karein**, aur mojooda `find_listing_links` / `find_next_page` us par chala kar dekhein ke kya aa raha hai.
2. **Company link ka pattern** dhoondein (jaise `/company/…`), ya woh card (`div`) jis mein har company hoti hai.
3. **Pagination test karein:** page 2 ka URL kholein aur check karein ke companies waqai badli hain.
   - Badal gayin → `find_next_page` kaafi hai.
   - Nahi badlin (JS se page badalta hai) → site ko `CLICK_PAGINATION` mein uske page-number link ke selector ke saath add karein. Shart: us link par `data-page="N"` ho aur active page par `is-active`/`active` class ya `aria-current="page"` ho.
4. **Company page par Website field** dhoondein aur `extract_all` mein us field ka exact `href` lein. Field na ho to `""`.
5. **Parser** `SITE_PARSERS` mein add karein. Site GeoDirectory par bani ho to kuch karne ki zaroorat nahi, khud chal jayega.
6. **Test:** pehle saved HTML par, phir chhota live test (2 pages, ~5–20 companies). Output scratch folder mein rakhein, taake asli CSV overwrite na ho.

---

## 9. Hadood

- **theconstructionusa:** pagination ke liye browser zaroori hai. Site apni pagination ka class ya `data-page` badal de to `CLICK_PAGINATION` ka selector update karna hoga.
- **exchange.construction:** State column zyada tar khali, kyunki page par state alag field mein nahi hoti.
- **GeoDirectory parser** sirf exchange.construction par test hua hai. Usi plugin wali doosri sites par bhi shayad chal jaye, lekin pakka nahi.
- **[main.py](main.py)** (command-line version) mein yeh changes nahi hain, sirf GUI ([app.py](app.py)) mein hain.
- "New Window" (do link ek saath) wala feature try karke **wapas hata diya gaya**. App ab bhi ek waqt mein ek URL chalata hai aur `output/extracted_data.csv` mein save karta hai.
