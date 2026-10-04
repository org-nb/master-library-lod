"""Import the Airtable Location export into the linked-open-data repo.

Parses ``data/import/places.eml`` (an Airtable "Location" table
export), enriches places through the Wikidata SPARQL endpoint, and
renders one Turtle file per place under ``data/places/``.

Modeling: schema.org carries the descriptive profile (name, postal
address, geo-coordinates, url, sameAs); each place is also typed
``po:Place`` from the BBC Programmes ontology to stay aligned with the
master-library catalog semantics (ADR-0001 profile, retained by
ADR-0003). Registry fields (slug, venue code, location id) use the
local ``nb:`` vocabulary defined in ``vocabulary/master-library.ttl``.

Usage::

    uv run python -m master_library_lod.places_import lookup   # network
    uv run python -m master_library_lod.places_import write    # offline
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLACES_EML = REPO_ROOT / "data" / "import" / "places.eml"
ENRICHMENT_JSON = REPO_ROOT / "data" / "import" / "places-enrichment.json"
CURATION_JSON = REPO_ROOT / "data" / "import" / "places-curation.json"
PLACES_DIR = REPO_ROOT / "data" / "places"

DATA_BASE = "https://data.nalandabodhi.org/"
PLACE_BASE = f"{DATA_BASE}place/"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "master-library-lod-import/0.1 (daniel.kapitan@nalandabodhi.org)"
SPARQL_CHUNK_SIZE = 20
SPARQL_PAUSE_SECONDS = 1.0

# Values that mark a venue or municipality column as "no real value".
PLACEHOLDER_VENUES = {
    "n/a",
    "unknown venue",
    "unidentified venue",
    "orphan partial recordings",
    "various (tibet)",
}
PLACEHOLDER_MUNICIPALITIES = {
    "",
    "n/a",
    "unknown municipality",
    "unidentified municipality",
    "various (tibet)",
}
# Venues that describe a group of people or a private setting rather
# than a findable location; only the municipality is looked up.
GROUP_VENUE_PREFIXES = ("ktgr",)
PRIVATE_VENUES = {"private home"}


class PlacesImportError(Exception):
    """Base error for the places import pipeline."""


class EmlParseError(PlacesImportError):
    """Raised when the Airtable export cannot be parsed."""


@dataclass(frozen=True)
class PlaceRecord:
    """One row of the Airtable Location table."""

    name: str
    location_id: str
    venue: str
    venue_code: str
    municipality: str
    country_name: str
    country_code: str
    short_name: str


class _TableParser(HTMLParser):
    """Collects rows from the first HTML table with nine columns."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_places_eml(path: Path) -> list[PlaceRecord]:
    """Parse an Airtable Location-table export into place records.

    Args:
        path: Path to the ``.eml`` export.

    Returns:
        Records in export order, without the header row.

    Raises:
        EmlParseError: If the message has no HTML body or the table
            does not have the expected nine columns.
    """
    with path.open("rb") as handle:
        message = email.message_from_binary_file(handle, policy=email.policy.default)
    body = message.get_body(preferencelist=("html",))
    if body is None:
        raise EmlParseError(f"no HTML body in {path}")
    parser = _TableParser()
    parser.feed(body.get_content())
    rows = [row for row in parser.rows if len(row) == 9]
    if not rows or "Location ID" not in rows[0]:
        raise EmlParseError(f"expected Location table with 9 columns in {path}")
    records = []
    for row in rows[1:]:
        records.append(
            PlaceRecord(
                name=row[0],
                location_id=row[1],
                venue=row[2],
                venue_code=row[3],
                municipality=row[4],
                country_name=row[5],
                country_code=row[6],
                short_name=row[7],
            )
        )
    return records


def _ascii_fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def kebab(value: str) -> str:
    """Return a kebab-case slug for a human-readable value."""
    folded = _ascii_fold(value).lower()
    folded = re.sub(r"[’']", "", folded)
    folded = re.sub(r"&", " and ", folded)
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", folded)).strip("-")


def _is_placeholder(value: str, placeholders: set[str]) -> bool:
    return value.strip().lower() in placeholders


def _is_group_venue(record: PlaceRecord) -> bool:
    return record.venue.lower().startswith(GROUP_VENUE_PREFIXES)


