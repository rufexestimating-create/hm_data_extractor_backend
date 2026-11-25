"""
Configuration for HM Data Extractor.
"""

# --- Request Settings ---
REQUEST_TIMEOUT = 20            # seconds per page request
DELAY_BETWEEN_REQUESTS = 0.2   # seconds between batches (fast but polite)
MAX_RETRIES = 2                # retry failed requests up to this many times
CONCURRENT_WORKERS = 10        # how many pages to fetch at the same time (10 parallel workers)
HOUZZ_WORKERS = 3              # Houzz returns 429 "Too Many Requests" above this
RATE_LIMIT_WAITS = [10, 30, 60, 120]  # seconds to pause on each 429 before retrying the same URL

# --- Browser Settings ---
HEADLESS = True                # True = invisible browser, False = visible browser window
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# --- Website finding ---
# When the directory lists NO website but shows a business e-mail (info@abcbuilders.com),
# use abcbuilders.com - only if that site opens and belongs to the same company.
# The CSV column "Website Source" then says "business e-mail domain (site checked)".
# Set to False to keep only websites the directory itself shows.
INFER_WEBSITE_FROM_EMAIL = True

# --- Output Settings ---
OUTPUT_DIR = "output"
DEFAULT_FILENAME = "extracted_data"

# --- Extraction Patterns ---
import re

PHONE_PATTERNS = [
    re.compile(r'\+?1?\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}'),
    re.compile(r'\(\d{3}\)\s*\d{3}[-.\s]?\d{4}'),
    re.compile(r'\d{3}[-.\s]\d{3}[-.\s]\d{4}'),
    re.compile(r'\+\d{1,3}[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}'),
]

EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
)

# Social media URL patterns
SOCIAL_PATTERNS = {
    "facebook": re.compile(r'https?://(?:www\.)?facebook\.com/[^\s"\'<>]+', re.I),
    "instagram": re.compile(r'https?://(?:www\.)?instagram\.com/[^\s"\'<>]+', re.I),
    "twitter": re.compile(r'https?://(?:www\.)?(?:twitter|x)\.com/[^\s"\'<>]+', re.I),
    "linkedin": re.compile(r'https?://(?:www\.)?linkedin\.com/[^\s"\'<>]+', re.I),
    "youtube": re.compile(r'https?://(?:www\.)?youtube\.com/[^\s"\'<>]+', re.I),
}
