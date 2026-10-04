# Contributing to Wikidata

This repo consumes Wikidata (place enrichment) and, through the
curation layer, also holds facts Wikidata lacks: official websites,
street addresses, and postal codes for dharma centers. Giving those
back is good linked-data citizenship. This page defines the workflow.

## Principles

- **Human-in-the-loop.** Nothing in this repo writes to Wikidata. The
  tooling only prepares proposals; submission is a manual
  QuickStatements import under your own named account, and you are
  accountable for every edit.
- **Add-only.** If an item already has a value for a property, we
  never modify or replace it. Replacing existing claims needs
  Wikidata community discussion, not a script.
- **Non-circular.** Only curation-layer facts are proposable
  (`data/import/places-curation.json`). Enrichment facts came *from*
  Wikidata; proposing them back would launder our own imports.
- **Venue-level geo only.** Municipality-level coordinates (town
  centers for unidentified venues) are never proposed; they would
  wrongly pin a center to its town.
- **Referenced.** Every proposed statement carries a reference
  (`S854`, reference URL), by default the place's own website. Facts
  without a public reference are reported but marked not submittable.
- **New items are out of scope** for this pipeline. Creating items
  (e.g. for the many centers without a Wikidata entry) is done
  case-by-case by a human who judges notability and finds independent
  references; do not batch-create.

## Workflow

### 1. Propose (automated)

```bash
uv run python -m master_library_lod.wikidata_contribution propose
```

Reads the place files and the curation layer, fetches the live claims
of all linked items (one SPARQL query), and writes to
`data/wikidata/`:

- `proposals.qs` — QuickStatements v1 commands, one statement per
  line, each with its reference.
- `proposals.md` — the review report: every proposal with provenance
  and notes, plus everything that was skipped and why.

### 2. Review (human)

Read `proposals.md`. Delete any command from `proposals.qs` you are
not prepared to defend — in particular:

- statements flagged as not submittable (never submit those),
- street addresses whose value has no house number (the tool flags
  them; Wikidata's P6375 wants a street address, not a locality),
- anything where the reference does not actually support the value
  (open the website and check).

Commit the trimmed batch if you want a record of what you submitted.

### 3. Submit (manual)

1. Log in to Wikidata and open
   <https://quickstatements.toolforge.org/#/batch>.
2. Paste the contents of `proposals.qs` (v1 commands) and step
   through the preview carefully.
3. Run the batch. Fix or revert anything the preview or edit summary
   flags.

### 4. Log (human)

Append to `data/wikidata/log.md`: date, the batch (paste the
QuickStatements batch URL), what was submitted, and any items that
failed or were rejected.

### 5. Verify (automated)

Re-run `propose`. Statements that were accepted disappear from the
diff (the live claims now hold them); leftovers stay in the report.
The next `places_import lookup` will also start returning the new
facts, which closes the loop.

## Properties covered

| Property | Meaning | From our data |
| --- | --- | --- |
| P856 | official website | `schema:url` (curated only) |
| P6375 | street address | `schema:streetAddress` (curated only) |
| P281 | postal code | `schema:postalCode` (curated only) |
| P625 | coordinates | only when curation carries venue-level geo |

P131 (located in the administrative unit) is deliberately not
proposed: it needs a region item QID, not a string.
