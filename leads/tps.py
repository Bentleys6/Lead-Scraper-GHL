"""TPS/CTPS screening through TPSCheck.uk, with a register of every number
already checked (tps_register.json, committed so it survives between sessions).

Registered numbers are never checked again and never uploaded. Clear numbers
are trusted for RECHECK_DAYS, then checked again (the ICO expects screening
within 28 days of a call). Numbers are stored hashed, not in plain text.
"""

import hashlib
import json
import os
import time
from datetime import date, timedelta

import requests

TPS_BASE = "https://api.tpscheck.uk"
REGISTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tps_register.json")
RECHECK_DAYS = 28


class OutOfChecks(Exception):
    pass


def e164(phone):
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+44" + digits[1:]
    return ""


def _key(number):
    return hashlib.sha256(number.encode()).hexdigest()[:20]


class Screener:
    """screen(phone) -> "registered", "clear", "unchecked" (out of checks) or "" (no usable number)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {os.environ['TPS_API_KEY']}",
                                     "Content-Type": "application/json"})
        try:
            with open(REGISTER) as fh:
                self.register = json.load(fh)
        except (OSError, ValueError):
            self.register = {}
        r = self.session.get(TPS_BASE + "/credits/", timeout=20)
        r.raise_for_status()
        self.credits = r.json()
        self.left = self.credits.get("requests_remaining") or 0
        self.checks_used = 0

    def _save(self):
        with open(REGISTER, "w") as fh:
            json.dump(self.register, fh, indent=0, sort_keys=True)

    def _check(self, number):
        for attempt in range(4):
            time.sleep(0.2)
            r = self.session.post(TPS_BASE + "/check", json={"phone": number}, timeout=30)
            if r.status_code != 429:
                break
            time.sleep(2 ** (attempt + 1))
        if r.status_code in (401, 402, 403, 429):
            raise OutOfChecks(f"{r.status_code} {r.text[:200]}")
        r.raise_for_status()
        d = r.json()
        return bool(d.get("tps")), bool(d.get("ctps"))

    def screen(self, phone):
        number = e164(phone)
        if not number:
            return ""
        k = _key(number)
        entry = self.register.get(k)
        if entry and (entry["tps"] or entry["ctps"]):
            return "registered"
        if entry and date.fromisoformat(entry["checked"]) >= date.today() - timedelta(days=RECHECK_DAYS):
            return "clear"
        if self.left <= 0:
            return "unchecked"
        try:
            tps, ctps = self._check(number)
        except OutOfChecks:
            self.left = 0
            return "unchecked"
        self.left -= 1
        self.checks_used += 1
        self.register[k] = {"tps": tps, "ctps": ctps, "checked": date.today().isoformat()}
        self._save()
        return "registered" if tps or ctps else "clear"
