#!/usr/bin/env python3
"""Replace search-area tags on scraper contacts with the county from each
contact's own postcode. Only touches contacts whose source is "Lead Scraper",
and only the area tags named on the command line.

    python3 retag_areas.py essex hertfordshire kent            # dry run
    python3 retag_areas.py essex hertfordshire kent --apply    # make changes
    python3 retag_areas.py --undo output/retag_backup_....json # restore
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests

from counties import county_for_postcode
from scraper import OUTPUT_DIR, load_env

BASE = "https://services.leadconnectorhq.com"


def client():
    load_env()
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {os.environ['GHL_API_KEY']}",
        "Version": "2021-07-28",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s, os.environ["GHL_LOCATION_ID"]


def call(s, method, path, **kw):
    for attempt in range(4):
        time.sleep(0.15)
        r = s.request(method, BASE + path, timeout=30, **kw)
        if r.status_code != 429:
            return r
        time.sleep(2 ** (attempt + 1))
    return r


def scraper_contacts(s, loc):
    params = {"locationId": loc, "limit": 100}
    while True:
        r = call(s, "GET", "/contacts/", params=params)
        r.raise_for_status()
        data = r.json()
        batch = data.get("contacts", [])
        for c in batch:
            if (c.get("source") or "").lower() == "lead scraper":
                yield c
        meta = data.get("meta", {})
        if not batch or not meta.get("startAfterId"):
            return
        params.update(startAfterId=meta["startAfterId"], startAfter=meta["startAfter"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old_tags", nargs="*", help="search-area tags to replace")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="BACKUP")
    args = ap.parse_args()
    s, loc = client()

    if args.undo:
        with open(args.undo) as fh:
            changes = json.load(fh)
        for ch in changes:
            call(s, "DELETE", f"/contacts/{ch['id']}/tags", json={"tags": ch["added"]})
            call(s, "POST", f"/contacts/{ch['id']}/tags", json={"tags": ch["removed"]})
        print(f"Restored {len(changes)} contacts")
        return

    old = {t.lower() for t in args.old_tags}
    if not old:
        sys.exit("Name the area tags to replace, e.g. essex hertfordshire")

    changes, unknown, total = [], 0, 0
    for c in scraper_contacts(s, loc):
        total += 1
        tags = {t.lower() for t in c.get("tags", [])}
        have_old = sorted(tags & old)
        if not have_old:
            continue
        county = county_for_postcode(c.get("postalCode"))
        if not county:
            unknown += 1
            continue
        remove = [t for t in have_old if t != county.lower()]
        add = [county] if county.lower() not in tags else []
        if remove or add:
            changes.append({"id": c["id"], "name": c.get("companyName") or c.get("contactName"),
                            "postcode": c.get("postalCode"), "removed": remove, "added": add})

    summary = {}
    for ch in changes:
        key = f"{'+'.join(ch['removed']) or '-'} -> {'+'.join(ch['added']) or '(already tagged)'}"
        summary[key] = summary.get(key, 0) + 1
    print(f"Scraper contacts: {total}. To change: {len(changes)}. Unknown postcode (left alone): {unknown}")
    for k, v in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {k}")

    if not args.apply:
        print("\nDry run - nothing changed. Re-run with --apply.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    backup = os.path.join(OUTPUT_DIR, f"retag_backup_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(backup, "w") as fh:
        json.dump(changes, fh, indent=1)
    print(f"\nBackup: {backup}")

    failed = 0
    for i, ch in enumerate(changes, 1):
        # Add first, so a contact is never left with no area tag.
        ok = True
        if ch["added"]:
            ok = call(s, "POST", f"/contacts/{ch['id']}/tags", json={"tags": ch["added"]}).ok
        if ok and ch["removed"]:
            ok = call(s, "DELETE", f"/contacts/{ch['id']}/tags", json={"tags": ch["removed"]}).ok
        failed += not ok
        if i % 50 == 0:
            print(f"  {i}/{len(changes)}")
    print(f"Done: {len(changes) - failed} updated, {failed} failed")


if __name__ == "__main__":
    main()
