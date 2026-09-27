"""SQLAlchemy engine, session factory, and Base for all ORM models."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.app.core.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that provides a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables (idempotent — safe to call on every startup)."""
    # Import models so their metadata is registered before create_all
    import backend.app.models.entities  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # Automatic SQLite migration helper for newly added columns
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, col in [
            ('products', 'company'),
            ('inventory_batches', 'company'),
            ('procurement_proposals', 'company'),
        ]:
            try:
                cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
                if cols and col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} VARCHAR(256)"))
                    conn.commit()
            except Exception:
                pass
