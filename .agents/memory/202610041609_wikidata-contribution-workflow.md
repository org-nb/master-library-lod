# Wikidata contribution workflow implemented

Date: 2026-10-04

## What was built

`master_library_lod.wikidata_contribution` with a `propose` command
plus `docs/wikidata-contribution.md` defining the five-stage workflow
(propose -> review -> submit -> log -> verify).

First live run: 15 submittable proposals across 7 items (KTD,
Karmê Chöling, Gampo Abbey, Dechen Chöling, Harvard Divinity School,
Kagyu Samye Ling, Shambhala/Drala Mountain Center), 8 skipped with
documented reasons. Output committed under `data/wikidata/`
(`proposals.qs`, `proposals.md`, `log.md` template).

## Safeguards encoded

- Add-only: existing claims never modified (skipped and reported).
- Non-circular: only curation-layer facts are proposable.
- Venue-level geo only: municipality-level coordinates never proposed.
- Referenced: every statement carries S854 (default: the place's own
  website); unreferenced facts reported as not submittable.
- No writes to Wikidata anywhere; submission is a manual
  QuickStatements import under the contributor's account.

## Reviewer notes for the first batch

- KSL street address "Eskdalemuir, Langholm" is flagged (no house
  number); likely drop it and keep only the postal code.
- Gampo Abbey reference is the http variant of its site (that is what
  Wikidata's existing P856 holds); consider upgrading the curation
  website to https first.
- New item creation (most centers lack Wikidata entries) is
  deliberately out of scope; case-by-case by a human.

## Left to do

- Human: submit the trimmed batch via QuickStatements, append to
  `data/wikidata/log.md`, re-run `propose` to verify.
- Consider a GitHub Action reminder or scheduled `propose` run later;
  not needed while batches are rare.

## Update (same day, follow-up request)

Removed `nb:locationId`, `nb:venueCode`, and `nb:shortName` from the
published data: dropped from the Turtle renderer, the vocabulary, and
all 200 regenerated place files. Those Airtable fields remain in the
import layer (parse/slug logic and the curation cache still key on
them internally) but are no longer published as triples. `nb:placeSlug`
stays as the only local registry term.

## Update 2 (same day, follow-up request)

The publish filter now keeps only records that name a real, findable
venue (`lookup_category == "venue"`): 175 files. Removed 25: all
`private-home-*` (privacy), `ktgr-*` (student groups, not places),
`place-l*` and `orphan-partial-recordings` (placeholders). The write
step prunes stale files, so regeneration cannot reintroduce them.
Those records remain in the eml, enrichment, and curation caches
(import layer); the Wikidata proposal batch is unaffected (it only
reads published venues with a sameAs).
