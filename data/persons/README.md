# Persons

Teacher records (persons), one Turtle file per person. Planned; no
source export has been imported yet.

Conventions (see the repo README for the full pattern):

- File name: `<slug>.ttl`, where `<slug>` is the immutable kebab-case
  slug of the person; the IRI is
  `https://data.nalandabodhi.org/person/<slug>`.
- Type: `a schema:Person` with `schema:name`, `schema:sameAs` (the
  Wikidata item), and role terms such as `schema:jobTitle` or
  `schema:hasOccupation` where appropriate.
- Link persons to places through events, not directly:
  `schema:event` on a place (or a dedicated events file) with
  `schema:performer` pointing at the person and `schema:location`
  pointing at the place.

Add the import pipeline (`persons_import.py`, following
`places_import.py`) together with the first source export; do not
hand-write person files.
