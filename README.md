# master-library-lod

Linked open data for the Nalandabodhi Master Library: the public,
data-only companion to the [master-library](../master-library)
application repository. It publishes the reference entities of the
teaching-video catalog as Turtle (.ttl) files — places (monasteries,
retreat centers, dharma centers) today, persons (teachers) next — so
they can be consumed by any RDF tooling, linked against Wikidata, and
validated in CI.

## Layout

```text
data/
  places/            one .ttl per published place (see "Regenerating")
  persons/           teachers (planned; see data/persons/README.md)
  import/            raw sources and lookup caches for the generators
vocabulary/
  master-library.ttl local nb: registry terms used by the data files
src/master_library_lod/
  places_import.py   eml -> Wikidata lookup -> Turtle generator
tests/               offline tests, including validation of all .ttl files
.github/workflows/   CI: syntax-validates every Turtle file on push
```

## Modeling

schema.org carries the descriptive profile; the BBC Programmes
ontology (`po:Place`) adds the catalog alignment used by the
master-library application. The local registry term `nb:placeSlug`
is defined in `vocabulary/master-library.ttl`. Internal Airtable
fields (location id, venue code, short name) stay in the import layer
and are not published as triples.

A resolved place carries the minimal set: postal address (with the
country as an ISO 3166-1 alpha-2 code), geo-coordinates, and website.
Note that schema.org has no `Monastery` type; places are typed
`schema:Place` (plus `po:Place`) and may also be typed
`schema:ReligiousOrganization` where that is accurate.

```turtle
<https://data.nalandabodhi.org/place/karma-triyana-dharmachakra>
  a po:Place, schema:Place ;
  schema:name "Karma Triyana Dharmachakra" ;
  nb:placeSlug "karma-triyana-dharmachakra" ;
  schema:address [
    a schema:PostalAddress ;
    schema:streetAddress "435 Meads Mountain Road" ;
    schema:addressLocality "Woodstock" ;
    schema:addressRegion "NY" ;
    schema:postalCode "12409" ;
    schema:addressCountry "US" ;
  ] ;
  schema:geo [
    a schema:GeoCoordinates ;
    schema:latitude 42.070833 ;
    schema:longitude -74.123056 ;
  ] ;
  schema:url <https://kagyu.org> ;
  schema:sameAs <http://www.wikidata.org/entity/Q977642> .
```

IRIs are minted under `https://data.nalandabodhi.org/` with the file
stem as the immutable slug: a place's IRI is
`https://data.nalandabodhi.org/place/<slug>`. Standing up that host
with content negotiation is a planned infrastructure task; until then
the IRIs are stable but do not resolve.

### Linking places and persons

When teacher data lands in `data/persons/`, a person is a
`schema:Person` and connects to places through events:

```turtle
<https://data.nalandabodhi.org/person/dilgo-khyentse-rinpoche>
  a schema:Person ;
  schema:name "Dilgo Khyentse Rinpoche" ;
  schema:sameAs <http://www.wikidata.org/entity/Q353743> .

# in a place file, or a dedicated events file:
<https://data.nalandabodhi.org/place/karma-triyana-dharmachakra>
  schema:event [
    a schema:Event ;
    schema:name "Winter Teaching 1990" ;
    schema:performer <https://data.nalandabodhi.org/person/dilgo-khyentse-rinpoche> ;
  ] .
```

`schema:performer` links a teacher to an event at a place;
`schema:location` links an event to its place; `schema:provider` or
`schema:employee` can express a teacher's standing relationship to a
center. Keep events as subjects when both teacher and place are known,
so the triple stays a statement about the event.

## Regenerating the data

Place files are generated, never hand-edited:

```bash
# network: query Wikidata SPARQL, write data/import/places-enrichment.json
uv run python -m master_library_lod.places_import lookup

# offline: render Turtle from the eml + enrichment + curation
uv run python -m master_library_lod.places_import write
```

Sources, in order of application. **None of the three import-layer
files is distributed in this repository**: they contain non-public
records (private homes, student groups), so they are supplied
manually when regenerating, and the test suite runs against a
synthetic fixture (`tests/fixtures/mini-places.eml`). Only the 175
records that name a real, findable venue are published; private
homes, student-group records (`ktgr-*`), and placeholders
(`place-l*`, orphan recordings) stay in the import layer.

1. `data/import/places.eml` — the Airtable Location table export
   (200 records, authoritative for registry fields and slugs).
2. `data/import/places-enrichment.json` — Wikidata lookup results
   (batched SPARQL, same-country strict, city fallback for unknown
   venues). Regenerable with `lookup` once the eml is supplied.
3. `data/import/places-curation.json` — reviewed manual overrides
   (verified addresses, corrected city matches, dropped wrong identity
   claims). Curation wins; unknown slugs raise an error. The curation
   layer is also the source for Wikidata contribution proposals.

To fix or enrich a place, edit the curation file and re-run `write`.

## Validation

Every Turtle file in `data/` and `vocabulary/` is parsed by the test
suite, which CI runs on every push:

```bash
uv run pytest
```

## Contributing back to Wikidata

This repo consumes Wikidata for enrichment; the curation layer also
holds facts Wikidata lacks (websites, street addresses, postal codes
for dharma centers). The
[Wikidata contribution workflow](docs/wikidata-contribution.md) turns
that curation into reviewed QuickStatements batches:

```bash
uv run python -m master_library_lod.wikidata_contribution propose
```

The command writes `data/wikidata/proposals.qs` (commands) and
`proposals.md` (review report) after diffing the curation against
live Wikidata claims. Submission is always a manual, human-reviewed
QuickStatements import; the tooling never writes to Wikidata.

## Status

- 175 published place files (of 200 source records); 163 with
  coordinates; 17 with websites; 16 with
  Wikidata identities. Gaps are `TODO` comments in the files.
- Persons data is planned; no source export exists yet.
