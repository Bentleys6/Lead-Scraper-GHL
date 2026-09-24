#!/usr/bin/env python3
"""Scrape Thomson Local for a niche + area, enrich from business websites and
Companies House, write a CSV, and upload to GoHighLevel.

Usage:
    python3 scraper.py "roofers" "surrey" --tag Roofer
    python3 scraper.py "kitchen fitters" "west yorkshire" --tag KBB --no-upload
    python3 scraper.py "heating engineers" "kent" \
        --keyword-tag "Heat Pump=heat pump,air source,ground source,ashp,gshp,mcs" \
        --fallback-tag Boiler
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
SLUG_CACHE = os.path.join(HERE, ".slug_cache.json")

TL_BASE = "https://www.thomsonlocal.com"
CH_BASE = "https://api.company-information.service.gov.uk"
# Thomson Local returns 403 without browser-like headers.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

PAGE_DELAY = 1.2
SITE_DELAY = 1.0
CH_DELAY = 0.4
MAX_PAGES = 40

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MOBILE_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)7\d{3}[\s-]?\d{3}[\s-]?\d{3}\b")
EMAIL_BLOCKLIST = ("example", "domain", "sentry", "wix", "wordpress", "jquery", "schema")
FILE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
SOCIAL_JUNK = ("sharer", "share.php", "/tr?", "/plugins/", "/dialog/", "/p/", "/reel/", "intent")

CSV_COLUMNS = ["business_name", "owner_name", "location", "phone", "email",
               "website", "facebook", "instagram", "tags"]

# Categories verified by hand where guessing slugs from the niche name fails.
# Thomson Local has no heat pump category: "heat-pump-installers" silently
# redirects to Central Heating, so heating niches use the real categories.
KNOWN_SLUGS = {
    "heating-engineers": ["central-heating", "boilers", "gas-engineers", "renewable-energy"],
}


# ---------------------------------------------------------------- helpers

def load_env():
    """Environment variables win; fall back to a local .env file."""
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalise_uk(phone):
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    return digits


def is_mobile(phone):
    d = normalise_uk(phone or "")
    return d.startswith("07") and len(d) == 11


def fmt_phone(digits):
    if len(digits) != 11:
        return digits
    if digits.startswith("02"):
        return f"{digits[:3]} {digits[3:7]} {digits[7:]}"
    if digits.startswith(("03", "08")):
        return f"{digits[:4]} {digits[4:7]} {digits[7:]}"
    return f"{digits[:5]} {digits[5:]}"


def fetch(session, url, **kwargs):
    try:
        resp = session.get(url, timeout=15, **kwargs)
        return resp if resp.status_code == 200 else None
    except requests.RequestException:
        return None


# ---------------------------------------------------------- thomson local

def slug_candidates(niche):
    base = slugify(niche)
    singular = base[:-1] if base.endswith("s") else base
    plural = singular + "s"
    cands = [base, plural, f"{singular}-services", f"{singular}-contractors",
             f"{singular}-fitters", f"{singular}ing-contractors", f"{singular}ing-services"]
    if singular.endswith("er"):  # roofer -> roofing
        stem = singular[:-2]
        cands = [c for c in cands if not c.startswith(f"{singular}ing")]
        cands += [f"{stem}ing-contractors", f"{stem}ing-services", f"{stem}ing"]
    return list(dict.fromkeys(cands))


def first_page_names(session, url):
    resp = fetch(session, url)
    if not resp:
        return frozenset()
    items = BeautifulSoup(resp.text, "html.parser").select("li.listing.clearFix h2.businessName")
    return frozenset(h.get_text(strip=True) for h in items)


def discover_slugs(session, niche):
    try:
        with open(SLUG_CACHE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    key = slugify(niche)
    if key in KNOWN_SLUGS:
        return KNOWN_SLUGS[key]
    if cache.get(key):
        return cache[key]

    print(f"Discovering Thomson Local categories for '{niche}'...")
    # Many slug variants are aliases for the same category. Listings rotate
    # between requests (the same URL twice overlaps ~70%), so treat >=50%
    # first-page overlap with an already-kept slug as an alias.
    def same(x, y):
        return len(x & y) >= 0.5 * min(len(x), len(y))

    found, seen = [], []
    for slug in slug_candidates(niche):
        names = first_page_names(session, f"{TL_BASE}/search/{slug}/london")
        alias = next((s for s, n in seen if same(n, names)), None) if names else None
        print(f"  {slug:<35} {len(names)} listings" + (f" (alias of {alias})" if alias else ""))
        if names and not alias:
            found.append(slug)
            seen.append((slug, names))
        time.sleep(PAGE_DELAY)
    cache[key] = found
    with open(SLUG_CACHE, "w") as fh:
        json.dump(cache, fh, indent=2)
    return found


def parse_listing(li):
    name_el = li.select_one("h2.businessName")
    if not name_el:
        return None
    phones = []
    for a in li.select('a[href^="tel:"]'):
        num = normalise_uk(a["href"][4:])
        if num and num not in phones:
            phones.append(num)
    phone = next((p for p in phones if is_mobile(p)), phones[0] if phones else "")

    def prop(name):
        el = li.select_one(f'[itemprop="{name}"]')
        return el.get_text(" ", strip=True) if el else ""

    street, city, postcode = prop("streetAddress"), prop("addressLocality"), prop("postalCode")
    web = li.select_one('a[data-yext="url"]')
    return {
        "business_name": name_el.get_text(" ", strip=True),
        "owner_name": "",
        "street": street, "city": city, "postcode": postcode,
        "location": ", ".join(p for p in (street, city, postcode) if p),
        "phone": phone,
        "email": "",
        "website": web["href"].strip() if web and web.get("href") else "",
        "facebook": "", "instagram": "",
        "site_text": "",
    }


def get_directory_page(session, url):
    """Back off and retry when Thomson Local rate-limits us, rather than
    mistaking a block page for 'no more listings'."""
    for wait in (30, 90, 180):
        try:
            resp = session.get(url, timeout=15)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code == 200:
            return resp
        if resp is not None and resp.status_code == 404:
            return None
        print(f"  ! {resp.status_code if resp is not None else 'error'} from Thomson Local, waiting {wait}s...")
        time.sleep(wait)
    sys.exit("Thomson Local is blocking requests. Try again later.")


def scrape_directory(session, slugs, area):
    area_slug = slugify(area)
    leads = {}
    for slug in slugs:
        page = 1
        while page <= MAX_PAGES:
            url = f"{TL_BASE}/search/{slug}/{area_slug}" + (f"?page={page}" if page > 1 else "")
            resp = get_directory_page(session, url)
            soup = BeautifulSoup(resp.text, "html.parser") if resp else None
            if soup and page == 1 and " UK |" in (soup.title.get_text() if soup.title else ""):
                sys.exit(f"Thomson Local doesn't recognise the area '{area}' and returned "
                         "nationwide results. Try a county or town name, e.g. 'surrey'.")
            items = soup.select("li.listing.clearFix") if soup else []
            if not items:
                break
            new = 0
            for li in items:
                lead = parse_listing(li)
                if not lead:
                    continue
                key = re.sub(r"[^a-z0-9]", "", lead["business_name"].lower())
                if key not in leads:
                    leads[key] = lead
                    new += 1
            print(f"  {slug} p{page}: {len(items)} listings ({new} new)")
            page += 1
            time.sleep(PAGE_DELAY)
    return list(leads.values())


# ------------------------------------------------------ website enrichment

def extract_from_page(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    out = {"emails": [], "facebook": "", "instagram": "", "mobile": "", "contact_url": ""}

    candidates = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
    candidates += EMAIL_RE.findall(soup.get_text(" "))
    for e in candidates:
        e = e.strip().lower()
        if (EMAIL_RE.fullmatch(e) and not any(b in e for b in EMAIL_BLOCKLIST)
                and not e.endswith(FILE_EXTS) and e not in out["emails"]):
            out["emails"].append(e)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if not out["facebook"] and "facebook.com/" in low and not any(j in low for j in SOCIAL_JUNK):
            out["facebook"] = href
        elif not out["instagram"] and "instagram.com/" in low and not any(j in low for j in SOCIAL_JUNK):
            out["instagram"] = href
        elif not out["mobile"] and low.startswith("tel:") and is_mobile(href[4:]):
            out["mobile"] = normalise_uk(href[4:])
        elif not out["contact_url"] and "contact" in low and not low.startswith(("mailto:", "tel:")):
            full = urljoin(base_url, href)
            if urlparse(full).netloc == urlparse(base_url).netloc:
                out["contact_url"] = full

    if not out["mobile"]:
        m = MOBILE_RE.search(soup.get_text(" "))
        if m:
            out["mobile"] = normalise_uk(m.group())
    return out


def pick_email(emails, website):
    """Prefer an address on the business's own domain."""
    if not emails:
        return ""
    host = urlparse(website).netloc.lower().removeprefix("www.")
    return next((e for e in emails if host and e.endswith("@" + host)), emails[0])


