from functools import lru_cache
from typing import Literal
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_env: Literal['development', 'test', 'production'] = 'development'
    database_url: str = 'postgresql+psycopg://ecoguard:ecoguard@localhost:5432/ecoguard'
    # Comma-separated browser origins allowed to call the API with cookies. The
    # web app is served from a different site than the API (Vercel vs Railway), so
    # this is the list that makes sign-in work. CORS_ALLOWED_ORIGINS is the
    # documented name; ALLOWED_ORIGINS stays accepted so existing deploys and the
    # docker-compose file keep working.
    allowed_origins: str = Field(
        default='http://localhost:5173,http://localhost:8080',
        validation_alias=AliasChoices('cors_allowed_origins', 'allowed_origins'))
    session_cookie: str = 'ecoguard_session'
    cookie_secure: bool = False
    session_samesite: str = 'lax'
    session_hours: int = 12
    jobs_mode: Literal['celery', 'inline'] = 'celery'
    # Escape hatch for a single-service deployment with no worker. Off by default:
    # long jobs (image classification) then belong in Celery, not a request thread.
    allow_inline_jobs: bool = False
    redis_url: str = 'redis://localhost:6379/0'
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None
    kv_rest_api_url: str | None = None
    kv_rest_api_token: str | None = None
    cache_ttl_seconds: int = 15
    storage_backend: Literal['local', 's3'] = 'local'
    media_dir: str = './var/media'
    max_upload_mb: int = 10
    s3_endpoint_url: str | None = None
    s3_bucket: str = 'ecoguard-private'
    s3_region: str = 'us-east-1'
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    image_assistance: Literal['auto', 'disabled'] = 'auto'
    speciesnet_model: str = 'kaggle:google/speciesnet/pyTorch/v4.0.3b/1'
    speciesnet_local_dir: str | None = None
    speciesnet_cache_dir: str = './var/models'
    speciesnet_device: str | None = None
    speciesnet_warmup: bool = True
    model_version: str = ''
    confidence_threshold: float = .75
    sms_provider: Literal['disabled', 'webhook'] = 'disabled'
    sms_webhook_url: str | None = None
    sms_webhook_token: str | None = None
    demo_enabled: bool = False
    enable_api_docs: bool = True
    auto_create_tables: bool = False
    # OSM data attribution — surfaced in /config and on every /areas-osm response
    # so the frontend can always render the required credit line.
    osm_attribution: str = '© OpenStreetMap contributors, ODbL'
    osm_source_name: str = 'OpenStreetMap (Geofabrik extract)'

    @property
    def origins(self):
        return [o.strip().rstrip('/') for o in self.allowed_origins.split(',') if o.strip()]

    @property
    def ai_enabled(self):
        return self.image_assistance != 'disabled'

    @model_validator(mode='after')
    def production_guard(self):
        # Applies in every environment: a SameSite=None cookie without Secure is
        # dropped by the browser, which looks like a broken login rather than a
        # misconfiguration. Fail loudly at startup instead.
        if self.session_samesite == 'none' and not self.cookie_secure:
            raise ValueError('SESSION_SAMESITE=none requires COOKIE_SECURE=true; browsers reject the cookie otherwise.')
        if self.app_env == 'production':
            if not self.cookie_secure or self.demo_enabled or self.auto_create_tables:
                raise ValueError('Production requires secure cookies, no demo seed and migrations (not auto_create_tables).')
            if self.session_samesite != 'none':
                raise ValueError(
                    'Production requires SESSION_SAMESITE=none: the web app and the API are served from '
                    'different sites, and a Lax session cookie is not sent on the cross-site sign-in request.')
            if not self.database_url.startswith('postgresql'):
                raise ValueError('Production requires PostgreSQL.')
            if self.jobs_mode != 'celery' and not self.allow_inline_jobs:
                raise ValueError(
                    'Production requires Celery jobs. Run a worker service (celery -A app.worker.celery_app '
                    'worker) alongside the API, or set ALLOW_INLINE_JOBS=true to run jobs in the web process.')
            if any(not o.startswith('https://') or '*' in o for o in self.origins):
                raise ValueError('Production requires explicit HTTPS origins.')
        return self

@lru_cache
def get_settings():
    return Settings()
