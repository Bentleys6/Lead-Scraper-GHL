#!/usr/bin/env python3
"""Delete scraper contacts (source "Lead Scraper") whose phone isn't a UK 07
mobile, along with their opportunities. Other contacts are never touched.

    python3 delete_non_mobile.py            # dry run
    python3 delete_non_mobile.py --apply    # delete (backup written first)
"""

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime

from retag_areas import call, client, scraper_contacts
from scraper import OUTPUT_DIR


def opportunities_by_contact(s, loc):
    r = call(s, "GET", "/opportunities/pipelines", params={"locationId": loc})
    r.raise_for_status()
    stages = {st["id"]: st["name"] for p in r.json().get("pipelines", []) for st in p.get("stages", [])}
    out, page = defaultdict(list), 1
    while True:
        r = call(s, "GET", "/opportunities/search",
                 params={"location_id": loc, "limit": 100, "page": page})
        r.raise_for_status()
        batch = r.json().get("opportunities", [])
        for o in batch:
            out[o["contactId"]].append({"id": o["id"], "stage": stages.get(o.get("pipelineStageId"), "?")})
        if len(batch) < 100:
            return out
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    s, loc = client()

    opps = opportunities_by_contact(s, loc)
    targets, kept = [], 0
    for c in scraper_contacts(s, loc):
        if (c.get("phone") or "").startswith("+447"):
            kept += 1
        else:
            targets.append({"contact": c, "opportunities": opps.get(c["id"], [])})

    by_stage = Counter(", ".join(sorted({o["stage"] for o in t["opportunities"]})) or "no opportunity"
                       for t in targets)
    print(f"Scraper contacts with a 07 number (kept): {kept}")
    print(f"Without a 07 number (to delete): {len(targets)}")
    for stage, n in by_stage.most_common():
        print(f"  {n:>4}  {stage}")

    if not args.apply:
        print("\nDry run - nothing deleted. Re-run with --apply.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    backup = os.path.join(OUTPUT_DIR, f"non_mobile_deleted_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(backup, "w") as fh:
        json.dump(targets, fh, indent=1)
    print(f"\nBackup: {backup}")

    deleted = failed = 0
    for i, t in enumerate(targets, 1):
        for o in t["opportunities"]:
            call(s, "DELETE", f"/opportunities/{o['id']}")
        if call(s, "DELETE", f"/contacts/{t['contact']['id']}").ok:
            deleted += 1
        else:
            failed += 1
        if i % 50 == 0:
            print(f"  {i}/{len(targets)}")
    print(f"Deleted {deleted} contacts, {failed} failed")


if __name__ == "__main__":
    main()