def enrich_from_website(session, lead):
    url = lead["website"]
    if not url:
        return
    if not url.startswith("http"):
        url = "http://" + url
    resp = fetch(session, url, allow_redirects=True)
    if not resp:
        return
    info = extract_from_page(resp.text, resp.url)
    texts = [BeautifulSoup(resp.text, "html.parser").get_text(" ")]

    missing = not info["emails"] or not info["facebook"] or not info["instagram"] or not info["mobile"]
    if missing and info["contact_url"]:
        time.sleep(SITE_DELAY)
        cresp = fetch(session, info["contact_url"])
        if cresp:
            extra = extract_from_page(cresp.text, cresp.url)
            texts.append(BeautifulSoup(cresp.text, "html.parser").get_text(" "))
            info["emails"] += [e for e in extra["emails"] if e not in info["emails"]]
            for k in ("facebook", "instagram", "mobile"):
                info[k] = info[k] or extra[k]

    lead["site_text"] = " ".join(texts).lower()
    lead["email"] = pick_email(info["emails"], resp.url)
    lead["facebook"] = info["facebook"]
    lead["instagram"] = info["instagram"]
    if info["mobile"] and not is_mobile(lead["phone"]):
        lead["phone"] = info["mobile"]


# ------------------------------------------------------------ keyword tags

