"""The Alembic migrations must produce exactly the ORM schema."""

import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from qal.config import get_settings
from qal.db import Base, make_engine

ROOT = os.path.dirname(os.path.dirname(__file__))


def test_upgrade_head_matches_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("QAL_DATABASE_URL", url)
    get_settings.cache_clear()
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    command.upgrade(cfg, "head")
    with make_engine(url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    command.downgrade(cfg, "base")
    get_settings.cache_clear()
