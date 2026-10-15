import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_osm_extract as bx  # noqa: E402

CONFIG = {"trades": [
    {"label": "Electrician", "osm": [["craft", "electrician"]]},
    {"label": "Plumber", "osm": [["craft", "plumber"]]},
    {"label": "Hair", "osm": [["shop", "hairdresser"]]},
    {"label": "Pest control", "osm": []},
    {"label": "Garage", "osm": [["shop", "car_repair"], ["craft", "plumber"]]},
]}


def point(ident, lon, lat, **props):
    return {"type": "Feature", "id": ident, "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def square(ident, **props):
    ring = [[-2.0, 53.0], [-2.0, 53.2], [-1.8, 53.2], [-1.8, 53.0], [-2.0, 53.0]]
    return {"type": "Feature", "id": ident, "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


class BuildTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def _seq(self, name, features, junk=()):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for f in features:
                fh.write("\x1e" + json.dumps(f) + "\n")  # GeoJSON text sequences start each record with RS
            for j in junk:
                fh.write(j + "\n")
        return path

    def _read(self, out, name):
        with gzip.open(os.path.join(out, name), "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_filters_cover_every_trade_tag_grouped_by_key(self):
        self.assertEqual(bx.osmium_filters(CONFIG), ["nwr/craft=electrician,plumber", "nwr/shop=car_repair,hairdresser"])
        self.assertEqual(bx.wanted_pairs(CONFIG), {("craft", "electrician"), ("craft", "plumber"),
                                                   ("shop", "hairdresser"), ("shop", "car_repair")})

    def test_real_config_filters_are_valid(self):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        filters = bx.osmium_filters(cfg)
        self.assertTrue(filters)
        for f in filters:
            self.assertRegex(f, r"^nwr/[a-z_]+=[a-z_,]+$")

    def test_print_filters_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(bx.main(["--print-filters", os.path.join(ROOT, "config.json")]), 0)
        self.assertIn("nwr/craft=", buf.getvalue())

    def test_centre_of_points_and_polygons(self):
        self.assertEqual(bx.centre({"type": "Point", "coordinates": [-2.5, 53.1]}), (53.1, -2.5))
        lat, lon = bx.centre(square("w1", name="x")["geometry"])
        self.assertAlmostEqual(lat, 53.1)
        self.assertAlmostEqual(lon, -1.9)
        multi = {"type": "MultiPolygon", "coordinates": [square("w1", name="x")["geometry"]["coordinates"]]}
        self.assertAlmostEqual(bx.centre(multi)[0], 53.1)
        for bad in (None, {}, {"type": "Point", "coordinates": []}, {"type": "Polygon", "coordinates": []},
                    {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}):
            self.assertIsNone(bx.centre(bad))

    def test_trade_record_keeps_only_the_tags_leadfinder_reads(self):
        rec = bx.trade_record(point("n1", -2.5, 53.123456789, name="Sparky", craft="electrician", website="https://s.example",
                                    **{"addr:street": "High St", "opening_hours": "Mo-Fr", "fixme": "x"}))
        self.assertEqual(rec["id"], "n1")
        self.assertEqual((rec["lat"], rec["lon"]), (53.12346, -2.5))
        self.assertEqual(rec["tags"], {"name": "Sparky", "craft": "electrician", "website": "https://s.example",
                                       "addr:street": "High St"})

    def test_trade_record_drops_nameless_and_unwanted_objects(self):
        self.assertIsNone(bx.trade_record(point("n1", -2.5, 53.1, craft="electrician")))
        self.assertIsNone(bx.trade_record({"type": "Feature", "id": "n2", "properties": {"name": "x"}, "geometry": None}))
        wanted = bx.wanted_pairs(CONFIG)
        self.assertIsNone(bx.trade_record(point("n3", -2.5, 53.1, name="Bakery", shop="bakery"), wanted))
        self.assertIsNotNone(bx.trade_record(point("n4", -2.5, 53.1, name="Salon", shop="hairdresser"), wanted))

    def test_place_record_only_keeps_named_cities_and_towns(self):
        self.assertEqual(bx.place_record(point("n1", -2.9, 53.19, name="Chester", place="city"))["place"], "city")
        self.assertIsNone(bx.place_record(point("n2", -2.9, 53.19, name="Hamlet", place="village")))
        self.assertIsNone(bx.place_record(point("n3", -2.9, 53.19, place="town")))

    def test_iter_features_skips_blank_and_damaged_lines(self):
        path = self._seq("t.geojsonseq", [point("n1", 0, 51, name="a")], junk=["", "{broken", "\x1e"])
        self.assertEqual([f["id"] for f in bx.iter_features(path)], ["n1"])

    def test_build_writes_the_extract_and_dedupes_by_id(self):
        trades = self._seq("trades.geojsonseq", [
            point("n1", -2.5, 53.1, name="Sparky", craft="electrician"),
            point("n1", -2.5, 53.1, name="Sparky", craft="electrician"),  # same object in a second file
            square("w2", name="Big Salon", shop="hairdresser"),
            point("n3", -2.5, 53.1, craft="plumber"),  # no name
        ])
        places = self._seq("places.geojsonseq", [point("n9", -2.9, 53.19, name="Chester", place="city")])
        out = os.path.join(self.dir, "out")
        meta = bx.build([trades], [places], out, source="http://example/gb.pbf", min_trades=1, min_places=1,
                        wanted=bx.wanted_pairs(CONFIG))
        self.assertEqual((meta["trades"], meta["places"]), (2, 1))
        self.assertEqual([r["id"] for r in self._read(out, "trades.jsonl.gz")], ["n1", "w2"])
        self.assertEqual(self._read(out, "places.jsonl.gz")[0]["name"], "Chester")
        with open(os.path.join(out, "meta.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["source"], "http://example/gb.pbf")
        self.assertRegex(saved["built_at"], r"^\d{4}-\d\d-\d\dT")

    def test_build_refuses_an_extract_that_is_too_small(self):
        trades = self._seq("trades.geojsonseq", [point("n1", -2.5, 53.1, name="Sparky", craft="electrician")])
        places = self._seq("places.geojsonseq", [point("n9", -2.9, 53.19, name="Chester", place="city")])
        with self.assertRaises(SystemExit) as ctx:
            bx.build([trades], [places], os.path.join(self.dir, "out"))  # real GB-sized minimums
        self.assertIn("looks wrong", str(ctx.exception))

    def test_build_script_checks_sizes_and_swaps_in_only_on_success(self):
        with open(os.path.join(ROOT, "tools", "build_osm_extract.sh"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("set -euo pipefail", text)
        self.assertLess(text.index("build_osm_extract.py\" --trades"), text.index('rm -rf "$OUT"'))
        self.assertIn("500000000", text)


if __name__ == "__main__":
    unittest.main()
