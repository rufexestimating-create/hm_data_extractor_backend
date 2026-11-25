"""One-time setup: grant permissions on extracted_data table in Supabase.

Requires these environment variables (put them in your .env, which is gitignored):
    SUPABASE_PROJECT_REF   your project ref, e.g. vtrtbghtfblyikwaclic
    SUPABASE_ACCESS_TOKEN  a Supabase personal access token (sbp_...)
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

PROJECT_REF = os.environ["SUPABASE_PROJECT_REF"]
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]

statements = [
    "GRANT ALL ON extracted_data TO service_role",
    "GRANT ALL ON extracted_data TO anon",
    "GRANT ALL ON extracted_data TO authenticated",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon",
]

for sql in statements:
    r = httpx.post(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query",
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"query": sql},
        timeout=30,
    )
    status = "OK" if r.status_code in (200, 201) else f"FAIL ({r.status_code})"
    print(f"  {status}: {sql[:60]}")

print("\nDone! Permissions granted.")
