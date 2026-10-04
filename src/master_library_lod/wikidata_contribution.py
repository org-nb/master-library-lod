"""Build reviewed Wikidata contribution proposals from curated place data.

The linked-open-data place files consume Wikidata (enrichment), and
the curation layer adds independently verified facts that Wikidata
often lacks. This module turns that curation layer into proposals a
human can submit to Wikidata through QuickStatements.

Safeguards, enforced in ``build_proposals`` and documented in
``docs/wikidata-contribution.md``:

- Add-only: any property the item already has is skipped, never
  modified or replaced.
- Non-circular: only curation-layer facts are proposable; enrichment
  facts came from Wikidata in the first place.
- Venue-level geo only: municipality-level coordinates are never
  proposed (they would pin a center to its town center).
- Referenced: every statement carries a reference (S854) to a public
  URL, by default the place's own website. Unreferenced facts are
  reported but marked not submittable.
- Human-in-the-loop: this module only writes proposal files; the
  submission itself is a manual QuickStatements import under the
  contributor's own account.

Usage::

    uv run python -m master_library_lod.wikidata_contribution propose
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import rdflib

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLACES_DIR = REPO_ROOT / "data" / "places"
CURATION_JSON = REPO_ROOT / "data" / "import" / "places-curation.json"
WIKIDATA_DIR = REPO_ROOT / "data" / "wikidata"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "master-library-lod-contribution/0.1 (daniel.kapitan@nalandabodhi.org)"

SCHEMA = rdflib.Namespace("https://schema.org/")
NB = rdflib.Namespace("https://data.nalandabodhi.org/ontology/")
WIKIDATA_ENTITY = "http://www.wikidata.org/entity/"

# Wikidata properties this workflow may propose, mapped from the
# curated schema.org facts. Region (P131) is deliberately absent: it
# needs a region item QID, not a string.
PROPOSABLE_PROPERTIES = ("P856", "P625", "P6375", "P281")

PROPERTY_LABELS = {
    "P856": "official website",
    "P625": "coordinates",
    "P6375": "street address",
    "P281": "postal code",
}


class ContributionError(Exception):
    """Base error for the contribution workflow."""


@dataclass(frozen=True)
class PlaceFacts:
    """Curated facts for one place, read from its Turtle file."""

    slug: str
    name: str
    qid: str
    website: str | None
    street_address: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class Proposal:
    """One proposed statement for one Wikidata item."""

    qid: str
    pid: str
    slug: str
    value: str
    reference: str | None
    note: str = ""
    submittable: bool = True

    @property
    def command(self) -> str:
        """The QuickStatements v1 command line for this proposal."""
        parts = [self.qid, self.pid, self.value]
        if self.reference:
            parts += [f'S854|"{self.reference}"']
        return "|".join(parts)


@dataclass(frozen=True)
class Skip:
    """A fact that was considered but not proposed, with the reason."""

    qid: str
    pid: str
    slug: str
    reason: str


def _turtle_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_place_facts(places_dir: Path) -> list[PlaceFacts]:
    """Read wikidata-linked facts from the place Turtle files.

    Args:
        places_dir: Directory with one ``<slug>.ttl`` per place.

    Returns:
        Facts for every place that carries a Wikidata identity
        (``schema:sameAs``); places without one are not candidates.
    """
    facts: list[PlaceFacts] = []
    for path in sorted(places_dir.glob("*.ttl")):
        graph = rdflib.Graph().parse(path, format="turtle")
        slug_values = list(graph.objects(None, NB.placeSlug))
        if len(slug_values) != 1:
            continue
        slug_node = slug_values[0]
        same_as = [
            str(obj)
            for obj in graph.objects(None, SCHEMA.sameAs)
            if str(obj).startswith(WIKIDATA_ENTITY)
        ]
        if not same_as:
            continue
        place = rdflib.URIRef(f"https://data.nalandabodhi.org/place/{slug_node}")
        address = graph.value(place, SCHEMA.address)
        geo = graph.value(place, SCHEMA.geo)
        website = graph.value(place, SCHEMA.url)
        street = (
            graph.value(address, SCHEMA.streetAddress) if address is not None else None
        )
        postal = (
            graph.value(address, SCHEMA.postalCode) if address is not None else None
        )
        facts.append(
            PlaceFacts(
                slug=str(slug_node),
                name=str(graph.value(place, SCHEMA.name) or slug_node),
                qid=same_as[0].removeprefix(WIKIDATA_ENTITY),
                website=str(website) if website else None,
                street_address=str(street) if street else None,
                postal_code=str(postal) if postal else None,
                latitude=float(str(graph.value(geo, SCHEMA.latitude))) if geo else None,
                longitude=float(str(graph.value(geo, SCHEMA.longitude)))
                if geo
                else None,
            )
        )
    return facts


def fetch_live_claims(qids: list[str]) -> dict[str, dict[str, set[str]]]:
    """Fetch the current values of proposable properties per item.

    Args:
        qids: Wikidata item IDs such as ``Q977642``.

    Returns:
        Mapping of QID to property values, e.g.
        ``{"Q977642": {"P856": {"https://kagyu.org"}}}``.

    Raises:
        ContributionError: On HTTP or protocol errors, after one retry.
    """
    values = " ".join(f"wd:{qid}" for qid in qids)
    query = f"""
