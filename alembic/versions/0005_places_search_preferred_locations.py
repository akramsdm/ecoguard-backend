"""Step 6: gazetteer name search + the public near-cases surface.

Additive only. Two changes:

* ``places`` gets the same name-search indexes ``areas_osm`` already has
  (trigram GIN on ``name``, plain GIN on the ``alt_names`` text[]), so the
  public place-name search can run over both tables in one query. The table
  itself was created empty by 0003; the OSM importer now fills it from the
  same local extract (no live geocoding).
* ``public_preferred_locations`` stores the *coarsened* point a device opts in
  to save for future visits. It is keyed by a client-generated id (there is no
  anonymous account), and the API only ever writes ``spatial.grid_point``
  values into it — never a precise fix.

Like 0003/0004 these tables live outside ``Base.metadata`` deliberately (the
SQLite test harness cannot create PostGIS geometry DDL in 0003, and this table
is only ever touched by the PostGIS-backed public router).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# NOTE: `revision` must fit alembic_version.version_num (varchar(32)) on
# Postgres; keep ids short even when the filename is more descriptive.
revision = '0005_public_nearby'
down_revision = '0004_spatial_authorization'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Place-name search: same index pair areas_osm uses for its name/alias lookups.
    op.create_index('ix_places_name_trgm', 'places', ['name'],
                    postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('ix_places_alt_names_gin', 'places', ['alt_names'],
                    postgresql_using='gin')

    # Anonymous opt-in "saved location for next visit". Only coarsened values
    # are written; updated_at is stamped by DO UPDATE in the API (no trigger).
    op.create_table(
        'public_preferred_locations',
        sa.Column('client_id', sa.String(64), primary_key=True),
        sa.Column('latitude', sa.Float(), nullable=False),
        sa.Column('longitude', sa.Float(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('public_preferred_locations')
    op.drop_index('ix_places_alt_names_gin', table_name='places')
    op.drop_index('ix_places_name_trgm', table_name='places')