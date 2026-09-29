#!/usr/bin/env python3
"""Screen GHL leads still in "New Leads" against TPS/CTPS (TPSCheck.uk) and
delete the registered ones. Contacts with any opportunity outside a pipeline's
first stage have been worked on, so they are never touched.

    python3 tps_screen.py            # dry run: screen and report
    python3 tps_screen.py --apply    # delete registered New Leads contacts

Results are cached in output/tps_cache.json so a number is only paid for once.
Screening stops cleanly when the TPSCheck allowance runs out; re-run later to
carry on. Deleted contacts are saved to output/tps_deleted_<time>.json first.
"""

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime

import requests

from retag_areas import call, client
from scraper import OUTPUT_DIR

TPS_BASE = "https://api.tpscheck.uk"
CACHE = os.path.join(OUTPUT_DIR, "tps_cache.json")


class OutOfChecks(Exception):
    pass


def tps_session():
    s = requests.Session()
    s.headers.update({"Authorization": f"Token {os.environ['TPS_API_KEY']}",
                      "Content-Type": "application/json"})
    return s


def tps_remaining(ts):
    r = ts.get(TPS_BASE + "/credits/", timeout=20)
    r.raise_for_status()
    return r.json()


def tps_check(ts, phone):
    for attempt in range(4):
        time.sleep(0.2)
        r = ts.post(TPS_BASE + "/check", json={"phone": phone}, timeout=30)
        if r.status_code != 429:
            break
        time.sleep(2 ** (attempt + 1))
    if r.status_code in (401, 402, 403, 429):
        raise OutOfChecks(f"{r.status_code} {r.text[:200]}")
    r.raise_for_status()
    d = r.json()
    return {"tps": bool(d.get("tps")), "ctps": bool(d.get("ctps")), "valid": d.get("valid", True)}


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
    ts = tps_session()

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

    try:
        with open(CACHE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}

    credits = tps_remaining(ts)
    print(f"Contacts with opportunities: {len(by_contact)} "
          f"(still in New Leads: {len(new_only)}, moved on and kept: {worked})")
    print(f"TPSCheck: {credits.get('requests_remaining')} checks left on the {credits.get('plan')} plan")

    left = credits.get("requests_remaining") or 0
    registered, clean, unscreened, no_phone, out_of_checks = [], 0, 0, 0, left <= 0
    for cid, opps in new_only:
        phone = (opps[0].get("contact") or {}).get("phone")
        if not phone:
            no_phone += 1
            continue
        if phone not in cache:
            if out_of_checks:
                unscreened += 1
                continue
            try:
                cache[phone] = tps_check(ts, phone)
                left -= 1
                out_of_checks = left <= 0
            except OutOfChecks as e:
                print(f"  TPSCheck allowance used up: {e}")
                out_of_checks = True
                unscreened += 1
                continue
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(CACHE, "w") as fh:
                json.dump(cache, fh)
        res = cache[phone]
        if res["tps"] or res["ctps"]:
            registered.append({"id": cid, "phone": phone, "tps": res["tps"], "ctps": res["ctps"],
                               "name": opps[0].get("name"),
                               "pipelines": sorted({stages[o["pipelineId"]][0] for o in opps}),
                               "opportunity_ids": [o["id"] for o in opps]})
        else:
            clean += 1

    per_pipe = defaultdict(int)
    for r in registered:
        for p in r["pipelines"]:
            per_pipe[p] += 1
    print(f"\nScreened New Leads: {len(registered) + clean}")
    print(f"  On TPS/CTPS: {len(registered)} "
          f"(TPS {sum(r['tps'] for r in registered)}, CTPS {sum(r['ctps'] for r in registered)})")
    for p, n in sorted(per_pipe.items()):
        print(f"    {p}: {n}")
    print(f"  Clear: {clean}")
    print(f"  Not screened yet (out of checks): {unscreened}")
    if no_phone:
        print(f"  No phone number: {no_phone}")

    if not args.apply:
        print("\nDry run - nothing deleted. Re-run with --apply.")
        return
    if not registered:
        print("\nNothing to delete.")
        return

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
