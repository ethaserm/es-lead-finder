import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leadfinder as lf  # noqa: E402


class FakeResp:
    def __init__(self, text="", status=200, ctype="text/html"):
        self.text, self.status_code = text, status
        self.headers = {"Content-Type": ctype}
        self.encoding = "utf-8"

        class Raw:
            def read(_, n, decode_content=True):
                return text.encode()
        self.raw = Raw()


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kw):
        if url.endswith("/robots.txt"):
            return FakeResp("", 404)
        return self.pages.get(url, FakeResp("", 404))


class FakeWS:
    def __init__(self, values):
        self.values = values
        self.updates = []

    def get_all_values(self):
        return self.values

    def update_cell(self, r, c, v):
        self.updates.append((r, c, v))

    def update(self, **kw):
        self.updates.append(kw)


class Tests(unittest.TestCase):
    def test_norm_name(self):
        self.assertEqual(lf.norm_name("S&O Conversions Ltd"), lf.norm_name("S and O Conversions Limited"))
        self.assertEqual(lf.norm_name("The Blue Fox Plumbing"), "bluefoxplumbing")

    def test_clean_site_blocks_social(self):
        self.assertEqual(lf.clean_site("https://www.facebook.com/foo"), "")
        self.assertEqual(lf.clean_site("foo-plumbing.co.uk"), "http://foo-plumbing.co.uk/")

    def test_extract_emails(self):
        page = ('<a href="mailto:Info@Foo-Plumbing.co.uk">x</a> logo@2x.png sentry@abc.sentry.io '
                'noreply@foo.com bob [at] gmail [dot] com')
        got = lf.extract_emails(page)
        self.assertIn("info@foo-plumbing.co.uk", got)
        self.assertIn("bob@gmail.com", got)
        self.assertFalse(any("png" in e or "sentry" in e or "noreply" in e for e in got))

    def test_cloudflare_decode(self):
        key = 0x42
        plain = "hi@foo.co.uk"
        enc = "%02x" % key + "".join("%02x" % (ord(c) ^ key) for c in plain)
        self.assertEqual(lf.decode_cf_email(enc), plain)
        self.assertIn(plain, lf.extract_emails(f'<a data-cfemail="{enc}">[email protected]</a>'))

    def test_pick_email_prefers_own_domain(self):
        emails = ["someone@gmail.com", "office@foo.co.uk"]
        self.assertEqual(lf.pick_email(emails, "foo.co.uk"), "office@foo.co.uk")
        self.assertEqual(lf.pick_email(["a@gmail.com"], "foo.co.uk"), "a@gmail.com")
        self.assertEqual(lf.pick_email([], "foo.co.uk"), "")

    def test_find_email_follows_contact_page(self):
        pages = {
            "http://foo.co.uk/": FakeResp('<a href="/contact-us">Contact</a>'),
            "http://foo.co.uk/contact-us": FakeResp("Email us: hello@foo.co.uk"),
        }
        crawler = lf.SiteCrawler(FakeSession(pages))
        with mock.patch("time.sleep"):
            self.assertEqual(crawler.find_email("http://foo.co.uk/"), "hello@foo.co.uk")

    def test_find_email_none_when_absent(self):
        crawler = lf.SiteCrawler(FakeSession({"http://bar.co.uk/": FakeResp("phone 0113 000")}))
        with mock.patch("time.sleep"):
            self.assertEqual(crawler.find_email("http://bar.co.uk/"), "")

    def test_load_known_with_title_rows(self):
        values = [
            ["ES Agents - Outreach Tracker"] * 4,
            ["Cold email pipeline"] * 4,
            ["Business Name", "Trade", "Area", "Contact Email"],
            ["Bluebird Bakery", "Bakery", "York", "hello@bluebirdbakery.co.uk"],
        ]

        class SH:
            def worksheet(_, name):
                return FakeWS(values)
        names, emails, _ = lf.load_known(SH(), ["Outreach Tracker"])
        self.assertIn(lf.norm_name("Bluebird Bakery"), names)
        self.assertIn("hello@bluebirdbakery.co.uk", emails)

    def test_prepare_queue_adds_columns_and_row_maps(self):
        ws = FakeWS([["Business Name", "Status"], ["Foo", "Pending"]])
        _, header = lf.prepare_queue_tab(ws)
        self.assertIn("Contact Email", header)
        lead = lf.Lead("Foo Plumbing", "Plumber", "York", "http://foo.co.uk", source="OpenStreetMap")
        lead.email = "info@foo.co.uk"
        row = lf.row_for(lead, header, "Pending")
        self.assertEqual(row[0], "Foo Plumbing")
        self.assertEqual(row[header.index("Contact Email")], "info@foo.co.uk")
        self.assertEqual(row[header.index("Status")], "Pending")

    def test_sheet_safe(self):
        self.assertEqual(lf.sheet_safe("=cmd"), "'=cmd")

    def test_uk_bbox(self):
        self.assertTrue(lf.in_uk(53.96, -1.08))
        self.assertFalse(lf.in_uk(43.6, -79.4))  # York, Ontario

    def test_rotation_wraps(self):
        cfg = {"trades": [{"label": "a"}, {"label": "b"}], "towns": ["x", "y"]}
        picks, nxt = lf.pick_combos(cfg, 3, 3)
        self.assertEqual(len(picks), 3)
        self.assertEqual(nxt, (3 + 3) % 4)

    def test_companies_house_match(self):
        class S:
            def get(_, *a, **k):
                r = FakeResp()
                r.status_code = 200
                r.json = lambda: {"items": [
                    {"title": "FOO PLUMBING LTD", "company_status": "active", "company_number": "123", "address_snippet": "York"},
                    {"title": "FOO PLUMBING LTD", "company_status": "dissolved", "company_number": "9"},
                ]}
                return r
        lead = lf.Lead("Foo Plumbing", "Plumber", "York")
        with mock.patch("time.sleep"):
            self.assertEqual(lf.companies_house_check(lead, S(), "k"), "Ltd (active) 123")

    def test_end_to_end_dry_run(self):
        leads = [
            lf.Lead("Foo Plumbing Ltd", "Plumber", "York", "http://foo.co.uk", source="OpenStreetMap"),
            lf.Lead("Foo Plumbing Ltd", "Plumber", "York", "http://foo.co.uk", source="Google Maps"),
            lf.Lead("No Site Sparks", "Electrician", "York", "", source="OpenStreetMap"),
            lf.Lead("Social Only", "Cafe", "York", "https://facebook.com/x", source="OpenStreetMap"),
        ]
        env = {"COMPANIES_HOUSE_API_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(lf, "overpass_leads", return_value=leads), \
                mock.patch.object(lf.SiteCrawler, "find_email", return_value="info@foo.co.uk"), \
                mock.patch.object(lf, "companies_house_check", return_value="Ltd (active) 123"), \
                mock.patch.object(lf, "load_state", return_value={"cursor": 0}):
            os.environ.pop("SHEET_ID", None)
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
            n = lf.run(True, 1, 10)
        self.assertEqual(n, 1)  # merged duplicate, dropped no-site + social-only


if __name__ == "__main__":
    unittest.main()
