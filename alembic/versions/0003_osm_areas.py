"""Add OSM-sourced geographic areas: areas_osm, areas_osm_aliases, places.

Additive only — nothing in 0001_baseline_schema or 0002_enable_postgis is touched.
The OSM tables live outside ``Base.metadata`` (the app's SQLite test harness cannot
create PostGIS geometry DDL), so they are managed exclusively by these hand-written
migrations; do not let ``alembic revision --autogenerate`` regenerate them.
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003_osm_areas'
down_revision = '0002_enable_postgis'
branch_labels = None
depends_on = None


class _Geom(sa.types.UserDefinedType):
    """Render exact PostGIS DDL (``geometry(MultiPolygon,4326)``) in migrations
    without pulling geoalchemy2 into the app."""

    def __init__(self, geometry_type: str, srid: int = 4326):
        self.geometry_type = geometry_type
        self.srid = srid

    def get_col_spec(self, **kw) -> str:
        return f'geometry({self.geometry_type},{self.srid})'

# The area_type vocabulary confirmed against the Uganda extract (see Step 0 profile).
AREA_TYPES = (
    'district', 'national_park', 'protected_area', 'nature_reserve',
    'game_reserve', 'wildlife_reserve', 'forest_reserve', 'other',
)


def _guard_drop_extension(ext: str) -> None:
    # Same pattern as 0002: let Postgres decide inside a savepoint, so a refused
    # DROP (extension still depended on) rolls back only the savepoint.
    bind = op.get_bind()
    try:
        with bind.begin_nested():
            bind.execute(sa.text(f'DROP EXTENSION IF EXISTS {ext}'))
    except Exception as exc:
        orig = getattr(exc, 'orig', None)
        if orig is not None and getattr(orig, 'sqlstate', None) == '2BP01':  # dependent_objects_still_exist
            logging.getLogger('alembic.runtime.migration').warning(
                f'Not dropping the {ext} extension: other objects still depend on it.')
        else:
            raise


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))

    op.create_table(
        'areas_osm',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('osm_type', sa.Text(), nullable=False),          # way | relation
        sa.Column('osm_id', sa.BigInteger(), nullable=False),
        sa.Column('area_type', sa.Text(), nullable=False),
        sa.Column('admin_level', sa.SmallInteger(), nullable=True),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('display_name', sa.Text(), nullable=True),
        sa.Column('alt_names', postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column('geom', _Geom('MultiPolygon'), nullable=False),
        sa.Column('centroid', _Geom('Point'), nullable=False),
        sa.Column('bbox', _Geom('Polygon'), nullable=False),
        sa.Column('simplified_geom', _Geom('MultiPolygon'), nullable=False),
        sa.Column('source_version', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.UniqueConstraint('osm_type', 'osm_id', name='uq_areas_osm_osm_type_id'),
    )
    op.create_check_constraint(
        'ck_areas_osm_area_type', 'areas_osm',
        'area_type IN (' + ', '.join(f"'{t}'" for t in AREA_TYPES) + ')',
    )

    op.create_table(
        'areas_osm_aliases',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('area_osm_id', sa.BigInteger(), nullable=False),
        sa.Column('alias', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['area_osm_id'], ['areas_osm.id'],
                                name='fk_areas_osm_aliases_area_osm_id', ondelete='CASCADE'),
        sa.UniqueConstraint('area_osm_id', 'alias', 'source',
                            name='uq_areas_osm_aliases_area_alias_source'),
    )

    op.create_table(
        'places',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('osm_type', sa.Text(), nullable=False),
        sa.Column('osm_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('alt_names', postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column('kind', sa.Text(), nullable=True),
        sa.Column('geom', _Geom('Point'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.UniqueConstraint('osm_type', 'osm_id', name='uq_places_osm_type_id'),
    )

    # Geometry search: GiST on every geometry column.
    for col in ('geom', 'centroid', 'bbox', 'simplified_geom'):
        op.create_index(f'ix_areas_osm_{col}', 'areas_osm', [col],
                        postgresql_using='gist')
    # Name search: trigram GIN on display names and aliases.
    op.create_index('ix_areas_osm_name_trgm', 'areas_osm', ['name'],
                    postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('ix_areas_osm_display_name_trgm', 'areas_osm', ['display_name'],
                    postgresql_using='gin',
                    postgresql_ops={'display_name': 'gin_trgm_ops'})
    # alt_names is a text[]; a plain GIN covers containment/equality lookups.
    op.create_index('ix_areas_osm_alt_names_gin', 'areas_osm', ['alt_names'],
                    postgresql_using='gin')
    op.create_index('ix_areas_osm_aliases_area_osm_id', 'areas_osm_aliases',
                    ['area_osm_id'])
    op.create_index('ix_areas_osm_aliases_alias_trgm', 'areas_osm_aliases', ['alias'],
                    postgresql_using='gin', postgresql_ops={'alias': 'gin_trgm_ops'})
    op.create_index('ix_places_geom', 'places', ['geom'], postgresql_using='gist')

    op.execute(sa.text(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    ))
    for table in ('areas_osm', 'places'):
        op.execute(sa.text(
            f'CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} '
            'FOR EACH ROW EXECUTE FUNCTION set_updated_at()'
        ))


def downgrade() -> None:
    for table in ('areas_osm_aliases', 'places', 'areas_osm'):
        op.drop_table(table)
    op.execute(sa.text('DROP FUNCTION IF EXISTS set_updated_at()'))
    _guard_drop_extension('pg_trgm')