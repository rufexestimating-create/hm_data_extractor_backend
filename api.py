"""
HM Data Extractor — FastAPI Backend
Wraps existing extraction logic with REST API + Server-Sent Events for real-time progress.
"""

import asyncio
import json
import os
import re
import time
import hashlib
from urllib.parse import urlparse
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from fastapi.responses import StreamingResponse

from browser import PageFetcher
from directory_parser import find_listing_links, houzz_page_url, click_pagination_selector
import smart_directory as sd
from extractor import extract_all
from exporter import save_csv, save_excel
from website_crawler import enrich_records_with_websites
import config
import supabase_db

app = FastAPI(title="HM Data Extractor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://187.124.96.140:8090",
        "http://187.124.96.140:3001",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractionRequest(BaseModel):
    url: str
    fast: bool = True
    max_pages: int | None = None


# Global state for the running extraction
_state: dict | None = None
_logs: list[str] = []
_progress: float = 0
_status: str = "idle"
_stop_event: asyncio.Event | None = None
_results: list[dict] = []
_stats: dict = {}
_batch_id: str = ""
_fast_mode: bool = True


def _log(msg: str):
    _logs.append(msg)


def _backup_name(url: str) -> str:
    parsed = urlparse(url)
    site = parsed.netloc.replace("www.", "").split(".")[0]
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    desc = "_".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "data")
    desc = re.sub(r"[^a-zA-Z0-9_-]", "_", desc)[:40]
    date = time.strftime("%Y%m%d_%H%M")
    return f"{site}_{desc}_{date}"


@app.get("/api/status")
async def get_status():
    return {
        "status": _status,
        "progress": _progress,
        "log_count": len(_logs),
        "result_count": len(_results),
        "stats": _stats,
    }


@app.get("/api/logs")
async def get_logs(after: int = 0):
    return {"logs": _logs[after:], "total": len(_logs)}


@app.get("/api/results")
async def get_results():
    return {"results": _results, "stats": _stats}


@app.get("/api/stream")
async def stream_progress():
    """SSE endpoint for real-time progress updates."""
    async def event_generator():
        last_log = 0
        last_progress = -1
        while True:
            if _status == "idle":
                yield {"event": "status", "data": json.dumps({"status": "idle"})}
                break

            new_logs = _logs[last_log:]
            current_progress = _progress

            if new_logs or current_progress != last_progress:
                yield {
                    "event": "update",
                    "data": json.dumps({
                        "status": _status,
                        "progress": current_progress,
                        "logs": new_logs,
                        "result_count": len(_results),
                        "stats": _stats,
                        "batch_id": _batch_id,
                    })
                }
                last_log = len(_logs)
                last_progress = current_progress

            if _status in ("done", "stopped", "error"):
                yield {
                    "event": "complete",
                    "data": json.dumps({
                        "status": _status,
                        "progress": 100 if _status == "done" else _progress,
                        "result_count": len(_results),
                        "stats": _stats,
                        "batch_id": _batch_id,
                    })
                }
                break

            await asyncio.sleep(0.3)

    return EventSourceResponse(event_generator())


@app.post("/api/extract")
async def start_extraction(req: ExtractionRequest):
    global _status
    if _status == "running":
        raise HTTPException(400, "Extraction already running")

    asyncio.create_task(_run_extraction(req.url, req.fast, req.max_pages))
    return {"message": "Extraction started", "url": req.url}


@app.post("/api/stop")
async def stop_extraction():
    global _stop_event
    if _stop_event:
        _stop_event.set()
        return {"message": "Stop signal sent"}
    raise HTTPException(400, "No extraction running")


@app.get("/api/batches")
async def list_batches(mode: str | None = None,
                       date_from: str | None = None,
                       date_to: str | None = None):
    """List extraction batches with optional filters."""
    try:
        batches = supabase_db.get_batches(mode=mode, date_from=date_from, date_to=date_to)
        return {"batches": batches}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/data")
async def get_data(batch_id: str | None = None, limit: int = 50, offset: int = 0,
                   mode: str | None = None,
                   date_from: str | None = None,
                   date_to: str | None = None,
                   search: str | None = None):
    """Fetch records with filters and pagination."""
    try:
        records = supabase_db.get_records(
            batch_id=batch_id, limit=limit, offset=offset,
            mode=mode, date_from=date_from, date_to=date_to, search=search)
        total = supabase_db.get_total_count(batch_id=batch_id, mode=mode,
                                             date_from=date_from,
                                             date_to=date_to, search=search)
        return {"records": records, "count": len(records), "total": total}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/export-csv")
