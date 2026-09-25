from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session

from .config import settings
from app.models import Base

_connect_args = {"check_same_thread": False}
if settings.DATABASE_URL.startswith("sqlite"):
    # SQLite 写互斥：并发事务（如并发认领）等待锁而不是立即报 database is locked。
    _connect_args["timeout"] = 15

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args
)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # WAL 提升读写并发；外键约束开启以支撑处置记录对预警删除的 RESTRICT。
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
