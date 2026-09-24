#!/usr/bin/env python3
"""Heat pump / boiler installer lead scraper -> CSV -> GoHighLevel.

Usage:
    python3 scraper.py "Surrey"
    python3 scraper.py "Stockport Cheshire" --no-upload
    python3 scraper.py "West Yorkshire" --max-pages 3 --skip-websites
"""

import argparse
import csv
import datetime
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import ghl_upload

NICHE_TAG = "Heat Pump Installers / Boiler Installers"

# Thomson Local has no dedicated "heat pump installer" category; these are the
# heating categories it does use. Slugs with no results in an area cost one
# request and are skipped. Tune with --slugs.
CANDIDATE_SLUGS = [
    "heat-pumps",
    "heat-pump-installers",
    "renewable-energy",
    "boilers-servicing-replacements-repairs",
    "boiler-service",
    "boiler-repairs",
    "central-heating-installation-servicing",
    "heating-engineers",
    "gas-engineers",
]

TL_BASE = "https://www.thomsonlocal.com"
CH_BASE = "https://api.company-information.service.gov.uk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}
PAGE_DELAY, SITE_DELAY, CH_DELAY = 1.2, 1.0, 0.4
MAX_PAGES = 40

EMAIL_BLOCKLIST = ("example", "domain", "sentry", "wix", "wordpress", "jquery", "schema")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# UK mobile in plain text: 07xxx xxxxxx / +44 7xxx xxxxxx with optional spaces/dashes
MOBILE_TEXT_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)7\d{3}[\s-]?\d{3}[\s-]?\d{3}\b")
COMPANY_SUFFIXES = re.compile(r"\b(ltd|limited|llp|plc|uk|the|and|co|company|services?|&)\b")

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# config / helpers
# ---------------------------------------------------------------------------

