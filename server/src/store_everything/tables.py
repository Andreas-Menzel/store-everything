"""Table definitions and the single SQLAlchemy metadata they register on.

There are no ORM model classes by design (ADR-0012): tables are declared here as Core
`Table` objects, and rows map to Pydantic models at the API boundary (`schemas.py`).

Fixing the naming convention *before* the first table exists is deliberate: constraint
and index names become part of migrations the moment a table is created, and renaming
them later is a migration of its own.
"""

from __future__ import annotations

from sqlalchemy import MetaData

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)