def assign_slugs(records: list[PlaceRecord]) -> dict[str, str]:
    """Return a mapping of location id to a unique, stable slug.

    The slug is the kebab-cased venue name, extended with the
    municipality (or, failing that, the country code or location id)
    only when the venue name alone is ambiguous. Placeholder venues get
    an opaque ``place-l<location id>`` slug because they do not name a
    real place.
    """
    bases: dict[str, list[str]] = {}
    for record in records:
        if _is_placeholder(record.venue, PLACEHOLDER_VENUES):
            base = (
                f"place-l{record.location_id}"
                if record.location_id
                else kebab(record.venue)
            )
        else:
            base = kebab(record.venue)
        bases.setdefault(base, []).append(record.location_id)

    slugs: dict[str, str] = {}
    for record in records:
        if _is_placeholder(record.venue, PLACEHOLDER_VENUES):
            base = (
                f"place-l{record.location_id}"
                if record.location_id
                else kebab(record.venue)
            )
        else:
            base = kebab(record.venue)
        if len(bases[base]) > 1:
            municipality = record.municipality.strip()
            if not _is_placeholder(municipality, PLACEHOLDER_MUNICIPALITIES):
                candidate = f"{base}-{kebab(municipality)}"
            elif record.country_code:
                candidate = f"{base}-{kebab(record.country_code)}"
            else:
                candidate = f"{base}-l{record.location_id or kebab(record.venue_code)}"
            slugs[record.location_id] = candidate
        else:
            slugs[record.location_id] = base
    duplicates = {
        slug for slug in slugs.values() if list(slugs.values()).count(slug) > 1
    }
    if duplicates:
        raise PlacesImportError(f"slug collision: {sorted(duplicates)}")
    return slugs


def _record_slug(record: PlaceRecord, slugs: dict[str, str]) -> str:
    return slugs[record.location_id]


def lookup_category(record: PlaceRecord) -> str:
    """Return the enrichment category for a record.

    Returns:
        ``venue`` when the venue name should be looked up directly,
        ``municipality`` when only the municipality is findable
        (private homes, student groups), ``none`` when nothing about
        the location is known.
    """
    if _is_placeholder(record.venue, PLACEHOLDER_VENUES):
        return "none"
    municipality = record.municipality.strip()
    if _is_group_venue(record) or record.venue.lower() in PRIVATE_VENUES:
        return (
            "municipality"
            if not _is_placeholder(municipality, PLACEHOLDER_MUNICIPALITIES)
            else "none"
        )
    return "venue"


