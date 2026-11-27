"""
HM Data Extractor — FastAPI Backend
Wraps existing extraction logic with REST API + Server-Sent Events for real-time progress.
Every request is tied to a signed-in Supabase user; each user has their own job and their own data.
"""

import asyncio
import json
import os
import re
import time
from collections import Counter
from contextvars import ContextVar
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from fastapi.responses import StreamingResponse

from browser import PageFetcher
from directory_parser import find_listing_links
import smart_directory as sd
from extractor import extract_all
from exporter import save_csv, save_excel
from website_crawler import enrich_records_with_websites
import config
import supabase_db
import user_auth
from user_auth import current_user

app = FastAPI(title="HM Data Extractor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractionRequest(BaseModel):
    url: str
    fast: bool = True
    max_pages: int | None = None


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str


class Job:
    """Everything about one user's current/last extraction."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.logs: list[str] = []
        self.results: list[dict] = []
        self.stats: dict = {}
        self.progress: float = 0
        self.status = "idle"
        self.stop_event: asyncio.Event | None = None
        self.batch_id = ""
        self.fast = True


_jobs: dict[str, Job] = {}
_job_ctx: ContextVar[Job] = ContextVar("job")


def job_for(user_id: str) -> Job:
    if user_id not in _jobs:
        _jobs[user_id] = Job(user_id)
    return _jobs[user_id]


def J() -> Job:
    return _job_ctx.get()


def _log(msg: str):
    J().logs.append(msg)


def _backup_name(url: str) -> str:
    parsed = urlparse(url)
    site = parsed.netloc.replace("www.", "").split(".")[0]
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    desc = "_".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "data")
    desc = re.sub(r"[^a-zA-Z0-9_-]", "_", desc)[:40]
    date = time.strftime("%Y%m%d_%H%M")
    return f"{site}_{desc}_{date}"


# ── Auth ──

@app.post("/api/auth/signup")
async def signup(req: SignupRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_auth.check_signup_rate(ip)
    user_auth.validate_signup(req.name, req.email, req.password)
    return {"message": "Validation passed"}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    return user


# ── Extraction control ──

@app.get("/api/status")
async def get_status(user: dict = Depends(current_user)):
    job = job_for(user["id"])
    return {
        "status": job.status,
        "progress": job.progress,
        "log_count": len(job.logs),
        "result_count": len(job.results),
        "stats": job.stats,
    }


@app.get("/api/logs")
async def get_logs(after: int = 0, user: dict = Depends(current_user)):
    job = job_for(user["id"])
    return {"logs": job.logs[after:], "total": len(job.logs)}


@app.get("/api/results")
async def get_results(user: dict = Depends(current_user)):
    job = job_for(user["id"])
    return {"results": job.results, "stats": job.stats}


@app.get("/api/stream")
async def stream_progress(user: dict = Depends(current_user)):
    """SSE endpoint for real-time progress updates."""
    job = job_for(user["id"])

    async def event_generator():
        last_log = 0
        last_progress = -1
        while True:
            if job.status == "idle":
                yield {"event": "status", "data": json.dumps({"status": "idle"})}
                break

            new_logs = job.logs[last_log:]
            current_progress = job.progress

            if new_logs or current_progress != last_progress:
                yield {
                    "event": "update",
                    "data": json.dumps({
                        "status": job.status,
                        "progress": current_progress,
                        "logs": new_logs,
                        "result_count": len(job.results),
                        "stats": job.stats,
                        "batch_id": job.batch_id,
                    })
                }
                last_log = len(job.logs)
                last_progress = current_progress

            if job.status in ("done", "stopped", "error"):
                yield {
                    "event": "complete",
                    "data": json.dumps({
                        "status": job.status,
                        "progress": 100 if job.status == "done" else job.progress,
                        "result_count": len(job.results),
                        "stats": job.stats,
                        "batch_id": job.batch_id,
                    })
                }
                break

            await asyncio.sleep(0.3)

    return EventSourceResponse(event_generator())


@app.post("/api/extract")
async def start_extraction(req: ExtractionRequest, user: dict = Depends(current_user)):
    job = job_for(user["id"])
    if job.status == "running":
        raise HTTPException(400, "Extraction already running")

    job.logs.clear()
    job.results.clear()
    job.stats.clear()
    job.progress = 0
    job.status = "running"
    job.stop_event = asyncio.Event()
    job.batch_id = supabase_db.generate_batch_id()
    job.fast = req.fast

    asyncio.create_task(_run_extraction(job, req.url, req.fast, req.max_pages))
    return {"message": "Extraction started", "url": req.url}


@app.post("/api/stop")
async def stop_extraction(user: dict = Depends(current_user)):
    job = job_for(user["id"])
    if job.stop_event and job.status == "running":
        job.stop_event.set()
        return {"message": "Stop signal sent"}
    raise HTTPException(400, "No extraction running")


# ── Saved data (always limited to the signed-in user) ──

@app.get("/api/batches")
async def list_batches(mode: str | None = None,
                       date_from: str | None = None,
                       date_to: str | None = None,
                       user: dict = Depends(current_user)):
    """List this user's extraction batches with optional filters."""
    try:
        batches = await asyncio.to_thread(
            supabase_db.get_batches, user["id"], mode=mode, date_from=date_from, date_to=date_to)
        return {"batches": batches}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/data")
async def get_data(batch_id: str | None = None, limit: int = 50, offset: int = 0,
                   mode: str | None = None,
                   date_from: str | None = None,
                   date_to: str | None = None,
                   search: str | None = None,
                   user: dict = Depends(current_user)):
    """Fetch this user's records with filters and pagination."""
    try:
        records = await asyncio.to_thread(
            supabase_db.get_records, user["id"],
            batch_id=batch_id, limit=limit, offset=offset,
            mode=mode, date_from=date_from, date_to=date_to, search=search)
        total = await asyncio.to_thread(
            supabase_db.get_total_count, user["id"], batch_id=batch_id, mode=mode,
            date_from=date_from, date_to=date_to, search=search)
        return {"records": records, "count": len(records), "total": total}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/export-csv")
async def export_csv_download(batch_id: str | None = None,
                              mode: str | None = None,
                              date_from: str | None = None,
                              date_to: str | None = None,
                              search: str | None = None,
                              user: dict = Depends(current_user)):
    """Download this user's filtered records as CSV file."""
    try:
        csv_str = await asyncio.to_thread(
            supabase_db.export_csv_string, user["id"], batch_id=batch_id, mode=mode,
            date_from=date_from, date_to=date_to, search=search)
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
async def delete_batch(batch_id: str, user: dict = Depends(current_user)):
    """Delete all of this user's records for a batch."""
    try:
        count = await asyncio.to_thread(supabase_db.delete_batch, batch_id, user["id"])
        return {"deleted": count, "batch_id": batch_id}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/open-output")
async def open_output(user: dict = Depends(current_user)):
    import subprocess
    output_path = os.path.abspath(_user_output_dir(user["id"]))
    os.makedirs(output_path, exist_ok=True)
    subprocess.Popen(f'explorer "{output_path}"')
    return {"message": "Opened", "path": output_path}


def _user_output_dir(user_id: str) -> str:
    return os.path.join(config.OUTPUT_DIR, "users", user_id[:8])


def _norm_name(name: str) -> str:
    return "name:" + re.sub(r"[^a-z0-9]", "", (name or "").lower())


async def _run_extraction(job: Job, url: str, fast: bool, max_pages: int | None):
    _job_ctx.set(job)

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

            if job.stop_event.is_set():
                job.status = "stopped"
                _save_results(url)
                return

            if not cards:
                _log("No companies found on this page.")
                job.status = "error"
                return

            n_web = sum(1 for c in cards if c.get("website"))
            _log(f"Found {len(cards)} companies ({n_web} show website on list page)")

            # Step 2: Visit company pages for website
            _log(f"Step 2: Opening {len(cards)} company pages ({workers} at a time)...")
            await _profiles_stage(fetcher, cards, url, fast, chrome)

            if job.stop_event.is_set():
                job.status = "stopped"
                _save_results(url)
                return

            # Step 3: Deep crawl websites
            if deep_crawl:
                todo = [r for r in job.results if r.get("website")]
                if todo:
                    _log(f"Step 3: Deep-crawling {len(todo)} websites for emails (10 parallel)...")
                    job.progress = 70

                    def on_done(done, total, rec, found_emails):
                        name = rec.get("name") or "Company"
                        cp = rec.get("contact_page") or rec.get("website_status") or ""
                        mail = ", ".join(found_emails[:2]) if found_emails else "no email"
                        _log(f"  [{done}/{total}] {name} | {cp} | {mail}")
                        job.progress = 70 + 25 * done / max(total, 1)

                    await enrich_records_with_websites(
                        todo, concurrency=10, progress_callback=on_done,
                        browser_fetch=fetcher.fetch_browser)

            # Save
            _save_results(url)
            _compute_stats()
            job.progress = 100
            job.status = "done"
            _log(f"Done! {len(job.results)} records extracted and saved.")

    except asyncio.CancelledError:
        job.status = "stopped"
        _save_results(url)
    except Exception as e:
        _log(f"Error: {e}")
        job.status = "error"
        _save_results(url)


async def _pages_stage(fetcher, url, is_houzz, max_pages):
    job = J()
    cards = []
    chrome = set()
    seen_keys = set()
    seen_pages = set()
    page_url = url
    page_num = 1

    while page_url and page_url not in seen_pages:
        if job.stop_event and job.stop_event.is_set():
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
        job.progress = min(15, 15 * page_num / max(max_pages or 10, 1))

        if max_pages and page_num >= max_pages:
            _log(f"  Reached page limit ({max_pages})")
            break

        how = ""
        if is_houzz:
            from directory_parser import find_next_page
            next_url = find_next_page(html, page_url, listings_count=added)
        else:
            next_url, how = sd.find_next_page(html, page_url, len(page_cards))

        go_dynamic = False
        if not is_houzz and page_cards:
            if page_num == 2 and added == 0:
                go_dynamic, next_url = True, None
                start_page = page_num
            elif not next_url:
                total = sd.total_hint(html)
                if how == "js" or sd.has_load_more(html) or (total and total > len(cards)):
                    go_dynamic = True
                    start_page = page_num + 1
            _log(f"  Pagination: next_url={bool(next_url)}, how='{how}', go_dynamic={go_dynamic}")

        if go_dynamic:
            _log("  Next pages load by clicking/scrolling - continuing in the browser...")
            pages = fetcher.iter_dynamic_pages(url, start_page=start_page)
            stale = 0
            try:
                async for num, dhtml in pages:
                    if job.stop_event and job.stop_event.is_set():
                        break
                    if max_pages and num > max_pages:
                        _log(f"  Reached page limit ({max_pages})")
                        break
                    dres = await asyncio.to_thread(sd.analyze_list, dhtml, url, chrome)
                    chrome |= dres["chrome_hosts"]
                    added = 0
                    for c in dres["records"]:
                        key = c["profile_url"] or ("name:" + (c.get("name") or "").lower())
                        if key not in seen_keys:
                            seen_keys.add(key)
                            cards.append(c)
                            added += 1
                    _log(f"  Page {num}: {added} new companies (Total: {len(cards)})")
                    job.progress = min(15, 15 * num / max(max_pages or 10, 1))
                    stale = stale + 1 if added == 0 else 0
                    if stale >= 3:
                        _log("  3 stale pages in a row - stopping")
                        break
            except Exception as e:
                _log(f"  Dynamic pagination error: {type(e).__name__}: {e}")
            finally:
                await pages.aclose()
            break

        if next_url and next_url not in seen_pages and (added or page_num == 1):
            page_url = next_url
            page_num += 1
            await asyncio.sleep(config.DELAY_BETWEEN_REQUESTS)
        else:
            _log(f"  Last page reached (Page {page_num})")
            break

    return cards, chrome


async def _profiles_stage(fetcher, cards, list_url, fast, chrome):
    job = J()
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
        job.results.append(rec)
        done_count += 1

    if direct:
        _log(f"  {len(direct)} companies already show website on list page")

    async def work(c):
        nonlocal done_count
        if job.stop_event and job.stop_event.is_set():
            return
        if not c["profile_url"]:
            rec = {"name": c["name"], "page_url": list_url, "website": c.get("website", ""),
                   "emails": list(c.get("emails") or []), "state": c.get("state", ""),
                   "license": "", "website_source": "listing page" if c.get("website") else ""}
            if fast:
                rec["phone"] = c.get("phone", "")
            else:
                rec.update({"phones": [c["phone"]] if c.get("phone") else [], "address": "", "socials": {}})
            job.results.append(rec)
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
        job.results.append(rec)
        done_count += 1
        _log(f"  [{done_count}/{total}] {rec.get('name', '—')}  ->  {rec.get('website') or '— (no website)'}")
        job.progress = 15 + 55 * done_count / max(total, 1)

    if need:
        sem = asyncio.Semaphore(config.CONCURRENT_WORKERS)
        async def limited(c):
            async with sem:
                await work(c)
        await asyncio.gather(*(limited(c) for c in need))


def _save_results(url: str):
    job = J()
    if not job.results:
        return

    user_dir = _user_output_dir(job.user_id)
    rel_dir = os.path.relpath(user_dir, config.OUTPUT_DIR)
    try:
        os.makedirs(user_dir, exist_ok=True)
        csv_path = save_csv(job.results, os.path.join(rel_dir, config.DEFAULT_FILENAME))
        xlsx_path = save_excel(job.results, os.path.join(rel_dir, config.DEFAULT_FILENAME))
        _log(f"CSV:   {csv_path}")
        _log(f"Excel: {xlsx_path}")
    except Exception as e:
        _log(f"CSV/Excel save failed (file may be open): {e}")

    try:
        os.makedirs(os.path.join(user_dir, "backups"), exist_ok=True)
        backup_path = save_csv(job.results, os.path.join(rel_dir, "backups", _backup_name(url)))
        _log(f"Backup: {backup_path}")
    except Exception as e:
        _log(f"Backup save failed: {e}")

    try:
        mode = "fast" if job.fast else "deep"
        count = supabase_db.save_records(job.results, url, mode, job.batch_id, job.user_id)
        _log(f"Supabase: {count} records saved (batch: {job.batch_id})")
    except Exception as e:
        if "user_id" in str(e):
            _log("Supabase save failed: run setup_supabase_auth.sql once in the Supabase SQL Editor "
                 "(the user_id column is missing). Your CSV/Excel files were saved.")
        else:
            _log(f"Supabase save failed: {e}")


def _compute_stats():
    job = J()
    results = job.results
    job.stats = {
        "total": len(results),
        "with_website": sum(1 for r in results if r.get("website")),
        "with_email": sum(1 for r in results if r.get("emails")),
        "with_phone": sum(1 for r in results if r.get("phones") or r.get("phone")),
        "with_license": sum(1 for r in results if r.get("license")),
        "sources": dict(Counter(
            r.get("website_source") for r in results if r.get("website_source")
        )),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
