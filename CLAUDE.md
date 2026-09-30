# Lead Scraper

A lead scraper in `leads/` that finds UK trade businesses on Thomson Local and
uploads them into GoHighLevel (GHL), the user's CRM. See `leads/README.md`.
(An older custom CRM web app used to live here; it was removed and isn't used.)

## Lead requests ("find heating engineers in Surrey")

The user is non-technical and never uses the terminal. When they ask for leads,
run everything yourself and report back in plain English.

1. Install dependencies: `pip3 install -q -r leads/requirements.txt`
2. Check the credentials exist as environment variables (don't print their
   values): `GHL_API_KEY`, `GHL_LOCATION_ID`, `COMPANIES_HOUSE_API_KEY`, `TPS_API_KEY`. If any
   are missing, tell the user to add them in the cloud environment settings
   (environment name in the session title bar → Edit), never in chat or a file.
3. Run from `leads/`, in the background (a county takes 15–30 minutes):

   **Heating engineers** (the user's main niche):
   ```
   python3 -u scraper.py "heating engineers" "<area>" \
       --keyword-tag "Heat Pump=heat pump,air source,ground source,ashp,gshp,mcs" \
       --fallback-tag Boiler --mobile-only > output/run_<area>.log 2>&1
   ```
   **Any other niche:** `python3 -u scraper.py "<niche>" "<area>" --tag "<Tag>" --mobile-only`.

   `--mobile-only` keeps only leads with a 07 number (the user asked for this);
   leave it off only if they ask for landlines too. It roughly halves the leads.
   Ask the user for the tag name if they haven't given one.

   For several areas, run them one after another, not in parallel, or Thomson
   Local blocks the requests.
4. When it finishes, report the summary at the end of the log: total leads,
   phone/mobile/email/owner/Facebook/Instagram counts, Heat Pump vs Boiler,
   the by-area breakdown, how many were dropped for having no 07 number, TPS removed / not checked, GHL created/updated/failed,
   and the Noah/Luca split. Send the CSV from `leads/output/` with SendUserFile.
5. Commit and push `leads/tps_register.json` after every scrape or TPS run.
   It's the only record of which numbers were already checked; without it,
   TPS numbers get re-checked (paid for again) and can be re-uploaded.

For a first run in a new niche, or if the user asks for a test, add `--limit 20`.
Use `--no-upload` only when they want the CSV without touching GHL.

## Things the user should know (mention them when relevant, briefly)
- Thomson Local's area search is a radius: a county search returns roughly
  half that county plus neighbours. Area tags come from each lead's own
  postcode, so they're accurate. For better coverage, suggest searching the
  county's main towns as well.
- The scraper screens every number against TPS/CTPS (TPSCheck.uk) before
  upload and drops registered ones. Numbers in `leads/tps_register.json` aren't
  checked again: TPS ones never, clear ones for 28 days (ICO expects screening
  within 28 days of a call). If the TPSCheck allowance runs out, leads are
  still uploaded but tagged "TPS Not Checked" and must not be called.
- `leads/tps_screen.py` screens contacts still in "New Leads" and, with
  `--apply`, deletes the TPS/CTPS ones (the user asked for deletion). Contacts
  moved past New Leads are never touched. Run it when the allowance renews to
  clear any "TPS Not Checked" leads. Send the user the `tps_deleted_*.json`
  backup it writes.
- Heat pump firms are roughly 4% of heating engineer leads.

## GHL facts
- New leads go into the first stage ("New Leads") of "Noah Cold Calling
  Pipeline" and "Luca Cold Calling Pipeline", alternating. The rotation is
  stored in `leads/.pipeline_state.json`, which doesn't survive between
  sessions, so each session starts with Noah. That's fine over time.
- Upsert matches on phone/email, so re-running an area doesn't duplicate
  contacts or opportunities.
- `leads/retag_areas.py` fixes area tags on older contacts (dry run by
  default; `--apply`; `--undo <backup>`).
- The GHL key can't read workflows. Before any bulk change to tags or
  contacts, ask whether workflows trigger on them.
