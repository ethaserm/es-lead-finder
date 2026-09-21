import os
import re
import sys
import time
import unittest
from datetime import date
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

    def test_clean_secret_strips_bom_and_whitespace(self):
        self.assertEqual(lf.clean_secret("\ufeff \u200b abc\r\n"), "abc")
        self.assertEqual(lf.clean_secret(None), "")

    def test_chain_exclusion(self):
        keys = lf.chain_keys({"excluded_chains": ["Travis Perkins", "B&Q", "Screwfix"]})
        self.assertTrue(lf.is_chain(lf.Lead("Travis Perkins Plc", "x", "y"), keys))
        self.assertTrue(lf.is_chain(lf.Lead("Local Shop", "x", "y", "https://www.screwfix.com/x"), keys))
        self.assertFalse(lf.is_chain(lf.Lead("Foo Plumbing", "x", "y", "http://foo.co.uk"), keys))
        keys2 = lf.chain_keys({"excluded_chains": ["Co-op", "Costa"]})
        self.assertTrue(lf.is_chain(lf.Lead("Co-op Food", "x", "y"), keys2))
        self.assertFalse(lf.is_chain(lf.Lead("Cooper Electrical", "x", "y"), keys2))
        self.assertFalse(lf.is_chain(lf.Lead("Costanza Plumbing", "x", "y"), keys2))

    def test_overpass_retries_and_rotates_mirrors(self):
        calls = []

        class S:
            def post(_, url, **k):
                calls.append(url)
                r = FakeResp()
                if len(calls) < 3:
                    r.status_code = 429
                else:
                    r.status_code = 200
                    r.json = lambda: {"elements": []}
                return r
        with mock.patch("time.sleep") as sl:
            data = lf.overpass_query("q", S())
        self.assertEqual(data, {"elements": []})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 3)  # each attempt hit a different mirror
        self.assertEqual([c.args[0] for c in sl.call_args_list], [2, 4])  # exponential backoff

    def test_overpass_gives_up_after_first_try_plus_two_retries(self):
        calls = []

        class S:
            def post(_, url, **k):
                calls.append(k["timeout"])
                raise lf.requests.ConnectionError("x")
        with mock.patch("time.sleep"):
            self.assertIsNone(lf.overpass_query("q", S()))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], (5, 25))  # each attempt capped at ~25s read

    def test_overpass_skips_when_time_budget_used_up(self):
        class S:
            def post(_, *a, **k):
                raise AssertionError("must not be called")
        with mock.patch.object(lf, "_overpass_deadline", 0):
            self.assertIsNone(lf.overpass_query("q", S()))

    def test_failed_search_returns_none_not_empty(self):
        trade = {"label": "Plumber", "osm": [["craft", "plumber"]]}
        with mock.patch.object(lf, "overpass_query", return_value=None), mock.patch("time.sleep"):
            self.assertIsNone(lf.overpass_leads(trade, "York", None))
        with mock.patch.object(lf, "overpass_query", return_value={"elements": []}), mock.patch("time.sleep"):
            self.assertEqual(lf.overpass_leads(trade, "York", None), [])

    def test_query_is_uk_only_with_place_fallback(self):
        seen = []
        trade = {"label": "Plumber", "osm": [["craft", "plumber"], ["shop", "x"]]}
        with mock.patch.object(lf, "overpass_query", side_effect=lambda q, s: seen.append(q)), \
                mock.patch("time.sleep"):
            lf.overpass_leads(trade, 'Bo"ston', None)
        q = seen[0]
        self.assertIn("49.8,-8.7,60.9,1.8", q)  # restricted to the UK bounding box
        self.assertIn("map_to_area->.a", q)
        self.assertIn('"place"~"^(city|town)$"', q)
        self.assertIn("(around.p:3000)", q)
        self.assertIn('nwr["craft"="plumber"](area.a)', q)
        self.assertNotIn('Bo"ston', q)  # quotes in names cannot break out of the query

    def test_no_osm_tags_means_no_request(self):
        with mock.patch.object(lf, "overpass_query", side_effect=AssertionError):
            self.assertEqual(lf.overpass_leads({"label": "Cleaner", "osm": []}, "York", None), [])

    def test_search_failing_on_its_retry_is_not_requeued(self):
        state = {"retry": [["Plumber", "York"]]}
        failed = [["Plumber", "York"], ["Garage", "Leeds"]]
        self.assertEqual(lf.next_retry_queue(failed, state), [["Garage", "Leeds"]])

    def test_config_covers_the_uk(self):
        cfg = lf.load_config()
        names = [t["name"] if isinstance(t, dict) else t for t in cfg["towns"]]
        self.assertGreaterEqual(len(names), 300)
        self.assertEqual(len(names), len(set(names)))
        for must in ("York", "City of London", "Manchester", "Edinburgh",
                     "Glasgow", "Cardiff", "Swansea", "Belfast", "Aberdeen", "Inverness", "Plymouth"):
            self.assertIn(must, names)
        self.assertEqual(len(cfg["trades"]), 14)
        for trade in cfg["trades"]:
            self.assertTrue({"label", "osm", "places"} <= set(trade))
        # a full cycle is finite and the rotation walks every combination exactly once
        total = len(cfg["towns"]) * len(cfg["trades"])
        seen, cursor = set(), 0
        while len(seen) < total:
            picks, cursor = lf.pick_combos(cfg, cursor, 14)
            seen |= {tuple(lf.combo_label(*p)) for p in picks}
            if cursor == 0:
                break
        self.assertEqual(len(seen), total)

    def _workflow_text(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "leadfinder.yml")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_workflow_schedule_and_limits(self):
        text = self._workflow_text()
        cron = re.search(r'cron:\s*"(\S+) (\S+) (\S+) (\S+) (\S+)"', text).groups()
        self.assertEqual(cron[1:], ("*", "*", "*", "*"))  # every hour, every day of the week
        self.assertRegex(cron[0], r"^\d+$")  # one fixed minute, not top of the hour
        self.assertNotEqual(cron[0], "0")
        env = {k: int(v) for k, v in re.findall(r'^\s+([A-Z_]+):\s*"(\d+)"', text, re.M)}
        self.assertEqual(env["COMBOS_PER_RUN"], 30)
        self.assertGreaterEqual(env["MAX_NEW_PER_RUN"], 500)
        self.assertGreaterEqual(env["MAX_SITES_PER_RUN"], 400)
        timeout = int(re.search(r"timeout-minutes:\s*(\d+)", text).group(1)) * 60
        # Overpass phase < whole-run budget; budget + Companies House grace + setup fits the job timeout
        self.assertLess(env["OVERPASS_BUDGET_SECONDS"], env["RUN_BUDGET_SECONDS"])
        self.assertLess(env["RUN_BUDGET_SECONDS"] + lf.CH_GRACE_SECONDS + 120, timeout)
        self.assertLess(timeout, 3600)  # a run must finish before the next hourly one starts

    def test_artifact_only_uploaded_while_repo_is_private(self):
        text = self._workflow_text()
        self.assertEqual(text.count("github.event.repository.private == true"), 2)

    def test_repo_files_hold_no_secrets_or_sheet_ids(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        bad = [r"private_key", r"BEGIN (RSA |EC )?PRIVATE", r"1[A-Za-z0-9_-]{43}",
               r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r"iam\.gserviceaccount"]
        for rel in ("README.md", "leadfinder.py", "config.json", ".gitignore",
                    ".github/workflows/leadfinder.yml"):  # this test file holds the patterns themselves
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
            for pat in bad:
                self.assertIsNone(re.search(pat, text), f"{rel} matches {pat}")

    def test_failed_searches_are_retried_first_next_run(self):
        cfg = {"trades": [{"label": "a"}, {"label": "b"}, {"label": "c"}], "towns": ["x", "y"]}
        state = {"cursor": 0, "retry": [["c", "y"], ["zzz", "gone"]]}
        picks, nxt = lf.pick_with_retries(cfg, state, 4)
        self.assertEqual([lf.combo_label(*p) for p in picks],
                         [["c", "y"], ["a", "x"], ["b", "x"], ["c", "x"]])
        self.assertEqual(nxt, 3)  # rotation advanced only by the slots it used
        picks, nxt = lf.pick_with_retries(cfg, {"cursor": 5}, 2)
        self.assertEqual(len(picks), 2)

    def test_ch_self_test_statuses(self):
        def sess(code, text=""):
            class S:
                def get(_, *a, **k):
                    r = FakeResp()
                    r.status_code = code
                    r.text = text
                    return r
            return S()
        self.assertTrue(lf.companies_house_self_test(sess(200), "k"))
        self.assertFalse(lf.companies_house_self_test(sess(401), "k"))
        self.assertFalse(lf.companies_house_self_test(sess(400), "k"))
        with mock.patch.object(lf, "log") as lg:
            lf.companies_house_self_test(sess(400, "bad SECRETKEY here"), "SECRETKEY")
        self.assertNotIn("SECRETKEY", lg.call_args.args[0])
        self.assertIn("bad *** here", lg.call_args.args[0])
        self.assertIsNone(lf.companies_house_self_test(sess(500), "k"))

    def test_key_shape_hides_key_and_flags_problems(self):
        good = lf.key_shape("abcd-1234")
        self.assertIn("length=9", good)
        self.assertNotIn("abcd", good)
        for bad, flag in (('"k1"', "quotes=True"), ("k 1", "whitespace=True"),
                          ("KEY=k1", "equals=True"), ("ké", "non_ascii=True"),
                          ("k\x001", "control=True")):
            self.assertIn(flag, lf.key_shape(bad))
        self.assertNotIn("quotes=True", good)

    # ------------------------------------------------------------------ emails
    def test_scan_page_reports_how_each_address_appeared(self):
        cf_key = 0x42
        cf_plain = "cf@foo.co.uk"
        cf_hex = "%02x" % cf_key + "".join("%02x" % (ord(c) ^ cf_key) for c in cf_plain)
        page = ('<a href="mailto:Info@Foo.co.uk?subject=hi">mail</a> <p>Call or write: sales@foo.co.uk</p>'
                '<p>bob [at] foo [dot] co.uk</p>'
                f'<a data-cfemail="{cf_hex}">[email protected]</a>'
                '<script>var e = "hidden@foo.co.uk";</script><!-- old@foo.co.uk -->')
        got = dict(lf.scan_page(page)[0])
        self.assertEqual(got["info@foo.co.uk"], "mailto")
        self.assertEqual(got["sales@foo.co.uk"], "text")
        self.assertEqual(got["bob@foo.co.uk"], "obfuscated")
        self.assertEqual(got[cf_plain], "cloudflare")
        self.assertNotIn("hidden@foo.co.uk", got)  # scripts and comments are not page content
        self.assertNotIn("old@foo.co.uk", got)

    def test_junk_addresses_are_dropped_and_counted(self):
        page = ("noreply@foo.co.uk no-reply@foo.co.uk do-not-reply@foo.co.uk abc@sentry.io x@o1.ingest.sentry.io "
                "a1b2@wixpress.com someone@example.com logo@2x.png banner@site.jpg real@foo.co.uk")
        found, junk = lf.scan_page(page)
        self.assertEqual([e for e, _ in found], ["real@foo.co.uk"])
        self.assertGreaterEqual(junk, 8)

    def test_domain_helpers(self):
        self.assertEqual(lf.registrable_domain("www.shop.foo.co.uk"), "foo.co.uk")
        self.assertEqual(lf.registrable_domain("mail.foo.com"), "foo.com")
        self.assertTrue(lf.email_matches_site("info@foo.co.uk", "www.foo.co.uk"))
        self.assertTrue(lf.email_matches_site("a@mail.foo.co.uk", "foo.co.uk"))
        self.assertFalse(lf.email_matches_site("hello@agency.co.uk", "foo.co.uk"))
        self.assertFalse(lf.email_matches_site("a@foo.com", "foo.co.uk"))

    def test_rank_emails_prefers_own_domain_then_freemail(self):
        cands = [("z@agency.com", "s"), ("me@gmail.com", "s"), ("bob@foo.co.uk", "s"), ("info@foo.co.uk", "s")]
        ranked = [e for e, _ in lf.rank_emails(cands, "foo.co.uk", set(lf.DEFAULT_FREEMAIL))]
        self.assertEqual(ranked, ["info@foo.co.uk", "bob@foo.co.uk", "me@gmail.com", "z@agency.com"])

    def test_mx_status(self):
        import dns.exception
        import dns.resolver

        class Ans:
            def __init__(self, host):
                self.exchange = mock.Mock(to_text=lambda: host)
        lf._mx_cache.clear()
        with mock.patch.object(dns.resolver.Resolver, "resolve", return_value=[Ans("mx.foo.co.uk.")]):
            self.assertEqual(lf.mx_status("foo.co.uk"), "ok")
        with mock.patch.object(dns.resolver.Resolver, "resolve", return_value=[Ans(".")]):
            self.assertEqual(lf.mx_status("nullmx.co.uk"), "none")  # a null MX means "does not accept mail"
        with mock.patch.object(dns.resolver.Resolver, "resolve", side_effect=dns.resolver.NXDOMAIN()):
            self.assertEqual(lf.mx_status("gone.co.uk"), "none")
        with mock.patch.object(dns.resolver.Resolver, "resolve", side_effect=dns.resolver.NoAnswer()):
            self.assertEqual(lf.mx_status("nomx.co.uk"), "none")
        with mock.patch.object(dns.resolver.Resolver, "resolve", side_effect=dns.exception.Timeout()):
            self.assertEqual(lf.mx_status("slow.co.uk"), "error")
        lf._mx_cache.clear()

    def _crawler(self, pages):
        return lf.SiteCrawler(FakeSession(pages))

    def test_collect_follows_a_linked_contact_page(self):
        pages = {
            "http://foo.co.uk/": FakeResp('<a href="/contact-us">Contact</a> made by web@agency.com'),
            "http://foo.co.uk/contact-us": FakeResp('Email us: <a href="mailto:hello@foo.co.uk">here</a>'),
        }
        with mock.patch("time.sleep"):
            found, junk, fetched = self._crawler(pages).collect(
                "http://foo.co.uk/", lambda e: lf.email_matches_site(e, "foo.co.uk"))
        self.assertTrue(fetched)
        self.assertIn(("hello@foo.co.uk", "Website contact page /contact-us (mailto link)"), found)
        self.assertIn(("web@agency.com", "Website homepage (visible text)"), found)

    def test_collect_never_fetches_pages_that_are_not_linked(self):
        asked = []

        class S(FakeSession):
            def get(self, url, **kw):
                asked.append(url)
                return super().get(url, **kw)
        crawler = lf.SiteCrawler(S({"http://foo.co.uk/": FakeResp("no address here")}))
        with mock.patch("time.sleep"):
            found, _, fetched = crawler.collect("http://foo.co.uk/", lambda e: True)
        self.assertEqual((found, fetched), ([], True))
        self.assertFalse(any("contact" in u for u in asked))  # no guessed /contact or /contact-us

    def test_collect_reports_an_unreachable_site(self):
        with mock.patch("time.sleep"):
            self.assertEqual(self._crawler({}).collect("http://down.co.uk/", lambda e: True), ([], 0, False))

    def _resolve(self, page, website="http://foo.co.uk/", rules=None, mx="ok", osm=None):
        crawler = self._crawler({website: FakeResp(page)} if page is not None else {})
        lead = lf.Lead("Foo Plumbing", "Plumber", "York", website)
        lead.osm_emails = osm or []
        rules = rules or lf.EmailRules()
        with mock.patch("time.sleep"), mock.patch.object(lf, "mx_status", return_value=mx):
            return lf.resolve_email(crawler, lead, rules)

    def test_resolve_accepts_own_domain_mailto_and_names_the_source(self):
        email, source, reason = self._resolve('<a href="mailto:info@foo.co.uk">x</a>')
        self.assertEqual((email, reason), ("info@foo.co.uk", ""))
        self.assertEqual(source, "Website homepage (mailto link)")

    def test_resolve_accepts_freemail_only_when_written_on_the_page(self):
        email, _, reason = self._resolve("Write to foo.plumbing@gmail.com")
        self.assertEqual((email, reason), ("foo.plumbing@gmail.com", ""))
        self.assertEqual(self._resolve("Write to foo.plumbing@aol.com")[2], "third_party")  # not in the freemail list

    def test_resolve_rejects_third_party_addresses(self):
        self.assertEqual(self._resolve("Site by hello@webagency.co.uk")[::2], ("", "third_party"))

    def test_resolve_rejects_domains_without_mx(self):
        self.assertEqual(self._resolve("info@foo.co.uk", mx="none")[::2], ("", "no_mx"))
        self.assertEqual(self._resolve("info@foo.co.uk", mx="error")[::2], ("", "mx_lookup_failed"))

    def test_resolve_skips_freemail_mx_lookup(self):
        with mock.patch.object(lf, "mx_status", side_effect=AssertionError("no lookup for gmail")):
            crawler = self._crawler({"http://foo.co.uk/": FakeResp("a.b@gmail.com")})
            with mock.patch("time.sleep"):
                self.assertEqual(lf.resolve_email(crawler, lf.Lead("F", "x", "y", "http://foo.co.uk/"),
                                                  lf.EmailRules())[0], "a.b@gmail.com")

    def test_resolve_never_guesses_an_address(self):
        email, source, reason = self._resolve("Ring us on 0113 496 0000")
        self.assertEqual((email, source, reason), ("", "", "no_email_found"))
        self.assertEqual(self._resolve(None)[2], "site_unavailable")
        self.assertEqual(self._resolve("noreply@foo.co.uk logo@2x.png")[2], "junk_only")

    def test_resolve_uses_openstreetmap_tag_and_labels_it(self):
        email, source, reason = self._resolve("no address on the page", osm=["hello@foo.co.uk"])
        self.assertEqual((email, source, reason), ("hello@foo.co.uk", "OpenStreetMap tag", ""))
        # an OSM address is held to the same domain rule as everything else
        self.assertEqual(self._resolve("nothing", osm=["x@webagency.co.uk"])[2], "third_party")

    def test_resolve_suppression_and_duplicates(self):
        rules = lf.EmailRules(suppressed_emails={"info@foo.co.uk"})
        self.assertEqual(self._resolve("info@foo.co.uk", rules=rules)[2], "suppressed")
        rules = lf.EmailRules(suppressed_domains={"foo.co.uk"})
        self.assertEqual(self._resolve("sales@foo.co.uk", rules=rules)[2], "suppressed")  # whole domain
        rules = lf.EmailRules(known_emails={"info@foo.co.uk"})
        self.assertEqual(self._resolve("info@foo.co.uk", rules=rules)[2], "duplicate")
        # bouncing one gmail address must not suppress every gmail address
        rules = lf.EmailRules(suppressed_emails={"bad@gmail.com"}, suppressed_domains={"gmail.com"})
        self.assertEqual(self._resolve("good@gmail.com", rules=rules)[::2], ("good@gmail.com", ""))
        self.assertEqual(self._resolve("bad@gmail.com", rules=rules)[2], "suppressed")

    def test_resolve_falls_back_to_a_second_address_that_passes(self):
        page = "sales@foo.co.uk and info@foo.co.uk"
        rules = lf.EmailRules(suppressed_emails={"info@foo.co.uk"})
        self.assertEqual(self._resolve(page, rules=rules)[::2], ("sales@foo.co.uk", ""))

    def test_load_suppression_reads_every_cell_of_both_tabs(self):
        tabs = {
            "Outreach Tracker": [["Business Name", "Email", "Status"], ["A", "Owner <a@foo.co.uk>", "Sent"]],
            "Replies": [["When", "From", "Note"], ["today", "b@bar.co.uk", "unsubscribe; also c@gmail.com"]],
        }
        import gspread

        class SH:
            def worksheet(_, name):
                if name not in tabs:
                    raise gspread.WorksheetNotFound(name)
                return FakeWS(tabs[name])
        with mock.patch.object(lf, "log") as lg:
            emails, domains = lf.load_suppression(SH(), ["Outreach Tracker", "Replies", "Missing"])
        self.assertEqual(emails, {"a@foo.co.uk", "b@bar.co.uk", "c@gmail.com"})
        self.assertEqual(domains, {"foo.co.uk", "bar.co.uk", "gmail.com"})
        self.assertTrue(any("'Missing' not found" in c.args[0] for c in lg.call_args_list))

    # ------------------------------------------------------------------ companies
    def _cfg(self):
        return lf.load_config()

    def _info(self, **kw):
        info = {"number": "01234567", "title": "FOO PLUMBING LTD", "status": "active", "type": "ltd",
                "created": "2019-06-01", "accounts_type": "micro-entity", "sic": ["43220"], "profile": True}
        info.update(kw)
        return info

    def _assess(self, **kw):
        lead = lf.Lead(kw.pop("name", "Foo Plumbing"), kw.pop("trade", "Plumber"), "York")
        return lf.assess_company(self._info(**kw), lead, self._cfg(), today=date(2026, 9, 21))

    def test_a_small_established_matching_ltd_passes(self):
        reasons, facts = self._assess()
        self.assertEqual(reasons, [])
        self.assertEqual(facts, "micro-entity accounts, active 7 yrs, SIC 43220 fits Plumber")
        for good in ("small", "total-exemption-small", "total-exemption-full"):
            self.assertEqual(self._assess(accounts_type=good)[0], [])

    def test_company_rules_send_leads_to_review(self):
        cases = [
            (dict(accounts_type="full"), "accounts: full"),
            (dict(accounts_type="group"), "accounts: group"),
            (dict(accounts_type=None), "accounts: none filed"),
            (dict(accounts_type="medium"), "accounts: medium"),
            (dict(created="2025-06-01"), "incorporated under 2 yrs ago"),
            (dict(created=None), "no incorporation date"),
            (dict(sic=["56101"]), "SIC does not fit trade (56101)"),
            (dict(sic=[]), "SIC does not fit trade"),
            (dict(status="dissolved"), "status dissolved"),
            (dict(type="plc"), "not a private Ltd (plc)"),
            (dict(type="private-limited-guarant-nsc"), "not a private Ltd (private-limited-guarant-nsc)"),
            (dict(profile=False), "company profile unavailable"),
            (dict(title="FOO HOLDINGS LTD"), "name contains 'holdings'"),
            (dict(title="FOO GROUP LTD"), "name contains 'group'"),
            (dict(title="FOO PLC"), "name contains 'plc'"),
            (dict(name="Foo Bank Plumbing"), "name contains 'bank'"),
            (dict(title="CITY COUNCIL SERVICES LTD"), "name contains 'council'"),
        ]
        for kw, expected in cases:
            reasons, _ = self._assess(**kw)
            self.assertIn(expected, reasons, kw)

    def test_no_companies_house_match_is_review(self):
        lead = lf.Lead("Foo", "Plumber", "York")
        self.assertEqual(lf.assess_company(None, lead, self._cfg())[0], ["not confirmed Ltd"])

    def test_company_age_boundary(self):
        self.assertEqual(self._assess(created="2024-09-21")[0], [])  # exactly two years
        self.assertTrue(self._assess(created="2024-09-22")[0])

    def test_every_trade_has_sic_prefixes(self):
        for trade in self._cfg()["trades"]:
            self.assertTrue(trade.get("sic"), trade["label"])

    def test_companies_house_lookup_fetches_the_profile(self):
        calls = []

        class S:
            def get(_, url, **k):
                calls.append((url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], k.get("auth")))
                r = FakeResp()
                r.status_code = 200
                if url.endswith("/search/companies"):
                    r.json = lambda: {"items": [
                        {"title": "FOO PLUMBING LTD", "company_status": "dissolved", "company_number": "9"},
                        {"title": "FOO PLUMBING LTD", "company_status": "active", "company_number": "123",
                         "company_type": "ltd", "date_of_creation": "2019-06-01", "address_snippet": "York"}]}
                else:
                    r.json = lambda: {"company_status": "active", "type": "ltd", "date_of_creation": "2019-06-01",
                                      "sic_codes": ["43220"], "accounts": {"last_accounts": {"type": "small"}}}
                return r
        with mock.patch("time.sleep"):
            info = lf.companies_house_lookup(lf.Lead("Foo Plumbing", "Plumber", "York"), S(), "thekey")
        self.assertEqual((info["number"], info["accounts_type"], info["sic"], info["profile"]),
                         ("123", "small", ["43220"], True))
        self.assertEqual([c[1] for c in calls], [("thekey", "")] * 2)  # Basic auth: key as username, empty password
        self.assertEqual(lf.requests.Request("GET", "https://x", auth=("thekey", "")).prepare()
                         .headers["Authorization"], "Basic dGhla2V5Og==")

    def test_lookup_without_a_match_returns_none(self):
        class S:
            def get(_, *a, **k):
                r = FakeResp()
                r.status_code = 200
                r.json = lambda: {"items": [{"title": "OTHER LTD", "company_status": "active", "company_number": "1"}]}
                return r
        with mock.patch("time.sleep"):
            self.assertIsNone(lf.companies_house_lookup(lf.Lead("Foo Plumbing", "Plumber", "York"), S(), "k"))

    def test_only_a_clean_company_is_pending(self):
        lead = lf.Lead("A", "Plumber", "York")
        lead.company = "Ltd (active) 01234567"
        self.assertEqual(lf.lead_status(lead, "key"), "Pending")
        lead.review_reasons = ["accounts: full"]
        self.assertEqual(lf.lead_status(lead, "key"), "Review - accounts: full")
        other = lf.Lead("B", "Plumber", "York")
        self.assertEqual(lf.lead_status(other, "key"), "Review - not confirmed Ltd")
        self.assertEqual(lf.lead_status(lead, ""), "Review - no Companies House check")

    def test_row_carries_email_source_and_why(self):
        ws = FakeWS([["Business Name", "Status"], ["Foo", "Pending"]])
        _, header = lf.prepare_queue_tab(ws)
        self.assertIn("Email Source", header)
        self.assertIn("Why", header)
        lead = lf.Lead("Foo Plumbing", "Plumber", "York", "http://foo.co.uk")
        lead.email, lead.email_source, lead.why = "info@foo.co.uk", "Website homepage (mailto link)", "small accounts"
        row = lf.row_for(lead, header, "Pending")
        self.assertEqual(row[header.index("Email Source")], "Website homepage (mailto link)")
        self.assertEqual(row[header.index("Why")], "small accounts")

    def test_osm_email_tags_are_read_and_merged(self):
        trade = {"label": "Plumber", "osm": [["craft", "plumber"]]}
        data = {"elements": [{"type": "node", "lat": 53.9, "lon": -1.1, "tags": {
            "name": "Foo", "website": "http://foo.co.uk", "contact:email": "Hello@Foo.co.uk; not-an-email"}}]}
        with mock.patch.object(lf, "overpass_query", return_value=data), mock.patch("time.sleep"):
            leads = lf.overpass_leads(trade, "York", None)
        self.assertEqual(leads[0].osm_emails, ["hello@foo.co.uk"])
        other = lf.Lead("Foo", "Plumber", "York")
        other.osm_emails = ["x@foo.co.uk"]
        leads[0].merge(other)
        self.assertEqual(leads[0].osm_emails, ["hello@foo.co.uk", "x@foo.co.uk"])

    def test_crawl_emails_runs_sites_in_parallel_and_survives_errors(self):
        import threading
        leads = [lf.Lead(f"B{i}", "x", "y", f"http://site{i}.co.uk") for i in range(6)]
        started, gate = set(), threading.Barrier(3, timeout=5)

        def work(lead):
            if "site0" in lead.website:
                raise ValueError("boom")
            started.add(threading.get_ident())
            if any(s in lead.website for s in ("site1", "site2", "site3")):
                gate.wait()  # only passes if three sites are being fetched at the same time
            return ("info@" + lead.website.split("//")[1].rstrip("/"), "src", "")
        with mock.patch.object(lf, "log"):
            out = lf.crawl_emails(work, leads, 4, time.monotonic() + 30)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[id(leads[0])], ("", "", "error"))  # the error became "no email"
        self.assertEqual(out[id(leads[5])][0], "info@site5.co.uk")
        self.assertGreaterEqual(len(started), 3)

    def test_crawl_emails_stops_at_the_deadline(self):
        leads = [lf.Lead("B", "x", "y", "http://slow.co.uk")]

        def work(lead):
            time.sleep(1.5)
            return ("a@slow.co.uk", "src", "")
        with mock.patch.object(lf, "log") as lg:
            out = lf.crawl_emails(work, leads, 1, time.monotonic() + 0.2)
        self.assertEqual(out, {})
        self.assertTrue(any("budget used up" in c.args[0] for c in lg.call_args_list))

    def _run_end_to_end(self, info):
        leads = [
            lf.Lead("Foo Plumbing Ltd", "Plumber", "York", "http://foo.co.uk", source="OpenStreetMap"),
            lf.Lead("Foo Plumbing Ltd", "Plumber", "York", "http://foo.co.uk", source="Google Maps"),
            lf.Lead("No Site Sparks", "Electrician", "York", "", source="OpenStreetMap"),
            lf.Lead("Social Only", "Cafe", "York", "https://facebook.com/x", source="OpenStreetMap"),
        ]
        found = ([("info@foo.co.uk", "Website homepage (mailto link)")], 0, True)
        with mock.patch.dict(os.environ, {"COMPANIES_HOUSE_API_KEY": "k"}, clear=False), \
                mock.patch.object(lf, "overpass_leads", return_value=leads), \
                mock.patch.object(lf.SiteCrawler, "collect", return_value=found), \
                mock.patch.object(lf, "mx_status", return_value="ok"), \
                mock.patch.object(lf, "companies_house_lookup", return_value=info), \
                mock.patch.object(lf, "companies_house_self_test", return_value=True), \
                mock.patch.object(lf, "load_state", return_value={"cursor": 0}), \
                mock.patch.object(lf, "log") as lg:
            os.environ.pop("SHEET_ID", None)
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
            n = lf.run(True, 1, 10)
        return n, "\n".join(str(c.args[0]) for c in lg.call_args_list)

    def test_end_to_end_dry_run_pending(self):
        n, logged = self._run_end_to_end(self._info())
        self.assertEqual(n, 1)  # merged duplicate, dropped no-site + social-only
        self.assertIn("[foo.co.uk] Pending", logged)
        self.assertNotIn("info@foo.co.uk", logged)  # lead emails never reach the (possibly public) log
        self.assertIn("skipped: no own website: 2", logged)
        self.assertIn("accepted: Pending: 1", logged)

    def test_end_to_end_dry_run_full_accounts_goes_to_review(self):
        n, logged = self._run_end_to_end(self._info(accounts_type="full"))
        self.assertEqual(n, 1)
        self.assertIn("[foo.co.uk] Review - accounts: full", logged)
        self.assertNotIn("] Pending", logged)


if __name__ == "__main__":
    unittest.main()
