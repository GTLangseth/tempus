# tests/test_config.py
from tempus.config import Settings


def test_dev_is_the_default(monkeypatch):
    monkeypatch.delenv("TEMPUS_DB", raising=False)
    s = Settings(_env_file=None)
    assert s.db == "dev" and s.homebase_host == "homebase" and s.prod_db_port == 5432
    assert s.prod_ro_password is None and s.prod_password is None


def test_prod_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TEMPUS_DB", "prod")
    monkeypatch.setenv("TEMPUS_HOMEBASE_HOST", "mini")
    monkeypatch.setenv("TEMPUS_PROD_DB_PORT", "5433")
    monkeypatch.setenv("TEMPUS_PROD_RO_PASSWORD", "ro")
    monkeypatch.setenv("TEMPUS_PROD_PASSWORD", "owner")
    s = Settings(_env_file=None)
    assert (s.db, s.homebase_host, s.prod_db_port) == ("prod", "mini", 5433)
    assert (s.prod_ro_password, s.prod_password) == ("ro", "owner")


def test_fields_can_be_set_by_name():
    assert Settings(_env_file=None, db="prod").db == "prod"
