from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa

revision = '0002_enable_postgis'
down_revision = '0001_baseline_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS postgis'))


def downgrade() -> None:
    bind = op.get_bind()
    # Only drop the extension when nothing else depends on it. Rebuilding Postgres's
    # dependency walk from the catalogs is fragile (a geometry column depends on the
    # extension's member *types*, not on the extension row), so let Postgres itself
    # decide inside a savepoint: a refused DROP rolls back only the savepoint and the
    # extension (plus its dependents) stay in place.
    try:
        with bind.begin_nested():
            bind.execute(sa.text('DROP EXTENSION IF EXISTS postgis'))
    except Exception as exc:
        orig = getattr(exc, 'orig', None)
        if orig is not None and getattr(orig, 'sqlstate', None) == '2BP01':  # dependent_objects_still_exist
            logging.getLogger('alembic.runtime.migration').warning(
                'Not dropping the postgis extension: other objects still depend on it.')
        else:
            raise