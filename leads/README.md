# Lead scraper

Scrapes Thomson Local for a niche in a UK area, enriches each business from its
website (email, Facebook, Instagram, mobile) and Companies House (director name),
writes a CSV to `output/`, and upserts every contact into GoHighLevel. New
contacts get an opportunity in the first stage of one of the pipelines in
`GHL_PIPELINES`, alternating so the split stays even across runs.

```
pip3 install -r requirements.txt
python3 scraper.py "roofers" "surrey" --tag Roofer
python3 scraper.py "kitchen fitters" "west yorkshire" --tag KBB --no-upload --limit 20
```

Credentials come from environment variables or `leads/.env` (see `.env.example`).

GHL private integration scopes: `contacts.write`, `contacts.readonly`,
`opportunities.write`, `opportunities.readonly`, and optionally
`locations/customFields.readonly`: with that one, if the location has custom fields named
"Facebook"/"Instagram" they are filled in.

Notes
- Contacts with neither a phone nor an email are left out of GHL (they are still in the CSV).
- Tags are added through the tags endpoint, so existing tags on existing contacts are kept.
- An unrecognised area makes Thomson Local return nationwide results; the script stops instead.
