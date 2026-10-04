"""Tests for the Wikidata contribution workflow.

GIVEN curated place facts in data/places and live Wikidata claims
WHEN proposals are built
THEN only add-only, referenced, non-circular statements are proposed.
"""

from __future__ import annotations

import pytest

from master_library_lod.wikidata_contribution import (
    build_proposals,
    load_place_facts,
    render_quickstatements,
    render_report,
)

FIXTURE_TTL = """\
# Place: Karma Triyana Dharmachakra
@prefix nb: <https://data.nalandabodhi.org/ontology/> .
@prefix schema: <https://schema.org/> .

<https://data.nalandabodhi.org/place/karma-triyana-dharmachakra> a schema:Place ;
    schema:name "Karma Triyana Dharmachakra" ;
    nb:placeSlug "karma-triyana-dharmachakra" ;
    schema:address [ a schema:PostalAddress ;
        schema:streetAddress "435 Meads Mountain Road" ;
        schema:postalCode "12409" ;
        schema:addressCountry "US" ;
    ] ;
    schema:url <https://kagyu.org> ;
    schema:sameAs <http://www.wikidata.org/entity/Q977642> .
"""

MUNICIPALITY_GEO_TTL = """\
# Place: Karme Choling
@prefix nb: <https://data.nalandabodhi.org/ontology/> .
@prefix schema: <https://schema.org/> .

<https://data.nalandabodhi.org/place/karme-choling> a schema:Place ;
    schema:name "Karmê Chöling" ;
    nb:placeSlug "karme-choling" ;
    schema:address [ a schema:PostalAddress ;
        schema:streetAddress "494 U.S. Route 5 South" ;
        schema:postalCode "05821" ;
    ] ;
    schema:geo [ a schema:GeoCoordinates ;
        schema:latitude 44.3197 ;
        schema:longitude -72.0789 ;
    ] ;
    schema:url <https://karmecholing.org> ;
    schema:sameAs <http://www.wikidata.org/entity/Q6372782> .
"""


@pytest.fixture
def places_dir(tmp_path):
    (tmp_path / "karma-triyana-dharmachakra.ttl").write_text(FIXTURE_TTL)
    (tmp_path / "karme-choling.ttl").write_text(MUNICIPALITY_GEO_TTL)
    return tmp_path


@pytest.fixture
def curation():
    return {
        "karma-triyana-dharmachakra": {
            "street_address": "435 Meads Mountain Road",
            "postal_code": "12409",
            "website": "https://kagyu.org",
        },
        "karme-choling": {
            "latitude": 44.3197,
            "longitude": -72.0789,
            "geo_source": "municipality",
            "street_address": "494 U.S. Route 5 South",
            "postal_code": "05821",
            "website": "https://karmecholing.org",
        },
    }


class TestLoadPlaceFacts:
    def test_loads_sameas_url_address(self, places_dir):
        facts = load_place_facts(places_dir)
        ktd = next(f for f in facts if f.slug == "karma-triyana-dharmachakra")
        assert ktd.qid == "Q977642"
        assert ktd.website == "https://kagyu.org"
        assert ktd.street_address == "435 Meads Mountain Road"
        assert ktd.postal_code == "12409"

    def test_ignores_places_without_wikidata_identity(self, tmp_path):
        (tmp_path / "no-identity.ttl").write_text(
            """\
@prefix nb: <https://data.nalandabodhi.org/ontology/> .
@prefix schema: <https://schema.org/> .

<https://data.nalandabodhi.org/place/no-identity> a schema:Place ;
    schema:name "Nowhere" ;
    nb:placeSlug "no-identity" ;
    schema:url <https://example.org> .
"""
        )
        assert load_place_facts(tmp_path) == []


class TestBuildProposals:
    def test_add_only_missing_properties(self, places_dir, curation):
        facts = load_place_facts(places_dir)
        live = {
            "Q977642": {"P856": {"https://kagyu.org"}},  # website already there
            "Q6372782": {},
        }
        proposals, _skipped = build_proposals(facts, live, curation)
        by_pid = {(p.qid, p.pid): p for p in proposals}
        assert ("Q977642", "P856") not in by_pid  # never modify existing
        assert ("Q977642", "P6375") in by_pid
        assert ("Q977642", "P281") in by_pid
        assert ("Q6372782", "P856") in by_pid

    def test_coordinates_only_when_venue_level(self, places_dir, curation):
        facts = load_place_facts(places_dir)
        live = {"Q977642": {}, "Q6372782": {}}
        proposals, skipped = build_proposals(facts, live, curation)
        coords = [p for p in proposals if p.pid == "P625"]
        assert coords == []
        skipped_kcl = [s for s in skipped if s.slug == "karme-choling"]
        assert any("municipality" in s.reason for s in skipped_kcl)

    def test_curation_only_no_circular_enrichment(self, places_dir):
        facts = load_place_facts(places_dir)
        curation = {}  # nothing curated: everything came from Wikidata
        live = {"Q977642": {}, "Q6372782": {}}
        proposals, _ = build_proposals(facts, live, curation)
        assert proposals == []

    def test_unreferenced_facts_are_flagged(self, places_dir, curation):
        (places_dir / "no-website.ttl").write_text(
            """\
@prefix nb: <https://data.nalandabodhi.org/ontology/> .
@prefix schema: <https://schema.org/> .

<https://data.nalandabodhi.org/place/no-website> a schema:Place ;
    schema:name "No Website Center" ;
    nb:placeSlug "no-website" ;
    schema:address [ a schema:PostalAddress ;
        schema:streetAddress "1 Example Street" ;
    ] ;
    schema:sameAs <http://www.wikidata.org/entity/Q1> .
"""
        )
        curation["no-website"] = {"street_address": "1 Example Street"}
        facts = load_place_facts(places_dir)
        live = {"Q977642": {}, "Q6372782": {}, "Q1": {}}
        proposals, _ = build_proposals(facts, live, curation)
        street = next(p for p in proposals if p.slug == "no-website")
        assert street.submittable is False
        assert "no public reference" in street.note


class TestRendering:
    def test_quickstatements_v1_syntax(self, places_dir, curation):
        facts = load_place_facts(places_dir)
        live = {"Q977642": {}, "Q6372782": {}}
        proposals, _ = build_proposals(facts, live, curation)
        text = render_quickstatements(proposals)
        lines = [
            line
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert all(line.startswith("Q") for line in lines)
        website = next(l for l in lines if "P856" in l and "kagyu.org" in l)
        assert website == ('Q977642|P856|"https://kagyu.org"|S854|"https://kagyu.org"')
        street = next(l for l in lines if "P6375" in l and "Meads" in l)
        assert '"435 Meads Mountain Road"@en' in street
        postal = next(l for l in lines if "P281" in l and "12409" in l)
        assert '"12409"' in postal

    def test_report_lists_proposals_and_skips(self, places_dir, curation):
        facts = load_place_facts(places_dir)
        live = {"Q977642": {"P856": {"https://kagyu.org"}}, "Q6372782": {}}
        proposals, skipped = build_proposals(facts, live, curation)
        report = render_report(proposals, skipped)
        assert "Q977642" in report
        assert "karma-triyana-dharmachakra" in report
        assert "municipality" in report  # skip reason documented
