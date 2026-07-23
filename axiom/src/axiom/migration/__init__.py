"""Explicit data migrations from the frozen Cairn database."""

from axiom.migration.legacy import MigrationReport, migrate_legacy_database

__all__ = ["MigrationReport", "migrate_legacy_database"]
