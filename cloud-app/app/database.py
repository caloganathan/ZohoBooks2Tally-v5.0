from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_column_types() -> None:
    """
    Reconcile column types that changed after tables were first created.

    Schema is provisioned with ``Base.metadata.create_all()``, which never
    ALTERs existing columns. Secrets are now encrypted at rest (Fernet
    ciphertext is longer than the original ``VARCHAR`` limits), so on an
    already-provisioned PostgreSQL database the secret columns must be widened
    to ``TEXT`` or inserts will fail / truncate after an upgrade.

    This runs the idempotent widening on PostgreSQL (ALTERing a column that is
    already ``TEXT`` is a no-op). It does nothing on SQLite, which does not
    enforce string length and is used only for local/dev and tests.
    """
    if engine.dialect.name != "postgresql":
        return

    widenings = [
        ("connectors", "secret"),
        ("zoho_tokens", "access_token"),
        ("zoho_tokens", "refresh_token"),
    ]
    with engine.begin() as conn:
        for table, column in widenings:
            conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TEXT'))
