"""Opt-in startup configuration; never load secrets during module import."""

import os
from pathlib import Path

from trading_companion.model import ModelConfigurationError


def _source_env_file(module_path: Path) -> Path | None:
    """Only recognize this source checkout, never a wheel's site-packages ancestors."""
    package = module_path.resolve().parent
    root = package.parent.parent
    if package.name == "trading_companion" and package.parent.name == "src" and (root / "pyproject.toml").is_file():
        return root / ".env"
    return None


LOCAL_ENV_FILE = _source_env_file(Path(__file__))
MODEL_VARIABLES = ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY")


def _read_local_values() -> dict[str, str | None]:
    """Read one fixed file, without parent searches or variable interpolation."""
    if LOCAL_ENV_FILE is None:
        return {}
    try:
        with LOCAL_ENV_FILE.open(encoding="utf-8-sig") as stream:
            try:
                from dotenv import dotenv_values
            except ImportError:
                raise ModelConfigurationError(
                    "Install model dependencies to read .env: python -m pip install -e '.[model]'"
                ) from None
            return dotenv_values(stream=stream, interpolate=False, verbose=False)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise ModelConfigurationError("Could not read the local .env; check its permissions and UTF-8 encoding") from None


def load_model_environment() -> dict[str, str]:
    """Merge only model settings, preserving existing environment values, even empty ones."""
    environment = {name: os.environ[name] for name in MODEL_VARIABLES if name in os.environ}
    disabled = os.environ.get("PYTHON_DOTENV_DISABLED", "").lower() in ("1", "true", "yes", "on")
    if disabled or len(environment) == len(MODEL_VARIABLES):
        return environment
    local = _read_local_values()
    return {**{name: local[name] for name in MODEL_VARIABLES if isinstance(local.get(name), str)}, **environment}
