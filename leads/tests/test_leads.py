"""Offline tests: python3 -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ghl_upload  # noqa: E402
import scraper  # noqa: E402

LISTING_HTML = """
<ul>
<li class="listing clearFix">
  <h2 class="businessName">Warm Homes Heating Ltd</h2>
  <a href="tel:01483 123456">Call</a><a href="tel:07700 900123">Mobile</a>
  <span itemprop="streetAddress">1 High St,</span>
  <span itemprop="addressLocality">Guildford</span>
  <span itemprop="postalCode">GU1 1AA</span>
  <a data-yext="url" href="www.warmhomes.co.uk">Website</a>
  <a href="/contactbusiness/123/warm-homes">Email</a>
</li>
<li class="listing clearFix">
  <h2 class="businessName">Boiler Bob</h2>
  <a href="tel:02079460000">Call</a>
</li>
</ul>
"""

WEBSITE_HTML = """
<a href="mailto:info@warmhomes.co.uk">Email</a>
<p>support@wix.com  logo@2x.png  Call us on 07911 123 456</p>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
<a href="https://www.facebook.com/warmhomes?ref=1">FB</a>
<a href="https://instagram.com/warmhomes">IG</a>
<a href="/contact-us">Contact</a>
"""


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.content, self.text, self.headers = b"x", str(payload), {}

    def json(self):
        return self._payload


class ScraperTests(unittest.TestCase):
    def test_parse_listings_prefers_mobile(self):
        leads = scraper.parse_listings(LISTING_HTML)
        self.assertEqual(len(leads), 2)
        a = leads[0]
        self.assertEqual(a["business_name"], "Warm Homes Heating Ltd")
        self.assertEqual(a["phone"], "07700900123")
        self.assertEqual((a["street"], a["city"], a["postcode"]), ("1 High St", "Guildford", "GU1 1AA"))
        self.assertEqual(a["website"], "http://www.warmhomes.co.uk")
        self.assertEqual(leads[1]["phone"], "02079460000")

    def test_mobile_detection(self):
        self.assertTrue(scraper.is_mobile("07911123456"))
        self.assertTrue(scraper.is_mobile(scraper.normalise_phone("+44 7911 123456")))
        self.assertFalse(scraper.is_mobile("07011123456"))  # personal numbering
        self.assertFalse(scraper.is_mobile("01483123456"))

    def test_website_extraction(self):
        out = scraper.extract_from_page(WEBSITE_HTML, "https://www.warmhomes.co.uk/")
        self.assertEqual(out["emails"], ["info@warmhomes.co.uk"])
        self.assertEqual(out["facebook"], "https://www.facebook.com/warmhomes")
        self.assertEqual(out["instagram"], "https://instagram.com/warmhomes")
        self.assertIn("07911123456", out["mobiles"])
        self.assertEqual(out["contact_url"], "https://www.warmhomes.co.uk/contact-us")

    def test_officer_name(self):
        self.assertEqual(scraper.format_officer_name("SMITH, John Paul"), "John Smith")

    def test_company_name_match(self):
        self.assertEqual(scraper.normalise_name("Warm Homes Heating Ltd"),
                         scraper.normalise_name("WARM HOMES HEATING LIMITED"))

    def test_sort_mobile_first(self):
        leads = [{"phone": "01483123456"}, {"phone": None}, {"phone": "07911123456"}]
        self.assertEqual([l["phone"] for l in scraper.sort_leads(leads)],
                         ["07911123456", "01483123456", None])

    def test_location_slug(self):
        self.assertEqual(scraper.slugify("West Yorkshire"), "west-yorkshire")


class GHLTests(unittest.TestCase):
    def test_e164(self):
        self.assertEqual(ghl_upload.to_e164("07911 123456"), "+447911123456")
        self.assertEqual(ghl_upload.to_e164("020 7946 0000"), "+442079460000")
        self.assertIsNone(ghl_upload.to_e164("123"))

    def test_split_modes(self):
        leads = [{"is_mobile": True}, {"is_mobile": True}, {"is_mobile": False}]
        self.assertEqual(ghl_upload.assign_pipelines(leads, "alternate"), ["A", "B", "A"])
        self.assertEqual(ghl_upload.assign_pipelines(leads, "mobile"), ["A", "A", "B"])

    @mock.patch.object(ghl_upload, "REQUEST_DELAY", 0)
    def test_upload_flow(self):
        calls = []

        def fake_request(method, url, **kw):
            path = url.replace(ghl_upload.BASE_URL, "")
            calls.append((method, path, kw.get("json")))
            if path == "/opportunities/pipelines":
                return FakeResponse(200, {"pipelines": [
                    {"id": "pA", "name": "Cold A", "stages": [{"id": "sA", "name": "New", "position": 0}]},
                    {"id": "pB", "name": "Cold B", "stages": [{"id": "sB", "name": "New", "position": 0}]},
                ]})
            if path.endswith("/customFields"):
                return FakeResponse(200, {"customFields": [{"id": "cf1", "name": "Facebook"}]})
            if path == "/contacts/upsert":
                n = sum(1 for c in calls if c[1] == "/contacts/upsert")
                return FakeResponse(200, {"new": n == 1, "contact": {"id": f"c{n}"}})
            if path == "/opportunities/search":
                return FakeResponse(200, {"opportunities": []})
            return FakeResponse(200, {})

        leads = [
            {"business_name": "A Ltd", "owner_name": "John Smith", "phone": "07911123456",
             "email": "", "website": "", "facebook": "https://facebook.com/a", "instagram": "",
             "street": "", "city": "", "postcode": "", "is_mobile": True},
            {"business_name": "B Ltd", "owner_name": "", "phone": "01483123456", "email": "",
             "website": "", "facebook": "", "instagram": "", "street": "", "city": "",
             "postcode": "", "is_mobile": False},
            {"business_name": "No Contact", "owner_name": "", "phone": None, "email": "",
             "website": "", "facebook": "", "instagram": "", "street": "", "city": "",
             "postcode": "", "is_mobile": False},
        ]
        config = {"GHL_API_KEY": "k", "GHL_LOCATION_ID": "loc", "GHL_PIPELINE_A": "cold a",
                  "GHL_PIPELINE_B": "Cold B", "PIPELINE_SPLIT": "alternate"}
        with mock.patch("requests.Session.request", side_effect=fake_request):
            stats = ghl_upload.upload_leads(leads, config, ["Niche", "Surrey"])

        self.assertEqual((stats["created"], stats["updated"], stats["failed"]), (1, 1, 0))
        self.assertEqual(stats["skipped_no_contact"], 1)
        self.assertEqual(stats["pipeline_counts"], {"Cold A": 1, "Cold B": 1})
        upserts = [c[2] for c in calls if c[1] == "/contacts/upsert"]
        self.assertEqual(upserts[0]["phone"], "+447911123456")
        self.assertEqual(upserts[0]["firstName"], "John")
        self.assertEqual(upserts[0]["locationId"], "loc")
        self.assertEqual(upserts[0]["customFields"], [{"id": "cf1", "field_value": "https://facebook.com/a"}])
        self.assertEqual(upserts[1]["name"], "B Ltd")
        opps = [c[2] for c in calls if c[1] == "/opportunities/"]
        self.assertEqual([o["pipelineId"] for o in opps], ["pA", "pB"])
        tags = [c[2] for c in calls if c[1].endswith("/tags")]
        self.assertEqual(tags[0], {"tags": ["Niche", "Surrey"]})


if __name__ == "__main__":
    unittest.main()
