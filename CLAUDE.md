# Marketing Agency CRM

Two independent parts:
- The CRM web app (repo root: `index.html`, `app/`, `lib/`).
- A lead scraper in `leads/` that finds UK trade businesses on Thomson Local and
  uploads them into GoHighLevel (GHL). See `leads/README.md`.

## Lead requests ("find heating engineers in Surrey")

The user is non-technical and never uses the terminal. When they ask for leads,
run everything yourself and report back in plain English.

1. Install dependencies: `pip3 install -q -r leads/requirements.txt`
2. Check the credentials exist as environment variables (don't print their
   values): `GHL_API_KEY`, `GHL_LOCATION_ID`, `COMPANIES_HOUSE_API_KEY`. If any
   are missing, tell the user to add them in the cloud environment settings
   (environment name in the session title bar → Edit), never in chat or a file.
3. Run from `leads/`, in the background (a county takes 15–30 minutes):

   **Heating engineers** (the user's main niche):
   ```
   python3 -u scraper.py "heating engineers" "<area>" \
       --keyword-tag "Heat Pump=heat pump,air source,ground source,ashp,gshp,mcs" \
       --fallback-tag Boiler > output/run_<area>.log 2>&1
   ```
   **Any other niche:** `python3 -u scraper.py "<niche>" "<area>" --tag "<Tag>"`.
   Ask the user for the tag name if they haven't given one.

   For several areas, run them one after another, not in parallel, or Thomson
   Local blocks the requests.
4. When it finishes, report the summary at the end of the log: total leads,
   phone/mobile/email/owner/Facebook/Instagram counts, Heat Pump vs Boiler,
   the by-area breakdown, GHL created/updated/failed, and the Noah/Luca split.
   Send the CSV from `leads/output/` with SendUserFile.

For a first run in a new niche, or if the user asks for a test, add `--limit 20`.
Use `--no-upload` only when they want the CSV without touching GHL.

## Things the user should know (mention them when relevant, briefly)
- Thomson Local's area search is a radius: a county search returns roughly
  half that county plus neighbours. Area tags come from each lead's own
  postcode, so they're accurate. For better coverage, suggest searching the
  county's main towns as well.
- Numbers must be screened against TPS/CTPS before anyone cold calls them.
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
