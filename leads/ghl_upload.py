"""GoHighLevel (LeadConnector API v2) uploader.

Upserts contacts, tags them, and drops each one into one of two cold-calling
pipelines as an opportunity. Safe to re-run: contacts are upserted (matched on
phone/email) and an opportunity is only created if the contact doesn't already
have one in either pipeline.
"""

import time

import requests

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
REQUEST_DELAY = 0.15


class GHLError(Exception):
    pass


def to_e164(phone):
    """07XXX XXXXXX -> +447XXXXXXXXX. Returns None if it isn't a plausible UK number."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+44" + digits[1:]
    return None


def split_name(full_name):
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def assign_pipelines(leads, mode="alternate"):
    """Return a list of "A"/"B", one per lead.

    alternate: leads are expected mobile-first, so alternating gives each
               pipeline an equal share of mobiles and landlines.
    mobile:    A = mobile numbers, B = everything else.
    """
    if mode == "mobile":
        return ["A" if lead.get("is_mobile") else "B" for lead in leads]
    if mode != "alternate":
        raise ValueError(f"Unknown PIPELINE_SPLIT mode: {mode!r} (use 'alternate' or 'mobile')")
    return ["A" if i % 2 == 0 else "B" for i in range(len(leads))]


class GHLClient:
    def __init__(self, api_key, location_id, session=None):
        if not api_key or not location_id:
            raise GHLError("GHL_API_KEY and GHL_LOCATION_ID must both be set")
        self.location_id = location_id
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method, path, **kwargs):
        for attempt in range(4):
            resp = self.session.request(method, BASE_URL + path, timeout=30, **kwargs)
            time.sleep(REQUEST_DELAY)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(float(resp.headers.get("Retry-After", 2 ** (attempt + 1))))
                continue
            if resp.status_code == 401:
                raise GHLError("401 Unauthorized — check GHL_API_KEY and the integration's scopes")
            if resp.status_code >= 400:
                raise GHLError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise GHLError(f"{method} {path} kept failing after retries ({resp.status_code})")

    # ---- custom fields (Facebook / Instagram) -------------------------------

    def find_custom_field_ids(self, names=("Facebook", "Instagram")):
        """Map wanted field names -> custom field ids. Missing fields/scope just return {}."""
        try:
            data = self._request("GET", f"/locations/{self.location_id}/customFields",
                                 params={"model": "contact"})
        except GHLError as e:
            print(f"  (custom fields unavailable, Facebook/Instagram stay CSV-only: {e})")
            return {}
        wanted = {n.lower(): n for n in names}
        found = {}
        for field in data.get("customFields", []):
            key = (field.get("name") or "").strip().lower()
            if key in wanted:
                found[wanted[key]] = field["id"]
        return found

    # ---- contacts -----------------------------------------------------------

    def upsert_contact(self, lead, custom_field_ids=None):
        """Returns (contact_id, is_new)."""
        first, last = split_name(lead.get("owner_name"))
        body = {
            "locationId": self.location_id,
            "companyName": lead["business_name"],
            "source": "Lead Scraper",
            "country": "GB",
        }
        if first:
            body["firstName"], body["lastName"] = first, last
        else:
            body["name"] = lead["business_name"]
        phone = to_e164(lead.get("phone"))
        if phone:
            body["phone"] = phone
        if lead.get("email"):
            body["email"] = lead["email"]
        for src, dest in (("website", "website"), ("street", "address1"),
                          ("city", "city"), ("postcode", "postalCode")):
            if lead.get(src):
                body[dest] = lead[src]
        custom = []
        for field_name, lead_key in (("Facebook", "facebook"), ("Instagram", "instagram")):
            field_id = (custom_field_ids or {}).get(field_name)
            if field_id and lead.get(lead_key):
                custom.append({"id": field_id, "field_value": lead[lead_key]})
        if custom:
            body["customFields"] = custom

        data = self._request("POST", "/contacts/upsert", json=body)
        return data["contact"]["id"], bool(data.get("new"))

    def add_tags(self, contact_id, tags):
        # Separate call so existing contacts keep the tags they already have.
        self._request("POST", f"/contacts/{contact_id}/tags", json={"tags": tags})

    # ---- pipelines / opportunities -----------------------------------------

    def resolve_pipelines(self, name_a, name_b, stage_name=None):
        data = self._request("GET", "/opportunities/pipelines",
                             params={"locationId": self.location_id})
        pipelines = {p["name"].strip().lower(): p for p in data.get("pipelines", [])}
        resolved = {}
        for key, name in (("A", name_a), ("B", name_b)):
            p = pipelines.get((name or "").strip().lower())
            if not p:
                available = ", ".join(sorted(x["name"] for x in data.get("pipelines", [])))
                raise GHLError(f"Pipeline {name!r} not found in GHL. Available: {available}")
            stages = sorted(p.get("stages", []), key=lambda s: s.get("position", 0))
            if not stages:
                raise GHLError(f"Pipeline {name!r} has no stages")
            stage = stages[0]
            if stage_name:
                stage = next((s for s in stages if s["name"].strip().lower() == stage_name.strip().lower()), None)
                if not stage:
                    raise GHLError(f"Stage {stage_name!r} not found in pipeline {name!r}")
            resolved[key] = {"name": p["name"], "pipeline_id": p["id"], "stage_id": stage["id"]}
        return resolved

    def has_opportunity(self, contact_id, pipeline_ids):
        data = self._request("GET", "/opportunities/search",
                             params={"location_id": self.location_id, "contact_id": contact_id})
        return any(o.get("pipelineId") in pipeline_ids for o in data.get("opportunities", []))

    def create_opportunity(self, contact_id, pipeline, title):
        self._request("POST", "/opportunities/", json={
            "locationId": self.location_id,
            "pipelineId": pipeline["pipeline_id"],
            "pipelineStageId": pipeline["stage_id"],
            "contactId": contact_id,
            "name": title,
            "status": "open",
        })


def upload_leads(leads, config, tags):
    """Upload leads (already sorted mobile-first). Returns a stats dict."""
    stats = {"created": 0, "updated": 0, "failed": 0, "skipped_no_contact": 0,
             "opp_created": 0, "opp_existing": 0, "opp_failed": 0,
             "pipeline_counts": {}}
    client = GHLClient(config.get("GHL_API_KEY"), config.get("GHL_LOCATION_ID"))

    pipelines = None
    if config.get("GHL_PIPELINE_A") and config.get("GHL_PIPELINE_B"):
        pipelines = client.resolve_pipelines(config["GHL_PIPELINE_A"], config["GHL_PIPELINE_B"],
                                             config.get("GHL_PIPELINE_STAGE") or None)
        pipeline_ids = {p["pipeline_id"] for p in pipelines.values()}
    else:
        print("  GHL_PIPELINE_A / GHL_PIPELINE_B not set — contacts only, no pipeline split.")

    custom_field_ids = client.find_custom_field_ids()
    uploadable = [l for l in leads if to_e164(l.get("phone")) or l.get("email")]
    stats["skipped_no_contact"] = len(leads) - len(uploadable)
    assignment = assign_pipelines(uploadable, config.get("PIPELINE_SPLIT") or "alternate")

    for i, (lead, slot) in enumerate(zip(uploadable, assignment), 1):
        try:
            contact_id, is_new = client.upsert_contact(lead, custom_field_ids)
            client.add_tags(contact_id, tags)
            stats["created" if is_new else "updated"] += 1
        except (GHLError, requests.RequestException, KeyError) as e:
            stats["failed"] += 1
            print(f"  [{i}/{len(uploadable)}] FAILED {lead['business_name']}: {e}")
            continue

        if pipelines:
            pipeline = pipelines[slot]
            try:
                if client.has_opportunity(contact_id, pipeline_ids):
                    stats["opp_existing"] += 1
                else:
                    client.create_opportunity(contact_id, pipeline, lead["business_name"])
                    stats["opp_created"] += 1
                    stats["pipeline_counts"][pipeline["name"]] = stats["pipeline_counts"].get(pipeline["name"], 0) + 1
            except (GHLError, requests.RequestException) as e:
                stats["opp_failed"] += 1
                print(f"  [{i}/{len(uploadable)}] opportunity FAILED {lead['business_name']}: {e}")

        if i % 25 == 0:
            print(f"  uploaded {i}/{len(uploadable)}")
    return stats
