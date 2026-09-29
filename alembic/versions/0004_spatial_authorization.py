"""Add spatial authorization: area assignments, per-report containment, location model.

Additive only — nothing from 0001/0002/0003 is touched. Like 0003_osm_areas, the
new tables and geometry columns are managed exclusively by these hand-written
migrations and stay outside ``Base.metadata``: the SQLite test harness cannot
create PostGIS geometry DDL, so the app talks to them through raw SQL gated on the
dialect being PostgreSQL (see app/spatial.py).

Rows are written only by server-side code or the assignment-migration script;
client input never feeds ``report_areas``.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004_spatial_authorization'
down_revision = '0003_osm_areas'
branch_labels = None
depends_on = None


class _Geom(sa.types.UserDefinedType):
    """Render exact PostGIS DDL (``geometry(Point,4326)``) in migrations without
    pulling geoalchemy2 into the app."""

    def __init__(self, geometry_type: str, srid: int = 4326):
        self.geometry_type = geometry_type
        self.srid = srid

    def get_col_spec(self, **kw) -> str:
        return f'geometry({self.geometry_type},{self.srid})'


_EMPTY = sa.Enum('exact', 'generalised', name='report_location_precision',
                 create_type=False, native_enum=True)


def upgrade() -> None:
    # -- vocabulary enums --------------------------------------------------
    op.execute("CREATE TYPE report_location_precision AS ENUM ('exact', 'generalised')")
    op.execute("CREATE TYPE report_location_source AS ENUM ('gps', 'manual', 'area_only')")

    # -- reports: true location, public (generalised) location, provenance ---
    op.add_column('reports', sa.Column('location_geom', _Geom('Point'), nullable=True))
    op.add_column('reports', sa.Column('public_geom', _Geom('Point'), nullable=True))
    op.add_column('reports', sa.Column(
        'location_precision', postgresql.ENUM('exact', 'generalised',
                                              name='report_location_precision',
                                              create_type=False, native_enum=True),
        nullable=True))
    op.add_column('reports', sa.Column(
        'location_source', postgresql.ENUM('gps', 'manual', 'area_only',
                                           name='report_location_source',
                                           create_type=False, native_enum=True),
        nullable=True))
    # The four location columns always move together (all set or all NULL),
    # so partial backfills cannot silently pass the invariant.
    op.create_check_constraint(
        'ck_reports_location_consistent', 'reports',
        '(location_geom IS NULL) = (public_geom IS NULL) '
        'AND (location_geom IS NULL) = (location_precision IS NULL) '
        'AND (location_geom IS NULL) = (location_source IS NULL)',
    )
    op.create_index('ix_reports_location_geom', 'reports', ['location_geom'],
                    postgresql_using='gist')
    op.create_index('ix_reports_public_geom', 'reports', ['public_geom'],
                    postgresql_using='gist')

    # -- user_area_assignments: history-preserving --------------------------
    op.create_table(
        'user_area_assignments',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('area_osm_id', sa.BigInteger(), nullable=False),
        sa.Column('assigned_by', sa.String(36), nullable=False),
        sa.Column('assigned_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('revoked_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='fk_user_area_assignments_user'),
        sa.ForeignKeyConstraint(['area_osm_id'], ['areas_osm.id'],
                                name='fk_user_area_assignments_area_osm'),
        sa.ForeignKeyConstraint(['assigned_by'], ['users.id'],
                                name='fk_user_area_assignments_assigned_by'),
    )
    # History is kept (revoked rows are never deleted), so uniqueness applies
    # only to the live assignment of a user to an area.
    op.execute(sa.text(
        'CREATE UNIQUE INDEX uq_user_area_assignment_active '
        'ON user_area_assignments (user_id, area_osm_id) WHERE revoked_at IS NULL'))
    op.create_index('ix_user_area_assignments_user', 'user_area_assignments',
                    ['user_id'])
    op.create_index('ix_user_area_assignments_area_osm', 'user_area_assignments',
                    ['area_osm_id'])

    # -- report_areas: precomputed containment (server-populated only) --------
    op.create_table(
        'report_areas',
        sa.Column('id', sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column('report_id', sa.String(36), nullable=False),
        sa.Column('area_osm_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'],
                                name='fk_report_areas_report', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['area_osm_id'], ['areas_osm.id'],
                                name='fk_report_areas_area_osm'),
        sa.UniqueConstraint('report_id', 'area_osm_id',
                            name='uq_report_areas_report_area'),
    )
    op.create_index('ix_report_areas_report', 'report_areas', ['report_id'])
    op.create_index('ix_report_areas_area_osm', 'report_areas', ['area_osm_id'])


def downgrade() -> None:
    op.drop_index('ix_reports_public_geom', table_name='reports')
    op.drop_index('ix_reports_location_geom', table_name='reports')
    op.drop_table('report_areas')
    op.drop_table('user_area_assignments')
    op.drop_column('reports', 'location_source')
    op.drop_column('reports', 'location_precision')
    op.drop_column('reports', 'public_geom')
    op.drop_column('reports', 'location_geom')
    op.execute(sa.text('DROP TYPE report_location_source'))
    op.execute(sa.text('DROP TYPE report_location_precision'))