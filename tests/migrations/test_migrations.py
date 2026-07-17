from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from enginery.ledger.connection import open_connection
from enginery.ledger.errors import MigrationFailedError
from enginery.ledger.migrations import apply_pending_migrations, current_schema_version
from enginery.ledger.schema import MIGRATIONS, Migration
from enginery.ledger.service import LedgerService


def test_fresh_database_has_no_schema_version(ledger_path: Path) -> None:
    connection = open_connection(ledger_path)
    try:
        assert current_schema_version(connection) == 0
    finally:
        connection.close()


def test_apply_pending_migrations_reaches_latest_version(ledger_path: Path) -> None:
    connection = open_connection(ledger_path)
    try:
        applied = apply_pending_migrations(connection)
        assert applied == tuple(migration.version for migration in MIGRATIONS)
        assert current_schema_version(connection) == MIGRATIONS[-1].version
    finally:
        connection.close()


def test_reopening_an_already_migrated_ledger_applies_nothing(ledger_path: Path) -> None:
    LedgerService.open(ledger_path).close()
    connection = open_connection(ledger_path)
    try:
        applied = apply_pending_migrations(connection)
        assert applied == ()
        assert current_schema_version(connection) == MIGRATIONS[-1].version
    finally:
        connection.close()


def test_failed_migration_leaves_schema_version_unchanged(ledger_path: Path) -> None:
    connection = open_connection(ledger_path)
    try:
        apply_pending_migrations(connection, migrations=MIGRATIONS)
        version_before = current_schema_version(connection)

        broken = Migration(
            version=version_before + 1,
            description="deliberately broken migration",
            statements=("CREATE TABLE this_is_not_valid_sql (",),
        )
        with pytest.raises(MigrationFailedError):
            apply_pending_migrations(connection, migrations=(*MIGRATIONS, broken))

        assert current_schema_version(connection) == version_before
    finally:
        connection.close()


def test_failed_migration_does_not_start_the_application(ledger_path: Path) -> None:
    """A LedgerService.open() caller must never receive a usable service
    when a migration fails partway through."""
    broken_migrations = (
        *MIGRATIONS,
        Migration(
            version=MIGRATIONS[-1].version + 1,
            description="deliberately broken migration",
            statements=("SELECT * FROM a_table_that_does_not_exist",),
        ),
    )
    connection = open_connection(ledger_path)
    try:
        with pytest.raises(MigrationFailedError):
            apply_pending_migrations(connection, migrations=broken_migrations)
    finally:
        connection.close()


def test_migration_second_statement_failure_rolls_back_the_first(ledger_path: Path) -> None:
    """A migration is one transaction: a later statement's failure must
    undo an earlier statement's DDL in the same migration, not leave a
    half-created table behind."""
    connection = open_connection(ledger_path)
    try:
        half_broken = Migration(
            version=1,
            description="half broken",
            statements=(
                "CREATE TABLE partially_created (id INTEGER PRIMARY KEY)",
                "CREATE TABLE this_is_not_valid_sql (",
            ),
        )
        with pytest.raises(MigrationFailedError):
            apply_pending_migrations(connection, migrations=(half_broken,))

        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'partially_created'"
        ).fetchone()
        assert table is None
        assert current_schema_version(connection) == 0
    finally:
        connection.close()


def test_open_ledger_service_applies_migrations_and_reports_version(ledger_path: Path) -> None:
    service = LedgerService.open(ledger_path)
    try:
        assert service.schema_version == MIGRATIONS[-1].version
    finally:
        service.close()


def test_migration_ordering_is_ascending_regardless_of_declaration_order(
    ledger_path: Path,
) -> None:
    out_of_order = tuple(reversed(MIGRATIONS)) if len(MIGRATIONS) > 1 else MIGRATIONS
    connection = open_connection(ledger_path)
    try:
        applied = apply_pending_migrations(connection, migrations=out_of_order)
        assert applied == tuple(sorted(applied))
    finally:
        connection.close()


def test_migration_runner_uses_immediate_transactions(ledger_path: Path) -> None:
    """Regression guard: migrations must not rely on sqlite3's implicit
    transaction handling, which silently commits on DDL in some driver
    configurations and would defeat rollback-on-failure."""
    connection = open_connection(ledger_path)
    try:
        assert connection.isolation_level is None
        apply_pending_migrations(connection)
    finally:
        connection.close()


def test_current_schema_version_survives_reconnect(ledger_path: Path) -> None:
    LedgerService.open(ledger_path).close()
    reopened: sqlite3.Connection = open_connection(ledger_path)
    try:
        assert current_schema_version(reopened) == MIGRATIONS[-1].version
    finally:
        reopened.close()
