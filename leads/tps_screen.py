#!/usr/bin/env python3
"""Screen GHL leads still in "New Leads" against TPS/CTPS (TPSCheck.uk) and
delete the registered ones. Contacts with any opportunity outside a pipeline's
first stage have been worked on, so they are never touched.

    python3 tps_screen.py            # dry run: screen and report
    python3 tps_screen.py --apply    # delete registered New Leads contacts

Every result goes in tps_register.json (see tps.py), so registered numbers are
never paid for twice. Screening stops cleanly when the TPSCheck allowance runs
out; re-run later to carry on. Deleted contacts are saved to output/tps_deleted_<time>.json first.
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime

from retag_areas import call, client
from scraper import OUTPUT_DIR
from tps import Screener

UNCHECKED = "TPS Not Checked"  # tag the scraper adds when it ran out of checks


def first_stages(s, loc):
    r = call(s, "GET", "/opportunities/pipelines", params={"locationId": loc})
    r.raise_for_status()
    out = {}
    for p in r.json().get("pipelines", []):
        stages = sorted(p.get("stages", []), key=lambda st: st.get("position", 0))
        if stages:
            out[p["id"]] = (p["name"], stages[0]["id"])
    return out


def all_opportunities(s, loc):
    page = 1
    while True:
        r = call(s, "GET", "/opportunities/search",
                 params={"location_id": loc, "limit": 100, "page": page})
        r.raise_for_status()
        batch = r.json().get("opportunities", [])
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete registered New Leads contacts")
    args = ap.parse_args()
    s, loc = client()

    stages = first_stages(s, loc)
    by_contact = defaultdict(list)
    for o in all_opportunities(s, loc):
        by_contact[o["contactId"]].append(o)

    new_only, worked = [], 0
    for cid, opps in by_contact.items():
        if all(stages.get(o.get("pipelineId"), (None, None))[1] == o.get("pipelineStageId") for o in opps):
            new_only.append((cid, opps))
        else:
            worked += 1

    screener = Screener()
    print(f"Contacts with opportunities: {len(by_contact)} "
          f"(still in New Leads: {len(new_only)}, moved on and kept: {worked})")
    print(f"TPSCheck: {screener.left} checks left on the {screener.credits.get('plan')} plan")

    registered, clean, unscreened, no_phone, now_clear = [], 0, 0, 0, []
    for cid, opps in new_only:
        phone = (opps[0].get("contact") or {}).get("phone")
        status = screener.screen(phone)
        if not status:
            no_phone += 1
        elif status == "unchecked":
            unscreened += 1
        elif status == "registered":
            registered.append({"id": cid, "phone": phone, "name": opps[0].get("name"),
                               "pipelines": sorted({stages[o["pipelineId"]][0] for o in opps}),
                               "opportunity_ids": [o["id"] for o in opps]})
        else:
            clean += 1
            tags = {x.lower() for x in (opps[0].get("contact") or {}).get("tags") or []}
            if UNCHECKED.lower() in tags:
                now_clear.append(cid)
    per_pipe = defaultdict(int)
    for r in registered:
        for p in r["pipelines"]:
            per_pipe[p] += 1
    print(f"\nScreened New Leads: {len(registered) + clean}")
    print(f"  On TPS/CTPS: {len(registered)}")
    for p, n in sorted(per_pipe.items()):
        print(f"    {p}: {n}")
    print(f"  Clear: {clean}" + (f" ({len(now_clear)} still tagged '{UNCHECKED}')" if now_clear else ""))
    print(f"  Not screened yet (out of checks): {unscreened}")
    if no_phone:
        print(f"  No phone number: {no_phone}")
    print(f"  (TPSCheck checks used this run: {screener.checks_used})")

    if not args.apply:
        print("\nDry run - nothing deleted. Re-run with --apply.")
        return
    for cid in now_clear:
        call(s, "DELETE", f"/contacts/{cid}/tags", json={"tags": [UNCHECKED]})
    if now_clear:
        print(f"\nRemoved '{UNCHECKED}' from {len(now_clear)} contacts now screened clear")
    if not registered:
        print("\nNothing to delete.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    backup = os.path.join(OUTPUT_DIR, f"tps_deleted_{datetime.now():%Y%m%d_%H%M%S}.json")
    full = []
    for r in registered:
        c = call(s, "GET", f"/contacts/{r['id']}")
        full.append({**r, "contact": c.json().get("contact") if c.ok else None})
    with open(backup, "w") as fh:
        json.dump(full, fh, indent=1)
    print(f"\nBackup: {backup}")

    deleted = failed = 0
    for r in registered:
        for oid in r["opportunity_ids"]:
            call(s, "DELETE", f"/opportunities/{oid}")
        if call(s, "DELETE", f"/contacts/{r['id']}").ok:
            deleted += 1
        else:
            failed += 1
    print(f"Deleted {deleted} contacts, {failed} failed")


if __name__ == "__main__":
    main()
