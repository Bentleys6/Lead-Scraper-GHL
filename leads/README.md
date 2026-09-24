# Lead scraper: Heat Pump / Boiler Installers → GoHighLevel

```
pip3 install -r requirements.txt
python3 scraper.py "Surrey"                 # scrape, enrich, CSV, upload to GHL
python3 scraper.py "Stockport Cheshire" --no-upload
python3 -m unittest discover -s tests       # offline tests
```

Config: set the variables in `.env.example` as environment variables, or copy it to `.env`
(gitignored). CSVs are written to `output/`.

Notes:
- Location slugs on Thomson Local are often `town-county` (`stockport-cheshire`) or a postcode
  district (`m21`); a bare town name may return nothing.
- Companies House needs a free API key. Only exact, active company-name matches are used, so sole
  traders (not on Companies House) get no owner name rather than a wrong one.
- Facebook/Instagram go into GHL only if you create contact custom fields named exactly
  `Facebook` and `Instagram`; otherwise they're CSV-only.
- Each contact gets one opportunity in pipeline A or B (`PIPELINE_SPLIT`); re-runs skip contacts
  that already have one in either pipeline.
- Thomson Local's HTML selectors have not been verified live yet (network was blocked when this was written).
