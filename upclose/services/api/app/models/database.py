"""Explicit database construction; importing models never connects to a database."""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    return sessionmaker(bind=engine, expire_on_commit=False)
