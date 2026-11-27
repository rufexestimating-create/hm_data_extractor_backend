"""
Supabase integration for HM Data Extractor.
Stores extracted records in Supabase and supports CSV export.
"""

import csv
import io
import os
import time
import uuid

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL or SUPABASE_SECRET_KEY not set in .env")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def generate_batch_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:8]}"


def save_records(records: list[dict], source_url: str, mode: str, batch_id: str, user_id: str) -> int:
    """Insert extraction records into Supabase. Returns number of rows inserted."""
    client = get_client()
    rows = []
    for rec in records:
        socials = rec.get("socials", {})
        phones = rec.get("phones", [])
        if isinstance(phones, str):
            phones = [phones] if phones else []

        row = {
            "user_id": user_id,
            "batch_id": batch_id,
            "source_url": source_url,
            "mode": mode,
            "name": rec.get("name") or "",
            "website": rec.get("website") or "",
            "contact_page": rec.get("contact_page") or rec.get("website_status") or "",
            "emails": " | ".join(rec.get("emails", [])),
            "phone": rec.get("phone") or " | ".join(phones),
            "address": rec.get("address") or "",
            "state": rec.get("state") or "",
            "license_number": rec.get("license") or "",
            "facebook": socials.get("facebook", ""),
            "instagram": socials.get("instagram", ""),
            "twitter": socials.get("twitter", ""),
            "linkedin": socials.get("linkedin", ""),
            "youtube": socials.get("youtube", ""),
            "page_url": rec.get("page_url") or "",
            "website_source": rec.get("website_source") or "",
        }
        rows.append(row)

    if not rows:
        return 0

    batch_size = 500
    inserted = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        client.table("extracted_data").insert(chunk).execute()
        inserted += len(chunk)

    return inserted


def get_batches(user_id: str, limit: int = 50, offset: int = 0,
                mode: str | None = None,
                date_from: str | None = None,
                date_to: str | None = None) -> list[dict]:
    """Get extraction batches with optional filters."""
    client = get_client()

    # Step 1: collect unique batch metadata (one row per batch_id via pagination)
    batches: dict[str, dict] = {}
    pg_offset = 0
    pg_size = 1000
    while True:
        query = (
            client.table("extracted_data")
            .select("batch_id, source_url, mode, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
        )
        if mode:
            query = query.eq("mode", mode)
        if date_from:
            query = query.gte("created_at", date_from)
        if date_to:
            query = query.lte("created_at", date_to + "T23:59:59Z")
        resp = query.range(pg_offset, pg_offset + pg_size - 1).execute()
        for row in resp.data:
            bid = row["batch_id"]
            if bid not in batches:
                batches[bid] = {
                    "batch_id": bid,
                    "source_url": row["source_url"],
                    "mode": row["mode"],
                    "created_at": row["created_at"],
                }
        if len(resp.data) < pg_size:
            break
        pg_offset += pg_size

    # Step 2: count records per batch using exact count queries
    for bid, info in batches.items():
        count_q = (
            client.table("extracted_data")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("batch_id", bid)
        )
        if mode:
            count_q = count_q.eq("mode", mode)
        if date_from:
            count_q = count_q.gte("created_at", date_from)
        if date_to:
            count_q = count_q.lte("created_at", date_to + "T23:59:59Z")
        cr = count_q.execute()
        info["count"] = cr.count or 0

    result = sorted(batches.values(), key=lambda b: b["created_at"], reverse=True)
    return result[offset:offset + limit]


def get_records(user_id: str, batch_id: str | None = None, limit: int = 1000, offset: int = 0,
                mode: str | None = None,
                date_from: str | None = None,
                date_to: str | None = None,
                search: str | None = None) -> list[dict]:
    """Fetch records with optional filters."""
    client = get_client()
    query = client.table("extracted_data").select("*").eq("user_id", user_id).order("created_at", desc=True)
    if batch_id:
        query = query.eq("batch_id", batch_id)
    if mode:
        query = query.eq("mode", mode)
    if date_from:
        query = query.gte("created_at", date_from)
    if date_to:
        query = query.lte("created_at", date_to + "T23:59:59Z")
    if search:
        query = query.or_(f"name.ilike.%{search}%,website.ilike.%{search}%,emails.ilike.%{search}%")
    resp = query.range(offset, offset + limit - 1).execute()
    return resp.data


def get_total_count(user_id: str, batch_id: str | None = None,
                    mode: str | None = None,
                    date_from: str | None = None,
                    date_to: str | None = None,
                    search: str | None = None) -> int:
    """Get total record count with filters."""
    client = get_client()
    query = client.table("extracted_data").select("id", count="exact").eq("user_id", user_id)
    if batch_id:
        query = query.eq("batch_id", batch_id)
    if mode:
        query = query.eq("mode", mode)
    if date_from:
        query = query.gte("created_at", date_from)
    if date_to:
        query = query.lte("created_at", date_to + "T23:59:59Z")
    if search:
        query = query.or_(f"name.ilike.%{search}%,website.ilike.%{search}%,emails.ilike.%{search}%")
    resp = query.execute()
    return resp.count or len(resp.data)


def export_csv_string(user_id: str, batch_id: str | None = None,
                      mode: str | None = None,
                      date_from: str | None = None,
                      date_to: str | None = None,
                      search: str | None = None) -> str:
    """Export filtered records as a CSV string."""
    records: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        page = get_records(user_id, batch_id=batch_id, limit=page_size,
                           offset=offset, mode=mode, date_from=date_from,
                           date_to=date_to, search=search)
        records.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    if not records:
        return ""

    first = records[0]
    is_fast = first.get("mode") == "fast"

    if is_fast:
        headers = ["Name", "Website", "Email(s)", "Phone", "State",
                    "License Number", "Source URL", "Website Source"]
        keys = ["name", "website", "emails", "phone", "state",
                "license_number", "page_url", "website_source"]
    else:
        headers = ["Name", "Website", "Contact Page", "Email(s)", "Phone(s)",
                    "Address", "State", "License Number", "Facebook", "Instagram",
                    "Twitter / X", "LinkedIn", "YouTube", "Source URL", "Website Source"]
        keys = ["name", "website", "contact_page", "emails", "phone",
                "address", "state", "license_number", "facebook", "instagram",
                "twitter", "linkedin", "youtube", "page_url", "website_source"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for rec in records:
        writer.writerow([rec.get(k, "") for k in keys])

    return buf.getvalue()


def delete_batch(batch_id: str, user_id: str) -> int:
    """Delete all records for a batch. Returns count deleted."""
    client = get_client()
    resp = client.table("extracted_data").delete().eq("batch_id", batch_id).eq("user_id", user_id).execute()
    return len(resp.data)
