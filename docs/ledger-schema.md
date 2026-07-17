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

## Migration 2: command inbox, transactional outbox, process-manager state, node leases

```sql
CREATE TABLE command_inbox (
            command_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            command_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'rejected')),
            processed_at TEXT
        )
```

```sql
CREATE TABLE outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            correlation_id TEXT NOT NULL,
            target TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            dispatched_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'failed'))
        )
```

```sql
CREATE INDEX outbox_status_idx ON outbox (status)
```

```sql
CREATE TABLE process_manager_state (
            process_manager_name TEXT NOT NULL,
            state_key TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (process_manager_name, state_key)
        )
```

```sql
CREATE TABLE node_leases (
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            fencing_token INTEGER NOT NULL,
            owner TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            expires_at TEXT,
            PRIMARY KEY (run_id, node_id)
        )
```
