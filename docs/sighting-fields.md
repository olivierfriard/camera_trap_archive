# Optional sighting fields

Run the additive, idempotent migration before restarting Flask:

```bash
.venv/bin/python scripts/migrate_sighting_fields.py
```

The migration adds nullable `sighting.pup_number INTEGER` (nonnegative) and
`sighting.anomaly TEXT`. Existing sightings retain unknown values (`NULL`). No
existing column, identifier, ownership rule or media job is changed. These fields
are displayed but not filtered or sorted, so no additional index is needed.

Photo/video creation and owner/admin editing use the same fields:
**Numero di cuccioli** accepts an optional integer from zero to 2147483647, the
PostgreSQL INTEGER limit. Empty means unknown, distinct from zero. **Anomalia**
is optional free text; whitespace-only input is stored as `NULL`. The backend
validates pup counts before writing, preserves drafts on errors, and displays
anomaly descriptions as escaped text in sighting details and lists.

Deploy the migration and application together. New code requires both columns.
