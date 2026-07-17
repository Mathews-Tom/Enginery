# Ledger schema

Generated from `src/enginery/ledger/schema.py` by `scripts/generate_ledger_schema_doc.py`. Do not edit by hand — regenerate after changing `MIGRATIONS`.

Migrations are forward-only. A schema mistake is corrected by a new migration appended to the list, never by editing an already-applied one.

## Migration 1: ledger core: schema_migrations, aggregates, events

```sql
CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
```

```sql
CREATE TABLE aggregates (
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (aggregate_type, aggregate_id)
        )
```

```sql
CREATE TABLE events (
            commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            UNIQUE (aggregate_type, aggregate_id, aggregate_version)
        )
```

```sql
CREATE INDEX events_correlation_idx ON events (correlation_id)
```

```sql
CREATE INDEX events_aggregate_idx ON events (aggregate_type, aggregate_id)
```
