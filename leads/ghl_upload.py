"""GoHighLevel uploader: upserts contacts, tags them, and splits new leads
evenly across the cold-calling pipelines."""

import json
import os
import time

import requests

BASE = "https://services.leadconnectorhq.com"
DELAY = 0.15
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pipeline_state.json")


def to_e164(phone):
    """07XXX XXXXXX -> +447XXXXXXXXX. Returns '' if not a usable UK number."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+44" + digits[1:]
    return ""


def split_name(full):
    parts = (full or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


class GHLClient:
    def __init__(self, token, location_id, pipeline_names):
        self.location_id = location_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.pipelines = self._resolve_pipelines(pipeline_names)
        self.social_fields = self._resolve_social_fields()

    def _request(self, method, path, **kwargs):
        time.sleep(DELAY)
        for attempt in range(3):
            resp = self.session.request(method, BASE + path, timeout=20, **kwargs)
            if resp.status_code != 429:
                return resp
            time.sleep(2 ** (attempt + 1))
        return resp

    def _resolve_pipelines(self, names):
        """Map each pipeline name to (pipeline_id, first_stage_id)."""
        resp = self._request("GET", "/opportunities/pipelines", params={"locationId": self.location_id})
        if resp.status_code != 200:
            raise RuntimeError(
                f"Could not list pipelines ({resp.status_code}): {resp.text[:200]}. "
                "Check the token has opportunities.readonly and opportunities.write scopes."
            )
        by_name = {p["name"].strip().lower(): p for p in resp.json().get("pipelines", [])}
        resolved = []
        for name in names:
            p = by_name.get(name.strip().lower())
            if not p:
                raise RuntimeError(f"Pipeline '{name}' not found. Available: {sorted(by_name)}")
            stages = sorted(p.get("stages", []), key=lambda s: s.get("position", 0))
            if not stages:
                raise RuntimeError(f"Pipeline '{name}' has no stages")
            resolved.append({"name": p["name"], "id": p["id"], "stage_id": stages[0]["id"]})
        return resolved

    def _resolve_social_fields(self):
        """Optional: populate contact custom fields whose name contains
        'facebook' / 'instagram' (e.g. "Facebook Page"; GHL reserves plain
        "Facebook"). Silently skipped if absent or the scope is missing."""
        resp = self._request("GET", f"/locations/{self.location_id}/customFields")
        if resp.status_code != 200:
            return {}
        fields = {}
        for f in resp.json().get("customFields", []):
            name = (f.get("name") or "").lower()
            for key in ("facebook", "instagram"):
                if key in name and key not in fields:
                    fields[key] = f["id"]
        return fields

    def _next_pipeline(self):
        """Round-robin persisted across runs so the split stays even over time."""
        try:
            with open(STATE_FILE) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        idx = state.get("next", 0) % len(self.pipelines)
        state["next"] = idx + 1
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)
        return self.pipelines[idx]

    def _has_opportunity(self, contact_id):
        resp = self._request("GET", "/opportunities/search",
                             params={"location_id": self.location_id, "contact_id": contact_id})
        if resp.status_code != 200:
            return True  # fail safe: don't risk creating duplicates
        ours = {p["id"] for p in self.pipelines}
        return any(o.get("pipelineId") in ours for o in resp.json().get("opportunities", []))

    def upload(self, lead, tags):
        """Returns (status, pipeline_name) where status is created/updated/skipped/failed."""
        phone = to_e164(lead.get("phone"))
        email = lead.get("email") or ""
        if not phone and not email:
            return "skipped", None

        first, last = split_name(lead.get("owner_name"))
        body = {
            "locationId": self.location_id,
            "companyName": lead["business_name"],
            "address1": lead.get("street", ""),
            "city": lead.get("city", ""),
            "postalCode": lead.get("postcode", ""),
            "country": "GB",
            "website": lead.get("website", ""),
            "source": "Lead Scraper",
        }
        if first:
            body["firstName"], body["lastName"] = first, last
        else:
            body["name"] = lead["business_name"]
        if phone:
            body["phone"] = phone
        if email:
            body["email"] = email
        custom = [{"id": fid, "field_value": lead[key]}
                  for key, fid in self.social_fields.items() if lead.get(key)]
        if custom:
            body["customFields"] = custom

        # Tags are added via the tags endpoint rather than in the upsert body,
        # because upsert replaces the tag list on existing contacts.
        resp = self._request("POST", "/contacts/upsert", json=body)
        if resp.status_code not in (200, 201):
            print(f"  ! GHL upsert failed for {lead['business_name']}: {resp.status_code} {resp.text[:150]}")
            return "failed", None
        data = resp.json()
        contact_id = data.get("contact", {}).get("id")
        is_new = bool(data.get("new"))

        self._request("POST", f"/contacts/{contact_id}/tags", json={"tags": tags})

        pipeline_name = None
        if is_new or not self._has_opportunity(contact_id):
            pipeline = self._next_pipeline()
            opp = self._request("POST", "/opportunities/", json={
                "locationId": self.location_id,
                "pipelineId": pipeline["id"],
                "pipelineStageId": pipeline["stage_id"],
                "contactId": contact_id,
                "name": lead["business_name"],
                "status": "open",
                "source": "Lead Scraper",
            })
            if opp.status_code in (200, 201):
                pipeline_name = pipeline["name"]
            else:
                print(f"  ! Opportunity failed for {lead['business_name']}: {opp.status_code} {opp.text[:150]}")

        return ("created" if is_new else "updated"), pipeline_name


def upload_leads(leads, tags, token, location_id, pipeline_names):
    client = GHLClient(token, location_id, pipeline_names)
    result = {"created": 0, "updated": 0, "failed": 0, "skipped": 0}
    per_pipeline = {p["name"]: 0 for p in client.pipelines}
    for i, lead in enumerate(leads, 1):
        status, pipeline = client.upload(lead, tags)
        result[status] += 1
        if pipeline:
            per_pipeline[pipeline] += 1
        if i % 25 == 0:
            print(f"  uploaded {i}/{len(leads)}")
    result["pipelines"] = per_pipeline
    if not client.social_fields:
        result["note"] = ("No Facebook/Instagram custom fields found in GHL, so those "
                          "links are in the CSV only.")
    return result