def parse_keyword_tags(specs):
    """'Heat Pump=heat pump,air source,mcs' -> [('Heat Pump', <regex>)]"""
    rules = []
    for spec in specs or []:
        tag, _, words = spec.partition("=")
        words = [w.strip().lower() for w in words.split(",") if w.strip()]
        if not tag.strip() or not words:
            sys.exit(f"Bad --keyword-tag '{spec}'. Use: \"Tag=word one,word two\"")
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")
        rules.append((tag.strip(), pattern))
    return rules


def assign_tags(lead, base_tags, rules, fallback):
    """Tag from evidence on the business's own website (plus its name)."""
    text = f"{lead['business_name'].lower()} {lead.get('site_text', '')}"
    matched = [tag for tag, pattern in rules if pattern.search(text)]
    if not matched and fallback:
        matched = [fallback]
    lead["tag_list"] = list(dict.fromkeys(base_tags + matched))
    lead["tags"] = ", ".join(lead["tag_list"])


# ------------------------------------------------------- companies house

NAME_NOISE = {"ltd", "limited", "llp", "plc", "uk", "the", "and", "co", "company", "t/a", "of"}
# Trade words say nothing about *which* business it is, so they can't count
# towards a match ("Michael Frickey Plumbing" != "Michael Chapman Plumbing").
TRADE_WORDS = {
    "services", "service", "plumbing", "plumbers", "plumber", "heating", "gas", "boiler",
    "boilers", "engineers", "engineer", "engineering", "solutions", "contractors",
    "contractor", "installations", "installers", "installer", "maintenance", "care",
    "roofing", "roofers", "roofer", "building", "builders", "builder", "electrical",
    "electricians", "kitchens", "kitchen", "bathrooms", "fitters", "group", "holdings",
    "energy", "renewables", "renewable", "home", "homes", "property", "properties",
    "southern", "south", "east", "north", "west", "safety", "systems", "technical",
}


