"""Mirror the imported OSM districts into the legacy ``areas`` table.

Step 4/5 imported real Ugandan geography into ``areas_osm`` (districts, parks,
reserves) while the community report form and advisory/redaction joins kept
working off the small seeded demo communities in ``areas``. This data migration
makes the reporter-facing list real: every ``areas_osm`` district is mirrored
into ``areas`` (id = the OSM id as text, centroid as lat/lon), so forms,
advisories, dashboards and /public/nearby keep their existing joins but present
genuine district names.

Additive and idempotent: existing rows (the demo communities, or admin-created
areas) are never touched, and re-running skips ids that already exist. Districts
only — the ~62 k village/town gazetteer rows are not expected to be a dropdown.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0006_areas_osm_districts'
down_revision = '0005_public_nearby'
branch_labels = None
depends_on = None

_DISTRICT_DESCRIPTION = 'OSM district area imported from the Uganda extract.'


def upgrade() -> None:
    bind = op.get_bind()
    # 0003 onwards is PostGIS-only; guard so a stray non-Postgres runner skips.
    if bind.dialect.name != 'postgresql':
        return
    if not sa.inspect(bind).has_table('areas_osm'):
        return
    bind.execute(sa.text('''
        INSERT INTO areas (id, name, description, latitude, longitude, radius_km)
        SELECT a.id::text,
               COALESCE(NULLIF(a.display_name, ''), a.name),
               :description,
               ST_Y(a.centroid),
               ST_X(a.centroid),
               10.0
        FROM areas_osm a
        WHERE a.area_type = 'district'
          AND NOT EXISTS (SELECT 1 FROM areas ar WHERE ar.id = a.id::text)
    '''), {'description': _DISTRICT_DESCRIPTION})


def downgrade() -> None:
    """Remove only the rows this migration added (best effort)."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    if not sa.inspect(bind).has_table('areas_osm'):
        return
    # Best effort: remove only the rows this migration added. Reports/advisories
    # referencing a district id will block the delete (FK), which is correct.
    bind.execute(sa.text('''
        DELETE FROM areas ar
        WHERE ar.description = :description
          AND EXISTS (
              SELECT 1 FROM areas_osm a
              WHERE a.area_type = 'district' AND a.id::text = ar.id)
    '''), {'description': _DISTRICT_DESCRIPTION})