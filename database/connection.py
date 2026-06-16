from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import settings

# Setup standard sync engine (PostgreSQL/SQLite)
engine_kwargs = {"pool_pre_ping": True}
if "sqlite" not in settings.DATABASE_URL:
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

engine = create_engine(
    settings.DATABASE_URL,
    **engine_kwargs
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    """
    Dependency generator for FastAPI routes to obtain a database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
