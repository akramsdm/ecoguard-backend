from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings

class Base(DeclarativeBase):
    pass

settings = get_settings()

def normalized_database_url(url: str) -> str:
    # Railway/Neon/standard DSNs are bare "postgresql://" which SQLAlchemy maps to the
    # psycopg2 driver; this project pins psycopg v3, so force the explicit driver.
    for prefix in ('postgresql://', 'postgres://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix):]
    return url

_database_url = normalized_database_url(settings.database_url)
engine = create_engine(_database_url, pool_pre_ping=True,
    connect_args={'check_same_thread': False, 'timeout': 30} if _database_url.startswith('sqlite') else {})
if _database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def _sqlite_fk(dbapi_conn, _):
        dbapi_conn.execute('PRAGMA foreign_keys=ON')
SessionLocal = sessionmaker(engine, expire_on_commit=False)

def get_db():
    with SessionLocal() as db:
        try:
            yield db
        except Exception:
            db.rollback()
            raise
