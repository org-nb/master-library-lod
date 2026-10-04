# Repo bootstrap: places LOD from the master-library backtrack

Date: 2026-10-04

## Context

The place registry was first built inside the master-library
application repo as a Vault-LD vault (`data/vault/`, never committed).
That was unset: public reference data belongs in its own data-only
repository. This repo is the result. The backtrack is recorded in
master-library's `.agents/memory/202610041546_backtrack-to-master-library-lod.md`.

## What was moved and changed

- `data/import/places.eml` (Airtable Location export, 200 records),
  `places-enrichment.json` (Wikidata lookup cache),
  `places-curation.json` (reviewed overrides): moved unchanged.
- Import pipeline moved from `master_library.places_import` to
  `master_library_lod.places_import`; rendering switched from
  Vault-LD Markdown to one Turtle file per place under `data/places/`.
  With Turtle, the earlier flat-profile constraint (oxivault does not
  expand nested YAML-LD) no longer applies, so addresses are now
  proper `schema:PostalAddress` nodes and coordinates proper
  `schema:GeoCoordinates` nodes. WGS84 terms dropped in favor of
  schema.org.
- `po:Place` typing retained alongside `schema:Place` for continuity
  with the master-library catalog semantics.
- Registry fields keep the `nb:` namespace
  (`https://data.nalandabodhi.org/ontology/`), now defined in
  `vocabulary/master-library.ttl` instead of vault ontology notes.
- Place IRIs: `https://data.nalandabodhi.org/place/<slug>`; slug equals
  file stem and is immutable once frozen.

## Data status (unchanged from the enrichment work)

200 places; 174 with coordinates; 17 with websites; 16 with Wikidata
identities; 7 with street addresses. Gaps are TODO comments in the
Turtle files; fix via `data/import/places-curation.json` and re-run
`uv run python -m master_library_lod.places_import write`.

## Gates

24 tests (parse, slugs, render, curation, and validation of all 200
Turtle files plus the vocabulary), ruff, ruff format, ty, uv audit:
all green. CI workflow `.github/workflows/validate.yml` runs the same
on push.

## Left to do

- Create the GitHub repository (local only so far; not committed).
- Persons (teachers): awaiting a source export; conventions documented
  in `data/persons/README.md` and the repo README.
- Open decision in master-library: how ADR-0003's vault consumes this
  registry (rdf2vault ingest vs ADR amendment).
- Namespace `https://data.nalandabodhi.org/` needs DNS/content
  negotiation eventually so IRIs resolve.