SELECT ?item ?p856 ?p6375 ?p281 ?coord WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P856 ?p856 . }}
  OPTIONAL {{ ?item wdt:P6375 ?p6375 . }}
  OPTIONAL {{ ?item wdt:P281 ?p281 . }}
  OPTIONAL {{ ?item wdt:P625 ?coord . }}
}}
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
            break
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as error:
            if attempt == 2:
                raise ContributionError(
                    f"Wikidata SPARQL request failed: {error}"
                ) from error
            logger.warning("SPARQL request failed (%s), retrying", error)
            time.sleep(1.0)
    live: dict[str, dict[str, set[str]]] = {qid: {} for qid in qids}
    for row in payload["results"]["bindings"]:
        qid = row["item"]["value"].removeprefix(WIKIDATA_ENTITY)
        for key, pid in (
            ("p856", "P856"),
            ("p6375", "P6375"),
            ("p281", "P281"),
            ("coord", "P625"),
        ):
            if key in row:
                live[qid].setdefault(pid, set()).add(row[key]["value"])
    return live


def build_proposals(
    facts: list[PlaceFacts],
    live: dict[str, dict[str, set[str]]],
    curation: dict[str, dict[str, object]],
) -> tuple[list[Proposal], list[Skip]]:
    """Diff curated facts against live Wikidata claims.

    Args:
        facts: Facts loaded from the place files.
        live: Live claims from ``fetch_live_claims``.
        curation: The curation layer; only facts recorded here are
            proposable, so Wikidata-sourced enrichment is never
            proposed back.

    Returns:
        The proposals and the skipped facts with reasons.
    """
    proposals: list[Proposal] = []
    skips: list[Skip] = []

    for fact in facts:
        item_live = live.get(fact.qid, {})
        curated = curation.get(fact.slug, {})
        candidates: list[tuple[str, str, str | None]] = []

        if fact.website and curated.get("website") == fact.website:
            candidates.append(("P856", f'"{fact.website}"', fact.website))
        if fact.street_address and curated.get("street_address") == fact.street_address:
            # P6375 is monolingual text.
            candidates.append(
                ("P6375", f"{_turtle_string(fact.street_address)}@en", None)
            )
        if fact.postal_code and curated.get("postal_code") == fact.postal_code:
            candidates.append(("P281", f'"{fact.postal_code}"', None))
        if (
            fact.latitude is not None
            and fact.longitude is not None
            and curated.get("geo_source") == "venue"
            and curated.get("latitude") is not None
        ):
            candidates.append(
                ("P625", f"@{fact.latitude:.6f}/{fact.longitude:.6f}", None)
            )

        for pid, value, reference in candidates:
            if pid in item_live:
                skips.append(
                    Skip(
                        fact.qid,
                        pid,
                        fact.slug,
                        "item already has a value; add-only policy",
                    )
                )
                continue
            reference = reference or fact.website
            note = ""
            submittable = bool(reference)
            if not reference:
                note = "no public reference available; find one before submitting"
            if pid == "P6375" and not re.match(r"^\d", fact.street_address or ""):
                note = (note + "; " if note else "") + (
                    "value has no house number; verify it is a real street address"
                )
            proposals.append(
                Proposal(
                    qid=fact.qid,
                    pid=pid,
                    slug=fact.slug,
                    value=value,
                    reference=reference,
                    note=note,
                    submittable=submittable,
                )
            )

        if fact.latitude is not None and curated.get("geo_source") == "municipality":
            skips.append(
                Skip(
                    fact.qid,
                    "P625",
                    fact.slug,
                    "municipality-level coordinates; never proposed",
                )
            )
    return proposals, skips


