"""SQLAlchemy engine, session factory, and Base for all ORM models."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.app.core.config import settings

# Ensure parent directory exists for SQLite files (e.g., ./data/procurement_agent.db)
if settings.database_url.startswith("sqlite:///"):
    raw_path = settings.database_url[len("sqlite:///"):]
    if raw_path and not raw_path.startswith(":memory:"):
        from pathlib import Path
        Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 60},  # required for SQLite + FastAPI
)

from sqlalchemy import event


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.close()


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
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA busy_timeout=60000;"))
        conn.commit()

        try:
            p_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(products)")).fetchall()]
            if p_cols and 'id' not in p_cols:
                conn.execute(text("""
                    CREATE TABLE products_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        product_code VARCHAR(64),
                        product_name VARCHAR(256) NOT NULL,
                        batch_no VARCHAR(64),
                        expiry_date DATETIME,
                        category VARCHAR(128),
                        company VARCHAR(256),
                        manufacturer VARCHAR(256),
                        is_promo_material BOOLEAN DEFAULT 0,
                        unit VARCHAR(32) DEFAULT 'units',
                        pack_size FLOAT DEFAULT 1.0,
                        min_order_qty FLOAT DEFAULT 1.0,
                        unit_cost FLOAT,
                        current_stock FLOAT DEFAULT 0.0,
                        reorder_point FLOAT DEFAULT 0.0,
                        reorder_enabled BOOLEAN DEFAULT 1,
                        supplier_name VARCHAR(256),
                        preferred_supplier_id VARCHAR(64),
                        created_at DATETIME,
                        updated_at DATETIME
                    );
                """))
                conn.execute(text("""
                    INSERT INTO products_new (
                        product_code, product_name, category, company, manufacturer,
                        is_promo_material, unit, pack_size, min_order_qty, unit_cost,
                        reorder_point, reorder_enabled, preferred_supplier_id, created_at, updated_at
                    )
                    SELECT 
                        product_code, product_name, category, company, manufacturer,
                        is_promo_material, unit, pack_size, min_order_qty, unit_cost,
                        reorder_point, reorder_enabled, preferred_supplier_id, created_at, updated_at
                    FROM products;
                """))
                conn.execute(text("DROP TABLE products;"))
                conn.execute(text("ALTER TABLE products_new RENAME TO products;"))
                conn.commit()
        except Exception:
            pass

    Base.metadata.create_all(bind=engine)

    # Automatic SQLite migration helper for newly added columns
    with engine.connect() as conn:
        for table, col, col_type in [
            ('products', 'batch_no', 'VARCHAR(64)'),
            ('products', 'expiry_date', 'DATETIME'),
            ('products', 'current_stock', 'FLOAT DEFAULT 0.0'),
            ('products', 'supplier_name', 'VARCHAR(256)'),
            ('products', 'company', 'VARCHAR(256)'),
            ('products', 'manufacturer', 'VARCHAR(256)'),
            ('products', 'is_promo_material', 'BOOLEAN DEFAULT 0'),
            ('inventory_batches', 'company', 'VARCHAR(256)'),
            ('inventory_batches', 'manufacturer', 'VARCHAR(256)'),
            ('inventory_batches', 'is_promo_material', 'BOOLEAN DEFAULT 0'),
            ('procurement_proposals', 'company', 'VARCHAR(256)'),
            ('procurement_proposals', 'manufacturer', 'VARCHAR(256)'),
            ('procurement_proposals', 'batch_numbers', 'VARCHAR(512)'),
            # v2 — sales_history product name & customer traceability
            ('sales_history', 'product_name', 'VARCHAR(256)'),
            ('sales_history', 'customer_code', 'VARCHAR(64)'),
            ('sales_history', 'customer_name', 'VARCHAR(256)'),
            ('sales_history', 'invoice_no', 'VARCHAR(64)'),
            ('sales_history', 'batch_no', 'VARCHAR(64)'),
            ('sales_history', 'company', 'VARCHAR(256)'),
            ('sales_history', 'free_qty', 'FLOAT DEFAULT 0.0'),
            ('sales_history', 'rate', 'FLOAT DEFAULT 0.0'),
            ('sales_history', 'amount', 'FLOAT DEFAULT 0.0'),
            # v3 — customers group/category classification
            ('customers', 'group_name', 'VARCHAR(128)'),
        ]:
            try:
                cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
                if cols and col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
            except Exception:
                pass