def _sparql_select(query: str) -> list[dict[str, dict[str, str]]]:
    """Run one SELECT query against the Wikidata SPARQL endpoint.

    Args:
        query: A SPARQL SELECT query string.

    Returns:
        The result bindings.

    Raises:
        PlacesImportError: On HTTP or protocol errors, after one retry.
    """
    request = urllib.request.Request(
        WIKIDATA_SPARQL,
        data=urllib.parse.urlencode({"query": query}).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            return payload["results"]["bindings"]
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as error:
            if attempt == 2:
                raise PlacesImportError(
                    f"Wikidata SPARQL request failed: {error}"
                ) from error
            logger.warning("SPARQL request failed (%s), retrying", error)
            time.sleep(SPARQL_PAUSE_SECONDS)
    raise PlacesImportError("unreachable")


_LOOKUP_QUERY = """
SELECT ?name ?item ?itemLabel ?coord ?website ?countryCode ?street
       ?instanceOf ?population WHERE {
  VALUES ?name { %s }
  ?item rdfs:label|skos:altLabel ?name .
  OPTIONAL { ?item wdt:P625 ?coord . }
  OPTIONAL { ?item wdt:P856 ?website . }
  OPTIONAL { ?item wdt:P17 ?country . ?country wdt:P297 ?countryCode . }
  OPTIONAL { ?item wdt:P6375 ?street . }
  OPTIONAL { ?item wdt:P31 ?instanceOf . }
  OPTIONAL { ?item wdt:P1082 ?population . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _query_names(names: list[str]) -> list[dict[str, dict[str, str]]]:
    values = " ".join(f'"{name}"@en' for name in names)
    return _sparql_select(_LOOKUP_QUERY % values)


def _coord_to_lat_lon(value: str) -> tuple[float, float] | None:
    match = re.match(r"Point\((-?\d+\.\d+) (-?\d+\.\d+)\)", value)
    if match is None:
        return None
    return float(match.group(2)), float(match.group(1))


def _binding(row: dict[str, dict[str, str]], key: str) -> str | None:
    value = row.get(key)
    return value["value"] if value else None


def collect_wikidata_matches(
    records: list[PlaceRecord], slugs: dict[str, str]
) -> dict[str, dict[str, object]]:
    """Look up venue and municipality names on Wikidata.

    Args:
        records: The parsed place records.
        slugs: Location-id-to-slug mapping.

    Returns:
        Enrichment results keyed by slug, ready for manual review and
        merging in ``merge_enrichment``.
    """
    venue_names = sorted(
        {record.venue for record in records if lookup_category(record) == "venue"}
    )
    municipality_names = sorted(
        {
            record.municipality.strip()
            for record in records
            if lookup_category(record) in ("venue", "municipality")
            and not _is_placeholder(
                record.municipality.strip(), PLACEHOLDER_MUNICIPALITIES
            )
        }
    )
    matches: dict[str, dict[str, object]] = {}
    for kind, names in (("venue", venue_names), ("municipality", municipality_names)):
        for start in range(0, len(names), SPARQL_CHUNK_SIZE):
            chunk = names[start : start + SPARQL_CHUNK_SIZE]
            logger.info("querying %s names %s..%s", kind, chunk[0], chunk[-1])
            for row in _query_names(chunk):
                name = _binding(row, "name")
                slug_key = f"{kind}:{name}"
                entry = matches.setdefault(
                    slug_key, {"kind": kind, "name": name, "items": []}
                )
                item = _binding(row, "item")
                items = cast("list[dict[str, object]]", entry["items"])
                item_entry: dict[str, object] | None = next(
                    (i for i in items if i["item"] == item), None
                )
                if item_entry is None:
                    item_entry = {
                        "item": item,
                        "label": _binding(row, "itemLabel"),
                        "wikidata": item,
                    }
                    items.append(item_entry)
                coord = (
                    _coord_to_lat_lon(row["coord"]["value"]) if "coord" in row else None
                )
                if coord:
                    item_entry["latitude"], item_entry["longitude"] = coord
                for key in ("website", "countryCode", "street"):
                    value = _binding(row, key)
                    if value:
                        item_entry[key] = value
                instance_of = _binding(row, "instanceOf")
                if instance_of:
                    instances = cast(
                        "list[str]", item_entry.setdefault("instance_of", [])
                    )
                    instances.append(instance_of)
                population = _binding(row, "population")
                if population and population.replace(".", "").isdigit():
                    item_entry["population"] = max(
                        float(str(item_entry.get("population", 0))), float(population)
                    )
            time.sleep(SPARQL_PAUSE_SECONDS)
    return matches


def _pick_item(
    items: list[dict[str, object]], country_code: str, prefer_population: bool = False
) -> dict[str, object] | None:
    """Pick the best Wikidata item for a record.

    For records with a country code, only same-country items (or
    items with no country statement at all) are eligible, so a
    same-named place abroad never wins. Prefers items with
    coordinates; with ``prefer_population`` (used for municipality
    matches) the most populous candidate wins, which disambiguates
    same-named cities such as Albany. Returns None when two equally
    good candidates remain.
    """
    if not items:
        return None
    if country_code:
        # Never accept a foreign-country match; items without a
        # country statement are the only acceptable fallback.
        same_country = [
            item for item in items if item.get("countryCode") == country_code
        ]
        no_country = [item for item in items if "countryCode" not in item]
        pool = same_country or no_country
        if not pool:
            return None
    else:
        pool = items
    with_coord = [item for item in pool if "latitude" in item]
    pool = with_coord or pool
    if len({item["item"] for item in pool}) == 1:
        return pool[0]
    if prefer_population:
        populations = [float(str(item.get("population", 0))) for item in pool]
        best = max(populations)
        if populations.count(best) == 1:
            return pool[populations.index(best)]
    return None


def merge_enrichment(
    records: list[PlaceRecord],
    slugs: dict[str, str],
    matches: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Merge Wikidata matches into one enrichment entry per record.

    Args:
        records: The parsed place records.
        slugs: Location-id-to-slug mapping.
        matches: Raw matches from ``collect_wikidata_matches``.

    Returns:
        Enrichment keyed by slug with ``source`` in
        ``venue|municipality|none`` and ``ambiguous`` flagged for
        manual review.
    """
    enrichment: dict[str, dict[str, object]] = {}
    for record in records:
        slug = _record_slug(record, slugs)
        category = lookup_category(record)
        if category == "none":
            enrichment[slug] = {"source": "none"}
            continue
        names = [record.venue] if category == "venue" else []
        municipality = record.municipality.strip()
        if not _is_placeholder(municipality, PLACEHOLDER_MUNICIPALITIES):
            names.append(municipality)
        entry: dict[str, object] = {
            "source": category,
            "country_code": record.country_code,
        }
        for name in names:
            is_venue_name = name == record.venue
            key = ("venue" if is_venue_name else "municipality") + ":" + name
            found = matches.get(key)
            if not found:
                continue
            item = _pick_item(
                cast("list[dict[str, object]]", found["items"]),
                record.country_code,
                prefer_population=not is_venue_name,
            )
            if item is None:
                entry["ambiguous"] = True
                entry["ambiguous_name"] = name
                continue
            if is_venue_name:
                entry.update(
                    {
                        "wikidata": item.get("wikidata"),
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                        "street_address": item.get("street"),
                        "website": item.get("website"),
                    }
                )
                entry["geo_source"] = "venue"
            else:
                # A municipality fallback may only pin coordinates;
                # asserting the city's website or identity for a venue
                # would be false.
                entry.update(
                    {
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                    }
                )
                entry["geo_source"] = "municipality"
                entry["municipality_wikidata"] = item.get("wikidata")
            entry["matched_name"] = name
            break
        enrichment[slug] = entry
    return enrichment


def load_enrichment(path: Path) -> dict[str, dict[str, object]]:
    """Load a previously saved enrichment cache."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def apply_curation(
    enrichment: dict[str, dict[str, object]], curation: dict[str, dict[str, object]]
) -> dict[str, dict[str, object]]:
    """Apply manual curation overrides on top of the enrichment.

    A curation entry updates the named fields for its slug; the
    optional ``_clear`` list removes fields first (for example to drop
    a Wikidata identity claim that the automatic match got wrong).

    Args:
        enrichment: Merged enrichment keyed by slug.
        curation: Curation overrides keyed by slug.

    Returns:
        The enrichment with curation applied, as a new dict.

    Raises:
        PlacesImportError: If a curation entry names an unknown slug.
    """
    curated = {slug: dict(entry) for slug, entry in enrichment.items()}
    for slug, overrides in curation.items():
        if slug not in curated:
            raise PlacesImportError(f"curation entry for unknown slug: {slug}")
        for field in cast("list[str]", overrides.get("_clear", [])):
            curated[slug].pop(field, None)
        for field, value in overrides.items():
            if field != "_clear":
                curated[slug][field] = value
    return curated


def _turtle_string(value: object) -> str:
    """Render a scalar as a Turtle string literal.

    JSON string escaping is a subset of Turtle's, so ``json.dumps``
    produces a valid quoted literal.
    """
    return json.dumps(value, ensure_ascii=False)


def _format_decimal(value: object) -> str:
    """Render a coordinate as a plain Turtle decimal literal."""
    return f"{float(str(value)):.6f}".rstrip("0").rstrip(".")


def render_place_turtle(
    record: PlaceRecord, slug: str, enrichment: dict[str, object] | None
) -> str:
    """Render one place as a Turtle document.

    Args:
        record: The parsed place record.
        slug: The registry slug for the record.
        enrichment: Merged enrichment entry, if any.

    Returns:
        The full Turtle text for the place file.
    """
    enrichment = enrichment or {}
    municipality = record.municipality.strip()
    has_municipality = not _is_placeholder(municipality, PLACEHOLDER_MUNICIPALITIES)

    lines = [
        f"# Place: {record.venue}",
        "# Generated by master_library_lod.places_import; do not edit.",
        "# Fix data in data/import/places-curation.json and re-run 'write'.",
        "@prefix nb: <https://data.nalandabodhi.org/ontology/> .",
        "@prefix po: <http://purl.org/ontology/po/> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix schema: <https://schema.org/> .",
        "",
        f"<{PLACE_BASE}{slug}> a po:Place, schema:Place ;",
        f"    rdfs:label {_turtle_string(record.venue)} ;",
        f"    schema:name {_turtle_string(record.venue)} ;",
        f"    nb:placeSlug {_turtle_string(slug)} ;",
    ]

    address_lines: list[str] = []
    address_fields = [
        ("schema:streetAddress", enrichment.get("street_address")),
        ("schema:addressLocality", municipality if has_municipality else None),
        ("schema:addressRegion", enrichment.get("address_region")),
        ("schema:postalCode", enrichment.get("postal_code")),
        ("schema:addressCountry", record.country_code or None),
    ]
    for predicate, value in address_fields:
        if value:
            address_lines.append(f"        {predicate} {_turtle_string(value)} ;")
    if address_lines:
        lines.append("    schema:address [ a schema:PostalAddress ;")
        lines.extend(address_lines)
        lines.append("    ] ;")

    if (
        enrichment.get("latitude") is not None
        and enrichment.get("longitude") is not None
    ):
        lines.append("    schema:geo [ a schema:GeoCoordinates ;")
        lines.append(
            f"        schema:latitude {_format_decimal(enrichment['latitude'])} ;"
        )
        lines.append(
            f"        schema:longitude {_format_decimal(enrichment['longitude'])} ;"
        )
        lines.append("    ] ;")

    if enrichment.get("website"):
        lines.append(f"    schema:url <{enrichment['website']}> ;")
    if enrichment.get("wikidata"):
        lines.append(f"    schema:sameAs <{enrichment['wikidata']}> ;")

    # Replace the trailing separator with the statement terminator.
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    lines.append("")

    comments: list[str] = []
    if enrichment.get("geo_source") == "municipality":
        comments.append(
            f"TODO: venue not identified; coordinates reference the municipality ({municipality})."
        )
    if enrichment.get("ambiguous"):
        comments.append(
            f'TODO: multiple Wikidata candidates matched "{enrichment.get("ambiguous_name")}"; pick one manually.'
        )
    if lookup_category(record) != "none":
        missing = [
            field
            for field in ("street_address", "latitude", "website")
            if not enrichment.get(field)
        ]
        if missing:
            comments.append(f"TODO: unresolved place properties: {', '.join(missing)}.")
    provenance = [f"Source: Airtable Location export, record name: {record.name}."]
    if enrichment.get("matched_name"):
        wikidata = enrichment.get("wikidata") or enrichment.get("municipality_wikidata")
        provenance.append(
            f"Enriched via Wikidata ({enrichment['matched_name']} -> {wikidata})."
        )
    for comment in comments + provenance:
        lines.append(f"# {comment}")
    lines.append("")
    return "\n".join(lines)


def write_place_files(
    records: list[PlaceRecord],
    slugs: dict[str, str],
    enrichment_by_slug: dict[str, dict[str, object]],
    places_dir: Path,
) -> list[Path]:
    """Write one Turtle file per published record into ``places_dir``.

    Only records that name a real, findable venue are published
    (``lookup_category == "venue"``). Private homes, student groups
    (``ktgr-*``), and placeholder records (``place-l*``, orphan
    recordings) stay in the import layer but are not published as
    linked data. Files for records that are no longer published are
    pruned.

    Args:
        records: The parsed place records.
        slugs: Location-id-to-slug mapping.
        enrichment_by_slug: Merged enrichment keyed by slug.
        places_dir: Target directory for the Turtle files.

    Returns:
        The written file paths, in record order.
    """
    places_dir.mkdir(parents=True, exist_ok=True)
    published = [
        (record, _record_slug(record, slugs))
        for record in records
        if lookup_category(record) == "venue"
    ]
    written: list[Path] = []
    for record, slug in published:
        path = places_dir / f"{slug}.ttl"
        path.write_text(
            render_place_turtle(record, slug, enrichment_by_slug.get(slug)),
            encoding="utf-8",
        )
        written.append(path)
    # Prune files whose records are no longer published.
    current_stems = {slug for _, slug in published}
    for stale in places_dir.glob("*.ttl"):
        if stale.stem not in current_stems:
            stale.unlink()
    return written


def _cli_lookup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    records = parse_places_eml(PLACES_EML)
    slugs = assign_slugs(records)
    matches = collect_wikidata_matches(records, slugs)
    enrichment = merge_enrichment(records, slugs, matches)
    ENRICHMENT_JSON.write_text(
        json.dumps(enrichment, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    resolved = sum(
        1 for entry in enrichment.values() if entry.get("latitude") is not None
    )
    logger.info(
        "wrote %s: %s/%s records have coordinates",
        ENRICHMENT_JSON,
        resolved,
        len(records),
    )


def _cli_write() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    records = parse_places_eml(PLACES_EML)
    slugs = assign_slugs(records)
    enrichment = load_enrichment(ENRICHMENT_JSON) if ENRICHMENT_JSON.exists() else {}
    if CURATION_JSON.exists():
        enrichment = apply_curation(enrichment, load_enrichment(CURATION_JSON))
    written = write_place_files(records, slugs, enrichment, PLACES_DIR)
    logger.info("wrote %s place files to %s", len(written), PLACES_DIR)


def main(argv: list[str] | None = None) -> int:
    """Run the import CLI.

    Args:
        argv: Command line arguments without the program name;
            defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = argv if argv is not None else sys.argv[1:]
    if args == ["lookup"]:
        _cli_lookup()
        return 0
    if args == ["write"]:
        _cli_write()
        return 0
    print("usage: python -m master_library_lod.places_import [lookup|write]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
