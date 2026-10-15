#!/usr/bin/env python3
"""Helpers for building the local OpenStreetMap extract that leadfinder.py searches.

The public Overpass servers are often overloaded, so once a week the workflow downloads the free
Geofabrik Great Britain extract, uses `osmium` to keep only the trades/places we search for, and this
script shrinks osmium's GeoJSON-seq export into two small gzip files:

    osm_extract/trades.jsonl.gz   {"id", "lat", "lon", "tags"} per named business
    osm_extract/places.jsonl.gz   {"name", "lat", "lon", "place"} per city / town
    osm_extract/meta.json         where and when it was built

Usage (see build_osm_extract.sh):
    python tools/build_osm_extract.py --print-filters config.json
    python tools/build_osm_extract.py --trades a.geojsonseq [b.geojsonseq ...] \\
        --places p.geojsonseq --out osm_extract --source "<url>"
Only the standard library is used.
"""
import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone

# the only tags leadfinder.py reads from an OSM object (plus the trade tags used for matching)
KEEP_TAGS = ("name", "website", "contact:website", "phone", "contact:phone", "email", "contact:email",
             "addr:street", "addr:city", "addr:postcode", "craft", "shop", "amenity")
MIN_TRADES, MIN_PLACES = 10000, 300  # sanity floor: a smaller result means the build went wrong


def osmium_filters(config):
    """osmium tags-filter expressions for every OSM tag the trades in config.json search, e.g.
    ['nwr/craft=electrician,plumber', 'nwr/shop=hairdresser']."""
    wanted = {}
    for trade in config.get("trades", []):
        for key, value in trade.get("osm", []):
            wanted.setdefault(key, set()).add(value)
    return [f"nwr/{key}=" + ",".join(sorted(values)) for key, values in sorted(wanted.items())]


def wanted_pairs(config):
    """{(key, value)} for every OSM tag the trades in config.json search."""
    return {(key, value) for trade in config.get("trades", []) for key, value in trade.get("osm", [])}


def iter_features(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e").strip()  # GeoJSON-seq lines start with a record separator
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _mean_point(ring):
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def centre(geometry):
    """(lat, lon) of a Point, or the mean of the outer ring's vertices for a polygon; None if unusable."""
    if not geometry:
        return None
    kind, coords = geometry.get("type"), geometry.get("coordinates")
    try:
        if kind == "Point":
            return coords[1], coords[0]
        if kind == "Polygon":
            return _mean_point(coords[0])
        if kind == "MultiPolygon":
            return _mean_point(coords[0][0])
    except (IndexError, TypeError):
        return None
    return None


def trade_record(feature, wanted=None):
    """Compact record for a named business, or None (no name / no location / not one of the trades).

    osmium also exports unrelated objects that merely share a building outline with a match, so
    when `wanted` ({(key, value)}) is given only objects carrying one of those tags are kept."""
    props = feature.get("properties") or {}
    if not props.get("name"):
        return None
    if wanted is not None and not any(props.get(k) == v for k, v in wanted):
        return None
    point = centre(feature.get("geometry"))
    if point is None:
        return None
    tags = {k: props[k] for k in KEEP_TAGS if props.get(k)}
    return {"id": feature.get("id"), "lat": round(point[0], 5), "lon": round(point[1], 5), "tags": tags}


def place_record(feature):
    props = feature.get("properties") or {}
    if not props.get("name") or props.get("place") not in ("city", "town"):
        return None
    point = centre(feature.get("geometry"))
    if point is None:
        return None
    return {"name": props["name"], "lat": round(point[0], 5), "lon": round(point[1], 5), "place": props["place"]}


def write_jsonl_gz(path, records):
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
            n += 1
    return n


def build(trade_paths, place_paths, out_dir, source="", min_trades=MIN_TRADES, min_places=MIN_PLACES, wanted=None):
    os.makedirs(out_dir, exist_ok=True)
    seen = set()

    def trades():
        for path in trade_paths:
            for feat in iter_features(path):
                rec = trade_record(feat, wanted)
                if rec is None:
                    continue
                if rec["id"] is not None:
                    if rec["id"] in seen:
                        continue
                    seen.add(rec["id"])
                yield rec

    def places():
        for path in place_paths:
            for feat in iter_features(path):
                rec = place_record(feat)
                if rec is not None:
                    yield rec
    n_trades = write_jsonl_gz(os.path.join(out_dir, "trades.jsonl.gz"), trades())
    n_places = write_jsonl_gz(os.path.join(out_dir, "places.jsonl.gz"), places())
    if n_trades < min_trades or n_places < min_places:
        raise SystemExit(f"extract looks wrong: {n_trades} businesses, {n_places} places "
                         f"(expected at least {min_trades} and {min_places})")
    meta = {"built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "source": source,
            "trades": n_trades, "places": n_places}
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print-filters", metavar="CONFIG", help="print the osmium tag filters for config.json and exit")
    ap.add_argument("--trades", nargs="+", default=[], help="GeoJSON-seq files of tagged businesses")
    ap.add_argument("--places", nargs="+", default=[], help="GeoJSON-seq files of place=city/town points")
    ap.add_argument("--out", default="osm_extract")
    ap.add_argument("--source", default="")
    ap.add_argument("--config", help="config.json: keep only businesses carrying one of its trade tags")
    ap.add_argument("--min-trades", type=int, default=MIN_TRADES, help="refuse to write an extract with fewer businesses")
    ap.add_argument("--min-places", type=int, default=MIN_PLACES, help="refuse to write an extract with fewer towns")
    args = ap.parse_args(argv)
    if args.print_filters:
        with open(args.print_filters, encoding="utf-8") as fh:
            print("\n".join(osmium_filters(json.load(fh))))
        return 0
    if not args.trades or not args.places:
        ap.error("--trades and --places are required")
    wanted = None
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            wanted = wanted_pairs(json.load(fh))
    meta = build(args.trades, args.places, args.out, args.source, args.min_trades, args.min_places, wanted)
    print(f"built {meta['trades']} businesses and {meta['places']} places into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
