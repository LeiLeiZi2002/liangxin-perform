from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest


def load_config_module() -> ModuleType:
    try:
        return import_module("app.config")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Settings module is not implemented: {exc}")


def test_settings_reads_supported_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./test.db")
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://localhost:4173")

    config = load_config_module()
    config.get_settings.cache_clear()
    settings = config.get_settings()

    assert settings.database_url == "sqlite:///./test.db"
    assert settings.frontend_origin == "http://localhost:4173"

    config.get_settings.cache_clear()


def test_settings_default_env_file_is_repository_root() -> None:
    config = load_config_module()

    configured_env_file = Path(config.Settings.model_config["env_file"])

    assert configured_env_file.resolve() == Path(__file__).resolve().parents[2] / ".env"