def load_config():
    """.env file in this folder, overridden by real environment variables."""
    config = {}
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(config) + ["GHL_API_KEY", "GHL_LOCATION_ID", "COMPANIES_HOUSE_API_KEY",
                               "GHL_PIPELINE_A", "GHL_PIPELINE_B", "GHL_PIPELINE_STAGE",
                               "PIPELINE_SPLIT"]:
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalise_phone(raw):
    """Return a UK number in 0XXXXXXXXXX form, or None."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    if digits.startswith("0") and len(digits) in (10, 11):
        return digits
    return None


def is_mobile(phone):
    # 07 numbers, excluding 070 (personal numbering) and 076 (pagers) — except 07624 (IoM mobile)
    return bool(phone) and len(phone) == 11 and phone.startswith("07") and (
        phone[2] not in "06" or phone.startswith("07624"))


def pick_phone(numbers):
    numbers = [n for n in numbers if n]
    for n in numbers:
        if is_mobile(n):
            return n
    return numbers[0] if numbers else None


def normalise_name(name):
    name = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    return " ".join(COMPANY_SUFFIXES.sub(" ", name).split())


def get(session, url, **kwargs):
    try:
        resp = session.get(url, timeout=15, **kwargs)
    except requests.RequestException:
        return None
    return resp if resp.status_code == 200 else None


# ---------------------------------------------------------------------------
# Thomson Local
# ---------------------------------------------------------------------------

def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
    leads = []
    for li in soup.select("li.listing"):
        name_el = li.select_one("h2.businessName")
        if not name_el:
            continue
        numbers = [normalise_phone(a["href"][4:]) for a in li.select('a[href^="tel:"]')]

        def prop(name):
            el = li.select_one(f'[itemprop="{name}"]')
            return el.get_text(" ", strip=True).rstrip(",") if el else ""

        site = li.select_one('a[data-yext="url"]')
        website = site.get("href", "").strip() if site else ""
        if website and not website.startswith("http"):
            website = "http://" + website.lstrip("/")
        leads.append({
            "business_name": name_el.get_text(" ", strip=True),
            "phone": pick_phone(numbers),
            "street": prop("streetAddress"),
            "city": prop("addressLocality"),
            "postcode": prop("postalCode"),
            "website": website,
            "email": "", "facebook": "", "instagram": "", "owner_name": "",
        })
    return leads


def scrape_thomson(session, slugs, location_slug, max_pages):
    found = {}
    for slug in slugs:
        seen_page_names = None
        slug_total = 0
        for page in range(1, max_pages + 1):
            url = f"{TL_BASE}/search/{slug}/{location_slug}"
            resp = get(session, url, params={"page": page} if page > 1 else None)
            time.sleep(PAGE_DELAY)
            # An unknown slug/location redirects elsewhere or 404s — treat as no results.
            if resp is None or f"/search/{slug}/" not in resp.url:
                break
            listings = parse_listings(resp.text)
            names = [l["business_name"] for l in listings]
            if not listings or names == seen_page_names:
                break
            seen_page_names = names
            for lead in listings:
                key = normalise_name(lead["business_name"])
                if key in found:  # fill gaps from the duplicate listing
                    existing = found[key]
                    for field, value in lead.items():
                        if value and not existing.get(field):
                            existing[field] = value
                    if is_mobile(lead["phone"]) and not is_mobile(existing["phone"]):
                        existing["phone"] = lead["phone"]
                else:
                    found[key] = lead
            slug_total += len(listings)
        print(f"  {slug}: {slug_total} listings")
    return list(found.values())


# ---------------------------------------------------------------------------
# Website enrichment
# ---------------------------------------------------------------------------

def extract_from_page(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = {"emails": [], "facebook": "", "instagram": "", "mobiles": [], "contact_url": ""}
    site_host = urlparse(base_url).netloc.lower().removeprefix("www.")

    candidates = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
    candidates += EMAIL_RE.findall(soup.get_text(" "))
    for email in candidates:
        email = email.strip().lower()
        if (EMAIL_RE.fullmatch(email) and not any(b in email for b in EMAIL_BLOCKLIST)
                and not email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))
                and email not in out["emails"]):
            out["emails"].append(email)
    # prefer an address on the business's own domain
    out["emails"].sort(key=lambda e: 0 if site_host and e.endswith("@" + site_host) else 1)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if "facebook.com" in low and not out["facebook"] and not any(
                x in low for x in ("sharer", "share.php", "/plugins/", "/dialog/", "/tr?")):
            out["facebook"] = href.split("?")[0]
        elif "instagram.com" in low and not out["instagram"] and "/p/" not in low:
            out["instagram"] = href.split("?")[0]
        elif low.startswith("tel:"):
            n = normalise_phone(href[4:])
            if is_mobile(n):
                out["mobiles"].append(n)
        elif not out["contact_url"] and "contact" in low and not low.startswith(("mailto:", "javascript:")):
            full = urljoin(base_url, href)
            if urlparse(full).netloc.lower().removeprefix("www.") == site_host:
                out["contact_url"] = full

    for match in MOBILE_TEXT_RE.findall(soup.get_text(" ")):
        n = normalise_phone(match)
        if is_mobile(n):
            out["mobiles"].append(n)
    return out


def enrich_from_website(session, lead):
    resp = get(session, lead["website"])
    time.sleep(SITE_DELAY)
    if resp is None:
        return
    pages = [extract_from_page(resp.text, resp.url)]
    first = pages[0]
    missing = (not first["emails"] or not first["facebook"] or not first["instagram"]
               or (not is_mobile(lead["phone"]) and not first["mobiles"]))
    if missing and first["contact_url"]:
        contact = get(session, first["contact_url"])
        time.sleep(SITE_DELAY)
        if contact is not None:
            pages.append(extract_from_page(contact.text, contact.url))

    for page in pages:
        if not lead["email"] and page["emails"]:
            lead["email"] = page["emails"][0]
        lead["facebook"] = lead["facebook"] or page["facebook"]
        lead["instagram"] = lead["instagram"] or page["instagram"]
        if not is_mobile(lead["phone"]) and page["mobiles"]:
            lead["phone"] = page["mobiles"][0]


# ---------------------------------------------------------------------------
# Companies House
# ---------------------------------------------------------------------------

def format_officer_name(raw):
    """'SMITH, John Paul' -> 'John Smith'."""
    if "," not in raw:
        return raw.title()
    surname, forenames = raw.split(",", 1)
    first = forenames.split()[0] if forenames.split() else ""
    return f"{first.title()} {surname.strip().title()}".strip()


def lookup_owner(session, api_key, business_name):
    """Return the first active director's name, or '' if there's no confident match.

    Sole traders and partnerships aren't on Companies House, so a miss is normal.
    """
    auth = (api_key, "")
    resp = get(session, f"{CH_BASE}/search/companies",
               params={"q": business_name, "items_per_page": 5}, auth=auth)
    time.sleep(CH_DELAY)
    if resp is None:
        return ""
    target = normalise_name(business_name)
    match = None
    for item in resp.json().get("items", []):
        if item.get("company_status") != "active":
            continue
        # Exact name match only — a wrong owner name on a cold call is worse than none.
        if normalise_name(item.get("title")) == target and target:
            match = item
            break
    if not match:
        return ""
    resp = get(session, f"{CH_BASE}/company/{match['company_number']}/officers", auth=auth)
    time.sleep(CH_DELAY)
    if resp is None:
        return ""
    for officer in resp.json().get("items", []):
        if officer.get("officer_role") == "director" and not officer.get("resigned_on"):
            return format_officer_name(officer.get("name", ""))
    return ""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def full_address(lead):
    return ", ".join(p for p in (lead["street"], lead["city"], lead["postcode"]) if p)


def sort_leads(leads):
    # mobiles first, then any phone, then the rest
    return sorted(leads, key=lambda l: (not is_mobile(l["phone"]), not l["phone"]))


def write_csv(leads, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["business_name", "owner_name", "location", "phone", "email",
                         "website", "facebook", "instagram"])
        for l in leads:
            writer.writerow([l["business_name"], l["owner_name"], full_address(l), l["phone"] or "",
                             l["email"], l["website"], l["facebook"], l["instagram"]])


def print_summary(leads, upload_stats):
    n = len(leads)
    count = lambda pred: sum(1 for l in leads if pred(l))
    print("\n" + "=" * 44)
    print(f"Total leads scraped: {n}")
    print(f"  with phone:     {count(lambda l: l['phone'])}")
    print(f"  with mobile:    {count(lambda l: is_mobile(l['phone']))}")
    print(f"  with email:     {count(lambda l: l['email'])}")
    print(f"  with owner:     {count(lambda l: l['owner_name'])}")
    print(f"  with Facebook:  {count(lambda l: l['facebook'])}")
    print(f"  with Instagram: {count(lambda l: l['instagram'])}")
    if upload_stats:
        s = upload_stats
        print(f"GHL contacts: {s['created']} created / {s['updated']} updated / {s['failed']} failed"
              f" ({s['skipped_no_contact']} skipped: no phone or email)")
        if s["opp_created"] or s["opp_existing"] or s["opp_failed"]:
            print(f"GHL opportunities: {s['opp_created']} created / {s['opp_existing']} already in a"
                  f" pipeline / {s['opp_failed']} failed")
            for name, c in s["pipeline_counts"].items():
                print(f"  -> {name}: {c}")
    print("=" * 44)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("area", help='UK area, e.g. "Surrey", "Stockport Cheshire", "M21"')
    parser.add_argument("--no-upload", action="store_true", help="CSV only, skip GoHighLevel")
    parser.add_argument("--skip-websites", action="store_true", help="skip website enrichment")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--slugs", nargs="+", default=CANDIDATE_SLUGS, help="Thomson Local category slugs")
    args = parser.parse_args()

    config = load_config()
    area = args.area.strip()
    location_slug = slugify(area)
    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"Scraping Thomson Local for '{area}' ({location_slug})...")
    leads = scrape_thomson(session, args.slugs, location_slug, args.max_pages)
    print(f"{len(leads)} unique businesses")
    if not leads:
        print("No listings. Thomson Local location slugs are often 'town-county' "
              "(e.g. 'Stockport Cheshire') or a postcode district (e.g. 'M21').")
        return 1

    if not args.skip_websites:
        with_site = [l for l in leads if l["website"]]
        print(f"Checking {len(with_site)} websites...")
        for i, lead in enumerate(with_site, 1):
            enrich_from_website(session, lead)
            if i % 10 == 0:
                print(f"  {i}/{len(with_site)}")

    ch_key = config.get("COMPANIES_HOUSE_API_KEY")
    if ch_key:
        print("Looking up owners on Companies House...")
        for lead in leads:
            lead["owner_name"] = lookup_owner(session, ch_key, lead["business_name"])
    else:
        print("COMPANIES_HOUSE_API_KEY not set — skipping owner lookup.")

    leads = sort_leads(leads)
    for lead in leads:
        lead["is_mobile"] = is_mobile(lead["phone"])
    out_path = HERE / "output" / f"{slugify(NICHE_TAG)}_{location_slug}_{datetime.date.today()}.csv"
    write_csv(leads, out_path)
    print(f"Saved {out_path}")

    upload_stats = None
    if not args.no_upload:
        print("Uploading to GoHighLevel...")
        try:
            upload_stats = ghl_upload.upload_leads(leads, config, [NICHE_TAG, area])
        except ghl_upload.GHLError as e:
            print(f"GHL upload aborted: {e}")
    print_summary(leads, upload_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
