"""The runtime database role must be able to DML and unable to DDL.

A leaked application credential must not be able to drop or alter tables. The
compose stack and CI create ``support_app`` (DML-only) and ``support_migrator``
(schema owner); Alembic runs as the migrator.

Skipped when the configured URL points at a superuser (plain local dev), so the
test is meaningful in the environments that actually implement the split.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from support_agent.db.session import dispose_engine, get_engine

pytestmark = pytest.mark.integration


async def test_application_role_cannot_run_ddl() -> None:
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            current_role = await conn.scalar(text("select current_user"))
            is_superuser = await conn.scalar(
                text("select rolsuper from pg_roles where rolname = current_user")
            )
        if is_superuser:
            pytest.skip(f"{current_role} is a superuser: role split not configured here")

        async with engine.connect() as conn:
            with pytest.raises(ProgrammingError):
                await conn.execute(text("create table ddl_probe (id integer)"))
            await conn.rollback()

        # DML on the owned-by-migrator tables is still allowed.
        async with engine.connect() as conn:
            count = await conn.scalar(text("select count(*) from documents"))
        assert count is not None and count >= 0
    finally:
        await dispose_engine()