def name_tokens(name):
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ")).split()
            if t not in NAME_NOISE}


def postcode_area(postcode):
    m = re.match(r"\s*([A-Za-z]{1,2})\d", postcode or "")
    return m.group(1).upper() if m else ""


def format_officer_name(raw):
    """'RAFIQ, Mohammed, Mr.' -> 'Mohammed Rafiq'"""
    if "," not in raw:
        return raw.title()
    surname, rest = raw.split(",", 1)
    forenames = [w for w in re.split(r"[\s,]+", rest)
                 if w and w.rstrip(".").lower() not in {"mr", "mrs", "ms", "miss", "dr", "sir"}]
    first = forenames[0] if forenames else ""
    return f"{first.title()} {surname.strip().title()}".strip()


class CompaniesHouseAuthError(Exception):
    pass


def lookup_owner(session, api_key, business_name, postcode=""):
    """Only accept a company whose distinctive (non-trade) name words are
    exactly the listing's. If several companies qualify, take the one
    registered in the listing's postcode area, else give up: a blank owner
    beats a wrong one. A lone match on a name of two or more distinctive words
    is accepted from any area, since small firms often register at their
    accountant's address."""
    distinct = name_tokens(business_name) - TRADE_WORDS
    if not distinct:
        return ""
    try:
        resp = session.get(f"{CH_BASE}/search/companies", timeout=15,
                           params={"q": business_name, "items_per_page": 10}, auth=(api_key, ""))
    except requests.RequestException:
        return ""
    time.sleep(CH_DELAY)
    if resp.status_code == 401:
        raise CompaniesHouseAuthError(resp.text[:100])
    if resp.status_code != 200:
        return ""

    matches = [item for item in resp.json().get("items", [])
               if item.get("company_status") == "active"
               and name_tokens(item.get("title", "")) - TRADE_WORDS == distinct]
    # With several candidates, or a one-word name ("Plumb Service") that could
    # be anyone, insist on the same postcode area.
    if len(matches) > 1 or len(distinct) == 1:
        area = postcode_area(postcode)
        matches = [m for m in matches
                   if area and postcode_area((m.get("address") or {}).get("postal_code", "")) == area]
    if len(matches) != 1:
        return ""
    number = matches[0]["company_number"]

    resp = fetch(session, f"{CH_BASE}/company/{number}/officers", auth=(api_key, ""))
    time.sleep(CH_DELAY)
    if not resp:
        return ""
    directors = [format_officer_name(o.get("name", "")) for o in resp.json().get("items", [])
                 if o.get("officer_role") == "director" and not o.get("resigned_on")]
    # Prefer the director whose surname is in the business name (A C Wilgar -> Wilgar).
    for d in directors:
        if d.split()[-1].lower() in distinct:
            return d
    return directors[0] if directors else ""


# ------------------------------------------------------------------- main