def render_quickstatements(proposals: list[Proposal]) -> str:
    """Render submittable proposals as QuickStatements v1 commands."""
    lines = [
        "# QuickStatements v1 commands, generated by",
        "# master_library_lod.wikidata_contribution. Review the matching",
        "# proposals.md, delete lines you do not want, and import at",
        "# https://quickstatements.toolforge.org under your own account.",
        "# Never run unreviewed; you are accountable for every edit.",
        "",
    ]
    lines += [p.command for p in proposals if p.submittable]
    lines.append("")
    return "\n".join(lines)


def render_report(proposals: list[Proposal], skips: list[Skip]) -> str:
    """Render the human review report for a proposal batch."""
    lines = [
        "# Wikidata contribution proposals",
        "",
        "Generated by `master_library_lod.wikidata_contribution`. Every",
        "statement is add-only, sourced from the curation layer, and carries",
        "a reference. Review, trim `proposals.qs`, submit via QuickStatements,",
        "then append the outcome to `log.md`.",
        "",
        "## Proposals",
        "",
    ]
    if not proposals:
        lines.append("_None: Wikidata already holds all curated facts._")
    else:
        lines.append(
            "| Item | Property | Proposed value | Reference | Submittable | Note |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for p in proposals:
            lines.append(
                f"| [{p.qid}](https://www.wikidata.org/wiki/{p.qid}) "
                f"({p.slug}) "
                f"| {p.pid} ({PROPERTY_LABELS[p.pid]}) "
                f"| `{p.value}` "
                f"| {p.reference or '-'} "
                f"| {'yes' if p.submittable else '**no**'} "
                f"| {p.note or '-'} |"
            )
    lines += ["", "## Skipped (for transparency)", ""]
    if not skips:
        lines.append("_None._")
    else:
        lines.append("| Item | Property | Slug | Reason |")
        lines.append("| --- | --- | --- | --- |")
        for s in skips:
            lines.append(f"| {s.qid} | {s.pid} | {s.slug} | {s.reason} |")
    lines.append("")
    return "\n".join(lines)


def propose() -> None:
    """Write proposal files for the current curation layer.

    Reads the place files and the locally supplied curation file,
    fetches live Wikidata claims, and writes ``data/wikidata/
    proposals.qs`` and ``proposals.md``. Without a curation file
    (it is not distributed with the repository) no proposals are
    possible, since only curation-layer facts may be proposed.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    facts = load_place_facts(PLACES_DIR)
    if CURATION_JSON.exists():
        with CURATION_JSON.open(encoding="utf-8") as handle:
            curation = json.load(handle)
    else:
        logger.warning(
            "%s not found; supply the curation layer manually to "
            "propose contributions (see README, Regenerating)",
            CURATION_JSON,
        )
        curation = {}
    qids = sorted({fact.qid for fact in facts})
    logger.info("fetching live claims for %s items", len(qids))
    live = fetch_live_claims(qids)
    proposals, skips = build_proposals(facts, live, curation)
    WIKIDATA_DIR.mkdir(parents=True, exist_ok=True)
    (WIKIDATA_DIR / "proposals.qs").write_text(
        render_quickstatements(proposals), encoding="utf-8"
    )
    (WIKIDATA_DIR / "proposals.md").write_text(
        render_report(proposals, skips), encoding="utf-8"
    )
    logger.info(
        "wrote %s proposals (%s submittable) and %s skips to %s",
        len(proposals),
        sum(1 for p in proposals if p.submittable),
        len(skips),
        WIKIDATA_DIR,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the contribution CLI.

    Args:
        argv: Command line arguments without the program name;
            defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = argv if argv is not None else sys.argv[1:]
    if args == ["propose"]:
        propose()
        return 0
    print("usage: python -m master_library_lod.wikidata_contribution [propose]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
