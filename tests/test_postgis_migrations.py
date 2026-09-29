from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app import workspace

REPO_ROOT = Path(__file__).resolve().parents[1]
ALembic_INI = REPO_ROOT / 'alembic.ini'
LIVE_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/ecoguard'
MAINT_URL = 'postgresql+psycopg://postgres:postgres@localhost:5432/postgres'


class _FakeDialect:
    name = 'postgresql'


class _FakeBind:
    dialect = _FakeDialect()


class _FakeDB:
    bind = _FakeBind()


def _admin_engine():
    return create_engine(MAINT_URL, isolation_level='AUTOCOMMIT')


def _scratch_url(name: str) -> str:
    return f'postgresql+psycopg://postgres:postgres@localhost:5432/{name}'


def _make_alembic_cfg(url: str) -> Config:
    cfg = Config(str(ALembic_INI))
    cfg.set_main_option('script_location', str(REPO_ROOT / 'alembic'))
    cfg.set_main_option('sqlalchemy.url', url)
    return cfg


def _schema_signature(engine):
    # Compare only the application's own tables: skip alembic's bookkeeping and any
    # tables owned by a PostGIS extension (e.g. spatial_ref_sys), so the baseline
    # check stays meaningful whether or not the live DB has PostGIS enabled yet.
    with engine.connect() as conn:
        ext_owned = set(conn.execute(text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = c.oid "
            "    AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e' "
            "JOIN pg_extension e ON e.oid = d.refobjid "
            "WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
        )).scalars())
    insp = inspect(engine)
    tables = [t for t in insp.get_table_names() if t not in ext_owned and t != 'alembic_version']
    result = {}
    for table in sorted(tables):
        cols = tuple(
            (c['name'], str(c['type']), c['nullable'])
            for c in insp.get_columns(table)
        )
        pks = tuple(insp.get_pk_constraint(table).get('constrained_columns') or [])
        fks = tuple(
            sorted(
                (tuple(fk.get('constrained_columns') or []), fk.get('referred_table'), tuple(fk.get('referred_columns') or []))
                for fk in insp.get_foreign_keys(table)
            )
        )
        uniques = tuple(
            sorted(tuple(uc.get('column_names') or []) for uc in insp.get_unique_constraints(table))
        )
        indexes = tuple(
            sorted(
                tuple(idx.get('column_names') or [])
                for idx in insp.get_indexes(table)
                if not idx.get('unique')
            )
        )
        result[table] = {'columns': cols, 'pk': pks, 'fks': fks, 'uniques': uniques, 'indexes': indexes}
    return result
def test_readiness_reports_postgis_present(monkeypatch):
    monkeypatch.setattr(workspace, '_alembic_current', lambda db: '0002_enable_postgis')
    monkeypatch.setattr(workspace, '_alembic_head', lambda: '0002_enable_postgis')
    monkeypatch.setattr(workspace, '_postgis_version', lambda db: '3.5.0')
    body = workspace.database_capabilities(_FakeDB())
    assert body['status'] == 'ok'
    assert body['database'] == 'postgresql-postgis'
    assert body['postgis_version'] == '3.5.0'
    assert body['alembic'] == {'current': '0002_enable_postgis', 'head': '0002_enable_postgis'}


def test_readiness_reports_postgis_absent(monkeypatch):
    monkeypatch.setattr(workspace, '_alembic_current', lambda db: '0001_baseline_schema')
    monkeypatch.setattr(workspace, '_alembic_head', lambda: '0001_baseline_schema')
    monkeypatch.setattr(workspace, '_postgis_version', lambda db: None)
    body = workspace.database_capabilities(_FakeDB())
    assert body['status'] == 'ok'
    assert body['database'] == 'postgresql'
    assert body['postgis_version'] is None
    assert body['alembic'] == {'current': '0001_baseline_schema', 'head': '0001_baseline_schema'}


