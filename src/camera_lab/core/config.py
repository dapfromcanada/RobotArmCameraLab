"""YAML config loading: ``yaml.safe_load`` + pydantic validation.

SECURITY-SENSITIVE (YAML loader). YAML files are edited by hand and are a trust
boundary: they are size-limited, parsed only with ``yaml.safe_load`` (no Python
object tags), and validated by a pydantic model. Error messages name the file
and the offending field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

#: Config files are small; refuse anything bigger than this.
MAX_YAML_BYTES = 1_000_000


class ConfigError(ValueError):
    """A config file is missing, unreadable, not valid YAML or fails validation."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = Path(path)
        super().__init__(f"{self.path}: {message}")


def read_yaml(path: Path) -> Any:
    """Read ``path`` with ``yaml.safe_load`` and return the raw data.

    Raises:
        ConfigError: if the file is missing, too large, unreadable or not valid YAML.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        raise ConfigError(p, "file not found") from None
    except OSError as exc:
        raise ConfigError(p, f"cannot read file: {exc.strerror}") from None
    if not p.is_file():
        raise ConfigError(p, "not a regular file")
    if size > MAX_YAML_BYTES:
        raise ConfigError(p, f"file is {size} bytes; limit is {MAX_YAML_BYTES}")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(p, f"cannot read file: {exc}") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        raise ConfigError(p, f"invalid YAML{where}") from None


def format_validation_error(exc: ValidationError) -> str:
    """Return a short, one-line-per-error description naming each field path."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "<root>"
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


def load_yaml_model[M: BaseModel](path: Path, model: type[M]) -> M:
    """Load ``path`` with ``yaml.safe_load`` and validate it as ``model``.

    Raises:
        ConfigError: naming the file and, for validation failures, each field path.
    """
    data = read_yaml(path)
    if data is None:
        raise ConfigError(Path(path), "file is empty")
    if not isinstance(data, dict):
        raise ConfigError(Path(path), f"top level must be a mapping, got {type(data).__name__}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(Path(path), format_validation_error(exc)) from None
