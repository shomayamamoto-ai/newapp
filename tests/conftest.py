import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from snsauto.models import Base, Project  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s


@pytest.fixture
def project(session):
    p = Project(name="test", description="fixture", brand_profile={"tone": "friendly"})
    session.add(p)
    session.flush()
    return p