def test_readiness_fails_closed_when_postgis_is_unreadable(monkeypatch):
    monkeypatch.setattr(workspace, '_alembic_current', lambda db: '0001_baseline_schema')
    monkeypatch.setattr(workspace, '_alembic_head', lambda: '0001_baseline_schema')

    def unreadable(db):
        raise HTTPException(503, 'Unable to inspect PostGIS capability.')

    monkeypatch.setattr(workspace, '_postgis_version', unreadable)
    with pytest.raises(HTTPException) as exc:
        workspace.database_capabilities(_FakeDB())
    assert exc.value.status_code == 503


def test_migrations_at_head_match_live_schema_and_install_postgis():
    try:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text('SELECT 1'))
    except OperationalError as exc:
        pytest.skip(f'Local Postgres is unavailable for migration checks: {exc}')

    scratch = f'ecoguard_migration_{uuid.uuid4().hex[:10]}'
    scratch_engine = create_engine(_scratch_url(scratch), isolation_level='AUTOCOMMIT')
    try:
        with admin.begin() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            conn.execute(text(f'CREATE DATABASE "{scratch}" TEMPLATE template0'))

        cfg = _make_alembic_cfg(_scratch_url(scratch))
        # Compare at head: the live dev database moves forward with each schema
        # step (it is at 0004 for spatial authorization), so the scratch schema
        # must be migrated to the same head the live DB must be on.
        command.upgrade(cfg, 'head')

        live = create_engine(LIVE_URL)
        assert _schema_signature(scratch_engine) == _schema_signature(live)

        command.upgrade(cfg, 'head')
        with scratch_engine.connect() as conn:
            installed = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='postgis'")).scalar_one_or_none()
        assert installed is not None
    finally:
        try:
            scratch_engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        finally:
            admin.dispose()


def test_0002_downgrade_drops_postgis_only_without_dependents():
    try:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text('SELECT 1'))
    except OperationalError as exc:
        pytest.skip(f'Local Postgres is unavailable for migration checks: {exc}')

    admin = _admin_engine()

    def scratch() -> str:
        name = 'ecoguard_downgrade_' + uuid.uuid4().hex[:8]
        with admin.begin() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
        return name

    def postgis_present(url: str) -> bool:
        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                return conn.execute(
                    text("SELECT count(*) FROM pg_extension WHERE extname='postgis'")
                ).scalar() > 0
        finally:
            eng.dispose()

    def current_rev(url: str) -> str:
        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                return conn.execute(text('SELECT version_num FROM alembic_version')).scalar()
        finally:
            eng.dispose()

    names = []
    try:
        # No dependents: downgrade drops the extension and completes.
        db_a = scratch()
        names.append(db_a)
        url_a = _scratch_url(db_a)
        cfg_a = _make_alembic_cfg(url_a)
        command.upgrade(cfg_a, 'head')
        assert postgis_present(url_a)
        command.downgrade(cfg_a, '0001_baseline_schema')
        assert not postgis_present(url_a)
        assert current_rev(url_a) == '0001_baseline_schema'

        # A geometry column depends on the extension: downgrade must keep postgis.
        db_b = scratch()
        names.append(db_b)
        url_b = _scratch_url(db_b)
        cfg_b = _make_alembic_cfg(url_b)
        command.upgrade(cfg_b, 'head')
        eng_b = create_engine(url_b, isolation_level='AUTOCOMMIT')
        try:
            with eng_b.begin() as conn:
                conn.execute(text('CREATE TABLE geo_test (id int, geom geometry(Point,4326))'))
        finally:
            eng_b.dispose()
        command.downgrade(cfg_b, '0001_baseline_schema')
        assert postgis_present(url_b)
        assert current_rev(url_b) == '0001_baseline_schema'
    finally:
        for name in names:
            url = _scratch_url(name)
            eng = create_engine(url)
            try:
                with eng.connect() as conn:
                    conn.execute(text(
                        'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                        f"WHERE datname='{name}' AND pid <> pg_backend_pid()"))
            finally:
                eng.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