def summarise(leads, tag_names=()):
    def n(fn):
        return sum(1 for l in leads if fn(l))
    print("\n========== SUMMARY ==========")
    print(f"Total leads:   {len(leads)}")
    print(f"Phone:         {n(lambda l: l['phone'])}")
    print(f"Mobile (07):   {n(lambda l: is_mobile(l['phone']))}")
    print(f"Email:         {n(lambda l: l['email'])}")
    print(f"Owner name:    {n(lambda l: l['owner_name'])}")
    print(f"Facebook:      {n(lambda l: l['facebook'])}")
    print(f"Instagram:     {n(lambda l: l['instagram'])}")
    for tag in tag_names:
        print(f"Tag {tag + ':':<10} {n(lambda l: tag in l['tag_list'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("niche", help='e.g. "roofers"')
    ap.add_argument("area", help='e.g. "surrey"')
    ap.add_argument("--tag", help='tag applied to every lead, e.g. "Roofer"')
    ap.add_argument("--keyword-tag", action="append", metavar='"TAG=word,word"',
                    help="tag leads whose website/name mentions any of the words (repeatable)")
    ap.add_argument("--fallback-tag", help="tag for leads that match no --keyword-tag")
    ap.add_argument("--slugs", help="comma-separated Thomson Local slugs (skips discovery)")
    ap.add_argument("--limit", type=int, help="only process the first N leads (for testing)")
    ap.add_argument("--no-upload", action="store_true", help="write CSV only")
    args = ap.parse_args()
    rules = parse_keyword_tags(args.keyword_tag)
    if not (args.tag or rules or args.fallback_tag):
        ap.error("give --tag and/or --keyword-tag / --fallback-tag")

    load_env()
    session = requests.Session()
    session.headers.update(HEADERS)

    slugs = args.slugs.split(",") if args.slugs else discover_slugs(session, args.niche)
    if not slugs:
        sys.exit(f"No Thomson Local categories found for '{args.niche}'. Pass --slugs manually.")
    print(f"Using categories: {', '.join(slugs)}")

    print(f"\nScraping Thomson Local for {args.niche} in {args.area}...")
    leads = scrape_directory(session, slugs, args.area)
    if args.limit:
        leads = leads[:args.limit]
    print(f"{len(leads)} unique businesses")

    print("\nChecking websites...")
    for i, lead in enumerate(leads, 1):
        enrich_from_website(session, lead)
        if lead["website"]:
            time.sleep(SITE_DELAY)
        if i % 10 == 0:
            print(f"  {i}/{len(leads)}")

    ch_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if ch_key:
        print("\nLooking up owners on Companies House...")
        try:
            for lead in leads:
                lead["owner_name"] = lookup_owner(session, ch_key, lead["business_name"], lead["postcode"])
        except CompaniesHouseAuthError as err:
            print(f"  ! Companies House rejected the API key ({err}) - owner names skipped.")
    else:
        print("\nCOMPANIES_HOUSE_API_KEY not set - skipping owner lookup.")

    base_tags = [args.tag] if args.tag else []
    for lead in leads:
        assign_tags(lead, base_tags, rules, args.fallback_tag)

    leads.sort(key=lambda l: (not is_mobile(l["phone"]), not l["phone"], l["business_name"].lower()))
    for lead in leads:
        lead["phone"] = fmt_phone(lead["phone"])

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, f"{slugify(args.niche)}_{slugify(args.area)}_{date.today()}.csv")
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(leads)
    print(f"\nSaved {csv_path}")

    tag_names = [t for t, _ in rules] + ([args.fallback_tag] if args.fallback_tag else [])
    summarise(leads, tag_names)

    if args.no_upload:
        return
    token, location = os.environ.get("GHL_API_KEY"), os.environ.get("GHL_LOCATION_ID")
    if not token or not location:
        print("\nGHL_API_KEY / GHL_LOCATION_ID not set - skipped GHL upload.")
        return
    pipelines = [p.strip() for p in os.environ.get(
        "GHL_PIPELINES", "Noah Cold Calling Pipeline,Luca Cold Calling Pipeline").split(",")]

    from ghl_upload import upload_leads
    print("\nUploading to GoHighLevel...")
    for lead in leads:
        lead["tag_list"].append(args.area.title())
    result = upload_leads(leads, token, location, pipelines)
    print(f"GHL: created {result['created']} / updated {result['updated']} / "
          f"failed {result['failed']} / skipped (no phone or email) {result['skipped']}")
    for name, count in result["pipelines"].items():
        print(f"  -> {name}: {count}")
    if result.get("note"):
        print(f"  {result['note']}")


if __name__ == "__main__":
    main()
