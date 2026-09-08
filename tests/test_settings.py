from pathlib import Path

from optionsignal.settings import db_path, listen_port, public_url


def test_listen_port_uses_railway_port(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    assert listen_port() == 8080


def test_public_url_from_railway_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "optionsignal-prod.up.railway.app")
    assert public_url() == "https://optionsignal-prod.up.railway.app"


def test_db_path_uses_volume(monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")
    assert db_path() == Path("/data/optionsignal.db")
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")
    monkeypatch.setenv("OPTIONSIGNAL_DB", "/tmp/custom.db")
    assert db_path() == Path("/tmp/custom.db")
