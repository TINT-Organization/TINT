import pytest

from audit_log.config import ConfigError, Settings
from audit_log.health import DEFAULT_PORT


def test_database_url_is_required():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env({})


def test_defaults():
    settings = Settings.from_env({"DATABASE_URL": "postgresql://db/tint"})
    assert settings.redis_url == "redis://localhost:6379"
    assert settings.port == DEFAULT_PORT == 8089
    assert settings.consumer


def test_overrides():
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://db/tint",
            "REDIS_URL": "redis://redis:6379",
            "AUDIT_LOG_PORT": "9000",
            "AUDIT_LOG_CONSUMER": "audit-log-1",
        }
    )
    assert (settings.redis_url, settings.port, settings.consumer) == (
        "redis://redis:6379",
        9000,
        "audit-log-1",
    )


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_invalid_port(port):
    with pytest.raises(ConfigError, match="AUDIT_LOG_PORT"):
        Settings.from_env({"DATABASE_URL": "postgresql://db/tint", "AUDIT_LOG_PORT": port})


def test_database_url_is_not_in_repr():
    assert "secreto" not in repr(Settings.from_env({"DATABASE_URL": "postgresql://u:secreto@db"}))