async def export_csv_download(batch_id: str | None = None,
                              mode: str | None = None,
                              date_from: str | None = None,
                              date_to: str | None = None,
                              search: str | None = None):
    """Download filtered records as CSV file."""
    try:
        csv_str = supabase_db.export_csv_string(
            batch_id=batch_id, mode=mode, date_from=date_from,
            date_to=date_to, search=search)
        if not csv_str:
            raise HTTPException(404, "No records found")
        filename = f"extracted_{batch_id}.csv" if batch_id else "extracted_all.csv"
        return StreamingResponse(
            iter([csv_str]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/batch/{batch_id}")
async def delete_batch(batch_id: str):
    """Delete all records for a batch."""
    try:
        count = supabase_db.delete_batch(batch_id)
        return {"deleted": count, "batch_id": batch_id}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/open-output")
async def open_output():
    import subprocess
    output_path = os.path.abspath(config.OUTPUT_DIR)
    os.makedirs(output_path, exist_ok=True)
    subprocess.Popen(f'explorer "{output_path}"')
    return {"message": "Opened", "path": output_path}


def _norm_name(name: str) -> str:
    return "name:" + re.sub(r"[^a-z0-9]", "", (name or "").lower())


async def _run_extraction(url: str, fast: bool, max_pages: int | None):
    global _state, _logs, _progress, _status, _stop_event, _results, _stats, _batch_id, _fast_mode

    _logs.clear()
    _results.clear()
    _stats.clear()
    _progress = 0
    _status = "running"
    _stop_event = asyncio.Event()
    _batch_id = supabase_db.generate_batch_id()
    _fast_mode = fast

    _log(f"Target: {url}")
    if fast:
        _log("FAST MODE: Name, Website, Email, Phone, State")
    else:
        _log("DEEP MODE: All Data + Website Crawl for Emails")

    is_houzz = "houzz.com" in url
    workers = config.HOUZZ_WORKERS if is_houzz else config.CONCURRENT_WORKERS

    try:
        async with PageFetcher(use_browser=False, workers=workers) as fetcher:
            deep_crawl = not fast

            # Step 1: Collect companies from list pages
            _log("Step 1: Crawling directory pages...")
            cards, chrome = await _pages_stage(fetcher, url, is_houzz, max_pages)

            if _stop_event.is_set():
                _status = "stopped"
                _save_results(url)
                return

            if not cards:
                _log("No companies found on this page.")
                _status = "error"
                return

            n_web = sum(1 for c in cards if c.get("website"))
            _log(f"Found {len(cards)} companies ({n_web} show website on list page)")

            # Step 2: Visit company pages for website
            _log(f"Step 2: Opening {len(cards)} company pages ({workers} at a time)...")
            await _profiles_stage(fetcher, cards, url, fast, chrome)

            if _stop_event.is_set():
                _status = "stopped"
                _save_results(url)
                return

            # Step 3: Deep crawl websites
            if deep_crawl:
                todo = [r for r in _results if r.get("website")]
                if todo:
                    _log(f"Step 3: Deep-crawling {len(todo)} websites for emails (10 parallel)...")
                    _progress = 70

                    def on_done(done, total, rec, found_emails):
                        global _progress
                        name = rec.get("name") or "Company"
                        cp = rec.get("contact_page") or rec.get("website_status") or ""
                        mail = ", ".join(found_emails[:2]) if found_emails else "no email"
                        _log(f"  [{done}/{total}] {name} | {cp} | {mail}")
                        _progress = 70 + 25 * done / max(total, 1)

                    await enrich_records_with_websites(
                        todo, concurrency=10, progress_callback=on_done,
                        browser_fetch=fetcher.fetch_browser)

            # Save
            _save_results(url)
            _compute_stats()
            _progress = 100
            _status = "done"
            _log(f"Done! {len(_results)} records extracted and saved.")

    except asyncio.CancelledError:
        _status = "stopped"
        _save_results(url)
    except Exception as e:
        _log(f"Error: {e}")
        _status = "error"
        _save_results(url)


async def _pages_stage(fetcher, url, is_houzz, max_pages):
    cards = []
    chrome = set()
    seen_keys = set()
    seen_pages = set()
    page_url = url
    page_num = 1

    while page_url and page_url not in seen_pages:
        if _stop_event and _stop_event.is_set():
            break
        seen_pages.add(page_url)
        _log(f"  Page {page_num}: {page_url[:80]}...")

        html, via = await fetcher.fetch_ex(page_url)
        if not html and via == "http":
            html = await fetcher.fetch_browser(page_url)
        if not html:
            _log(f"  Failed to load Page {page_num}")
            break

        if is_houzz:
            page_cards = [{"name": "", "profile_url": l, "website": ""}
                          for l in find_listing_links(html, page_url)]
        else:
            res = await asyncio.to_thread(sd.analyze_list, html, page_url, chrome)
            if not res["records"] and via == "http":
                bhtml = await fetcher.fetch_browser(page_url)
                if bhtml:
                    res2 = await asyncio.to_thread(sd.analyze_list, bhtml, page_url, chrome)
                    if res2["records"]:
                        res, html = res2, bhtml
            chrome |= res["chrome_hosts"]
            page_cards = res["records"]

        added = 0
        for c in page_cards:
            key = c["profile_url"] or ("name:" + (c.get("name") or "").lower())
            if key not in seen_keys:
                seen_keys.add(key)
                cards.append(c)
                added += 1

        _log(f"  Page {page_num}: {added} new companies (Total: {len(cards)})")
        _progress = min(15, 15 * page_num / max(max_pages or 10, 1))

        if max_pages and page_num >= max_pages:
            _log(f"  Reached page limit ({max_pages})")
            break

        if is_houzz:
            from directory_parser import find_next_page
            next_url = find_next_page(html, page_url, listings_count=added)
        else:
            next_url = sd.find_next_page(html, page_url, len(page_cards))[0]

        if next_url and next_url not in seen_pages and (added or page_num == 1):
            page_url = next_url
            page_num += 1
            await asyncio.sleep(config.DELAY_BETWEEN_REQUESTS)
        else:
            _log(f"  Last page reached (Page {page_num})")
            break

    return cards, chrome


async def _profiles_stage(fetcher, cards, list_url, fast, chrome):
    global _progress
    by_url = {c["profile_url"]: c for c in cards if c["profile_url"]}
    done_count = 0
    total = len(cards)

    # Cards with website already (fast mode)
    direct = [c for c in cards if fast and c.get("website")]
    need = [c for c in cards if not (fast and c.get("website"))]

    for c in direct:
        rec = {"name": c["name"], "page_url": c["profile_url"] or list_url,
               "website": c["website"], "emails": list(c.get("emails") or []),
               "state": c.get("state", ""), "license": "",
               "website_source": "listing page" if c["website"] else ""}
        if fast:
            rec["phone"] = c.get("phone", "")
        else:
            rec.update({"phones": [c["phone"]] if c.get("phone") else [], "address": "", "socials": {}})
        _results.append(rec)
        done_count += 1

    if direct:
        _log(f"  {len(direct)} companies already show website on list page")

    async def work(c):
        global _progress
        nonlocal done_count
        if _stop_event and _stop_event.is_set():
            return
        if not c["profile_url"]:
            rec = {"name": c["name"], "page_url": list_url, "website": c.get("website", ""),
                   "emails": list(c.get("emails") or []), "state": c.get("state", ""),
                   "license": "", "website_source": "listing page" if c.get("website") else ""}
            if fast:
                rec["phone"] = c.get("phone", "")
            else:
                rec.update({"phones": [c["phone"]] if c.get("phone") else [], "address": "", "socials": {}})
            _results.append(rec)
            done_count += 1
            return

        html = await fetcher.fetch(c["profile_url"])
        if not html:
            html = await fetcher.fetch_browser(c["profile_url"])
        if not html:
            done_count += 1
            return

        rec = await asyncio.to_thread(extract_all, html, c["profile_url"], fast, c.get("name", ""), chrome)
        src = "company page" if rec.get("website") else ""
        if not rec.get("website") and c.get("website"):
            rec["website"], src = c["website"], "listing page"
        if rec.get("website") and not sd.valid_website(rec["website"]):
            rec["website"], src = "", ""
        rec["website_source"] = src
        if not rec.get("name"):
            rec["name"] = c.get("name", "")
        _results.append(rec)
        done_count += 1
        _log(f"  [{done_count}/{total}] {rec.get('name', '—')}  ->  {rec.get('website') or '— (no website)'}")
        _progress = 15 + 55 * done_count / max(total, 1)

    if need:
        await asyncio.gather(*(work(c) for c in need))


def _save_results(url: str):
    if not _results:
        return
    try:
        csv_path = save_csv(_results)
        xlsx_path = save_excel(_results)
        _log(f"CSV:   {csv_path}")
        _log(f"Excel: {xlsx_path}")
    except Exception as e:
        _log(f"CSV/Excel save failed (file may be open): {e}")

    try:
        backup_dir = os.path.join(config.OUTPUT_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        bname = _backup_name(url)
        backup_path = save_csv(_results, os.path.join("backups", bname))
        _log(f"Backup: {backup_path}")
    except Exception as e:
        _log(f"Backup save failed: {e}")

    try:
        mode = "fast" if _fast_mode else "deep"
        count = supabase_db.save_records(_results, url, mode, _batch_id)
        _log(f"Supabase: {count} records saved (batch: {_batch_id})")
    except Exception as e:
        _log(f"Supabase save failed: {e}")


def _compute_stats():
    global _stats
    total = len(_results)
    _stats = {
        "total": total,
        "with_website": sum(1 for r in _results if r.get("website")),
        "with_email": sum(1 for r in _results if r.get("emails")),
        "with_phone": sum(1 for r in _results if r.get("phones") or r.get("phone")),
        "with_license": sum(1 for r in _results if r.get("license")),
        "sources": dict(Counter(
            r.get("website_source") for r in _results if r.get("website_source")
        )),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
