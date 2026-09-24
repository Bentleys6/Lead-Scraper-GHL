# Lead Scraper

Finds UK trade businesses on Thomson Local, enriches them with owner names
(Companies House), emails and social links (their own websites), and uploads
them into GoHighLevel, split evenly between the Noah and Luca cold calling
pipelines.

Everything lives in [`leads/`](leads/). See [`leads/README.md`](leads/README.md)
for usage and settings.

```
pip3 install -r leads/requirements.txt
cd leads
python3 scraper.py "heating engineers" "kent" \
    --keyword-tag "Heat Pump=heat pump,air source,ground source,ashp,gshp,mcs" \
    --fallback-tag Boiler
```

Credentials (`GHL_API_KEY`, `GHL_LOCATION_ID`, `COMPANIES_HOUSE_API_KEY`) are
read from environment variables. See `leads/.env.example`.
