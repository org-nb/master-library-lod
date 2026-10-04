"""Tests for the places import pipeline.

GIVEN the synthetic Airtable Location fixture in tests/fixtures
WHEN the pipeline parses, slugs, and renders Turtle
THEN the linked-open-data place files are deterministic and
offline-testable without committing the real export.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import rdflib

from master_library_lod.places_import import (
    PLACES_DIR,
    EmlParseError,
    PlacesImportError,
    apply_curation,
    assign_slugs,
    kebab,
    lookup_category,
    parse_places_eml,
    render_place_turtle,
    write_place_files,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_EML = REPO_ROOT / "tests" / "fixtures" / "mini-places.eml"
SCHEMA = rdflib.Namespace("https://schema.org/")
NB = rdflib.Namespace("https://data.nalandabodhi.org/ontology/")
PO = rdflib.Namespace("http://purl.org/ontology/po/")
PLACE = rdflib.Namespace("https://data.nalandabodhi.org/place/")


@pytest.fixture(scope="module")
def records():
    return parse_places_eml(FIXTURE_EML)


@pytest.fixture(scope="module")
def slugs(records):
    return assign_slugs(records)


class TestParsePlacesEml:
    def test_parses_all_records(self, records):
        assert len(records) == 8

    def test_decodes_diacritics(self, records):
        karme = next(r for r in records if r.location_id == "4")
        assert karme.venue == "Karmê Chöling"
        assert karme.municipality == "Barnet"

    def test_keeps_record_fields(self, records):
        ktd = next(r for r in records if r.location_id == "1")
        assert ktd.venue == "Karma Triyana Dharmachakra"
        assert ktd.venue_code == "KTD"
        assert ktd.country_code == "US"
        assert ktd.short_name == "KTD Woodstock, US"

    def test_record_without_location_id(self, records):
        opr = records[7]
        assert opr.location_id == ""
        assert opr.venue == "Orphan Partial Recordings"

    def test_rejects_file_without_html_body(self, tmp_path):
        eml = tmp_path / "plain.eml"
        eml.write_text("Subject: no html\n\nplain text\n")
        with pytest.raises(EmlParseError):
            parse_places_eml(eml)


class TestKebab:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Karma Triyana Dharmachakra", "karma-triyana-dharmachakra"),
            ("Karmê Chöling", "karme-choling"),
            ("Saint-Léon sur Vézère", "saint-leon-sur-vezere"),
            ("KTGR students’ teachings", "ktgr-students-teachings"),
            ("Cha'an Center", "chaan-center"),
        ],
    )
    def test_slugify(self, value, expected):
        assert kebab(value) == expected


class TestAssignSlugs:
    def test_slugs_are_unique(self, records, slugs):
        assert len(set(slugs.values())) == len(records)

    def test_unique_venue_gets_plain_slug(self, slugs):
        assert slugs["1"] == "karma-triyana-dharmachakra"

    def test_ambiguous_venue_gets_municipality_suffix(self, records, slugs):
        same_venue = [r for r in records if r.venue == "Karma Thegsum Choling"]
        assert slugs["2"] == "karma-thegsum-choling-los-angeles"
        assert slugs["3"] == "karma-thegsum-choling-hartford"
        assert len(same_venue) == 2

    def test_placeholder_venue_gets_opaque_slug(self, slugs):
        assert slugs["7"] == "place-l7"
        assert slugs[""] == "orphan-partial-recordings"


class TestLookupCategory:
    def test_categories(self, records):
        by_id = {r.location_id: r for r in records}
        assert lookup_category(by_id["1"]) == "venue"
        assert lookup_category(by_id["5"]) == "municipality"
        assert lookup_category(by_id["6"]) == "municipality"
        assert lookup_category(by_id["7"]) == "none"
        assert lookup_category(by_id[""]) == "none"


class TestApplyCuration:
    def test_overrides_fields_and_clears(self):
        enrichment: dict[str, dict[str, object]] = {
            "ktd": {
                "source": "venue",
                "wikidata": "http://www.wikidata.org/entity/Q1",
                "website": None,
            }
        }
        curated = apply_curation(
            enrichment,
            {
                "ktd": {
                    "_clear": ["wikidata"],
                    "website": "https://example.org",
                    "latitude": 42.0,
                }
            },
        )
        assert curated["ktd"]["website"] == "https://example.org"
        assert curated["ktd"]["latitude"] == 42.0
        assert "wikidata" not in curated["ktd"]
        assert (
            enrichment["ktd"]["wikidata"] == "http://www.wikidata.org/entity/Q1"
        )  # input is not mutated

    def test_unknown_slug_raises(self):
        with pytest.raises(PlacesImportError):
            apply_curation({}, {"nope": {"website": "https://example.org"}})


class TestRenderPlaceTurtle:
    def test_resolved_place(self, records, slugs):
        record = next(r for r in records if r.location_id == "1")
        ttl = render_place_turtle(
            record,
            slugs["1"],
            {
                "source": "venue",
                "wikidata": "http://www.wikidata.org/entity/Q977642",
                "latitude": 42.070833,
                "longitude": -74.123056,
                "street_address": "435 Meads Mountain Road",
                "address_region": "NY",
                "postal_code": "12409",
                "website": "https://kagyu.org",
            },
        )
        graph = rdflib.Graph().parse(data=ttl, format="turtle")
        place = PLACE["karma-triyana-dharmachakra"]
        assert (place, rdflib.RDF.type, PO.Place) in graph
        assert (place, rdflib.RDF.type, SCHEMA.Place) in graph
        assert (
            place,
            SCHEMA.name,
            rdflib.Literal("Karma Triyana Dharmachakra"),
        ) in graph
        assert (
            place,
            NB.placeSlug,
            rdflib.Literal("karma-triyana-dharmachakra"),
        ) in graph
        assert (place, SCHEMA.url, rdflib.URIRef("https://kagyu.org")) in graph
        assert (
            place,
            SCHEMA.sameAs,
            rdflib.URIRef("http://www.wikidata.org/entity/Q977642"),
        ) in graph
        address = graph.value(place, SCHEMA.address)
        assert address is not None
        assert (
            address,
            SCHEMA.streetAddress,
            rdflib.Literal("435 Meads Mountain Road"),
        ) in graph
        assert (address, SCHEMA.addressCountry, rdflib.Literal("US")) in graph
        geo = graph.value(place, SCHEMA.geo)
        assert geo is not None
        latitude = graph.value(geo, SCHEMA.latitude)
        assert float(str(latitude)) == pytest.approx(42.070833)
        longitude = graph.value(geo, SCHEMA.longitude)
        assert float(str(longitude)) == pytest.approx(-74.123056)

    def test_unresolved_place_flags_todo(self, records, slugs):
        record = next(r for r in records if r.location_id == "1")
        ttl = render_place_turtle(record, slugs["1"], None)
        assert "TODO: unresolved place properties" in ttl
        assert "schema:geo" not in ttl

    def test_placeholder_venue_has_no_descriptive_fields(self, records, slugs):
        record = next(r for r in records if r.location_id == "7")
        ttl = render_place_turtle(record, slugs["7"], None)
        assert "TODO" not in ttl
        assert "schema:geo" not in ttl
        assert "schema:url" not in ttl
        graph = rdflib.Graph().parse(data=ttl, format="turtle")
        assert graph.value(PLACE["place-l7"], SCHEMA.addressCountry) is None


class TestWritePlaceFiles:
    def test_writes_one_file_per_published_venue(self, records, slugs, tmp_path):
        written = write_place_files(records, slugs, {}, tmp_path)
        assert len(written) == 4
        assert (tmp_path / "karma-triyana-dharmachakra.ttl").exists()
        rdflib.Graph().parse(
            tmp_path / "karma-triyana-dharmachakra.ttl", format="turtle"
        )

    def test_unpublished_records_get_no_files(self, records, slugs, tmp_path):
        write_place_files(records, slugs, {}, tmp_path)
        for slug in (
            "ktgr-students",
            "private-home",
            "place-l7",
            "orphan-partial-recordings",
        ):
            assert not (tmp_path / f"{slug}.ttl").exists()

    def test_prunes_stale_files(self, records, slugs, tmp_path):
        stale = tmp_path / "ktgr-video-clips.ttl"
        stale.write_text("@prefix schema: <https://schema.org/> .\n")
        write_place_files(records, slugs, {}, tmp_path)
        assert not stale.exists()


class TestCommittedData:
    """Validate the checked-in linked-open-data files."""

    def test_all_place_files_parse(self):
        files = sorted(PLACES_DIR.glob("*.ttl"))
        assert len(files) == 175
        for path in files:
            graph = rdflib.Graph().parse(path, format="turtle")
            subjects = list(graph.subjects(rdflib.RDF.type, SCHEMA.Place))
            assert len(subjects) == 1, path

    def test_place_slugs_match_file_names(self):
        for path in PLACES_DIR.glob("*.ttl"):
            graph = rdflib.Graph().parse(path, format="turtle")
            slugs = list(graph.objects(None, NB.placeSlug))
            assert len(slugs) == 1
            assert str(slugs[0]) == path.stem

    def test_no_unpublished_records_in_data(self):
        stems = {path.stem for path in PLACES_DIR.glob("*.ttl")}
        assert not any(
            stem.startswith(("private-home-", "place-l", "ktgr-"))
            or stem == "orphan-partial-recordings"
            for stem in stems
        )

    def test_vocabulary_parses(self):
        graph = rdflib.Graph().parse(REPO_ROOT / "vocabulary" / "master-library.ttl")
        assert len(graph) > 0
