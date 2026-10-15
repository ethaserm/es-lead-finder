#!/usr/bin/env bash
# Builds the local OpenStreetMap extract used by leadfinder.py (see tools/build_osm_extract.py).
# Downloads the free Geofabrik Great Britain extract, so the workflow only runs it about once a week
# (the result is cached). Needs: curl, osmium-tool, python3.
#   usage: tools/build_osm_extract.sh [output-dir]      (default: osm_extract)
set -euo pipefail

OUT="${1:-osm_extract}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
SRC_URL="${OSM_SOURCE_URL:-https://download.geofabrik.de/europe/great-britain-latest.osm.pbf}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v osmium >/dev/null || { echo "osmium-tool is not installed"; exit 1; }

echo "downloading $SRC_URL"
FINAL_URL="$(curl -fSL --retry 4 --retry-delay 15 --retry-all-errors -o "$WORK/src.osm.pbf" -w '%{url_effective}' "$SRC_URL")"
SIZE="$(stat -c %s "$WORK/src.osm.pbf")"
echo "downloaded $((SIZE / 1048576)) MB from $FINAL_URL"
[ "$SIZE" -gt "${OSM_MIN_SOURCE_BYTES:-500000000}" ] || { echo "download is too small to be the GB extract"; exit 1; }

mapfile -t FILTERS < <(python3 "$HERE/build_osm_extract.py" --print-filters "$ROOT/config.json")
echo "keeping: ${FILTERS[*]}"
osmium tags-filter "$WORK/src.osm.pbf" "${FILTERS[@]}" -o "$WORK/trades.osm.pbf" --overwrite
osmium tags-filter "$WORK/src.osm.pbf" n/place=city,town -o "$WORK/places.osm.pbf" --overwrite
rm -f "$WORK/src.osm.pbf"

osmium export "$WORK/trades.osm.pbf" -f geojsonseq --geometry-types=point,polygon --add-unique-id=type_id \
  -o "$WORK/trades.geojsonseq" --overwrite
osmium export "$WORK/places.osm.pbf" -f geojsonseq --geometry-types=point -o "$WORK/places.geojsonseq" --overwrite

# build into a temp folder and only swap it in when it looks right, so a failed build never
# replaces a good (cached) extract
python3 "$HERE/build_osm_extract.py" --trades "$WORK/trades.geojsonseq" --places "$WORK/places.geojsonseq" \
  --out "$WORK/out" --source "$FINAL_URL" --config "$ROOT/config.json" ${OSM_MIN_TRADES:+--min-trades "$OSM_MIN_TRADES"} ${OSM_MIN_PLACES:+--min-places "$OSM_MIN_PLACES"}
rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
mv "$WORK/out" "$OUT"
cat "$OUT/meta.json"; echo
