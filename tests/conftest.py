"""Test environment setup.

Environment variables are set before any ``app`` module is imported, because
``app.db`` builds its engine at import time from the resolved settings. Keep this
file free of ``app`` imports.
"""
import os
import tempfile

_DB = os.path.join(tempfile.gettempdir(), 'ecoguard_pytest.db')

os.environ['DATABASE_URL'] = 'sqlite:///' + _DB
os.environ['APP_ENV'] = 'test'
os.environ['AUTO_CREATE_TABLES'] = 'true'
os.environ['JOBS_MODE'] = 'inline'
os.environ['ALLOWED_ORIGINS'] = 'http://localhost:5173'
os.environ['STORAGE_BACKEND'] = 'local'
os.environ['MEDIA_DIR'] = os.path.join(tempfile.gettempdir(), 'ecoguard_pytest_media')
os.environ['SPECIESNET_WARMUP'] = 'false'
os.environ['CACHE_TTL_SECONDS'] = '0'
os.environ['REDIS_URL'] = ''
