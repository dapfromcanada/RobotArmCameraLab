"""Shared CLI plumbing: per-invocation state, error → exit-code mapping, output.

Results go to **stdout** (human text, or JSON with ``--json``); logs and error
messages go to **stderr**. With ``--json`` an error is also printed to stdout as
``{"ok": false, "error": {"code": ..., "message": ...}}``.

Exit codes: 0 OK · 1 unexpected error · 2 usage error / bad argument ·
3 camera not found or busy · 4 rejected by safety limits or camera range ·
5 vendor SDK not installed · 6 capture timeout / incomplete buffer.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ValidationError

from camera_lab.core.lights import install_lights_off_handlers
from camera_lab.core.logging import get_logger
from camera_lab.two_d.errors import TwoDError
from camera_lab.two_d.service import TwoDService, build_service

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REJECTED = 4

_log = get_logger(__name__)


@dataclass
class CliState:
    """Global options of one CLI invocation; builds the service lazily."""

    sim: bool = False
    quiet: bool = False
    _service: TwoDService | None = field(default=None, repr=False)

    def service(self, ctx: typer.Context) -> TwoDService:
        """Build the service once; close cameras + lights off when the command ends."""
        if self._service is None:
            svc = build_service(sim=self.sim)
            uninstall = (
                install_lights_off_handlers(svc.light_service.controller)
                if svc.light_service is not None
                else None
            )

            def _cleanup() -> None:
                try:
                    svc.shutdown()
                finally:
                    if uninstall is not None:
                        uninstall()

            ctx.call_on_close(_cleanup)
            self._service = svc
        return self._service


def state(ctx: typer.Context) -> CliState:
    """Return the :class:`CliState` stored on the root context."""
    obj = ctx.find_root().obj
    if not isinstance(obj, CliState):
        obj = CliState()
        ctx.find_root().obj = obj
    return obj


def _default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, tuple | set | frozenset):
        return list(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def to_json(data: Any) -> str:
    """Pretty, stable JSON."""
    return json.dumps(data, indent=2, sort_keys=True, default=_default)


def emit(data: Any, json_out: bool, human: Callable[[], str] | str | None = None) -> None:
    """Print a result: JSON (with ``--json``) or human text, to stdout."""
    if json_out:
        typer.echo(to_json({"ok": True, "result": data}))
    else:
        text = human() if callable(human) else human
        typer.echo(text if text is not None else to_json(data))


def fail(code: str, message: str, exit_code: int, json_out: bool) -> typer.Exit:
    """Print an error and return the ``typer.Exit`` to raise."""
    if json_out:
        typer.echo(to_json({"ok": False, "error": {"code": code, "message": message}}))
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(exit_code)


def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Map typed errors to exit codes; never show a traceback on stdout."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        json_out = bool(kwargs.get("json_out", False))
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort, typer.BadParameter):
            raise
        except TwoDError as exc:
            raise fail(exc.code, str(exc), exc.exit_code, json_out) from None
        except ValidationError as exc:
            msg = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise fail("invalid_argument", msg, EXIT_USAGE, json_out) from None
        except Exception as exc:
            _log.exception("unexpected_error")
            raise fail(
                "internal_error", f"{type(exc).__name__}: {exc}", EXIT_ERROR, json_out
            ) from None

    return wrapper


def parse_floats(text: str, n: int, name: str) -> list[float]:
    """Parse ``"a,b,c"`` into ``n`` floats (``typer.BadParameter`` otherwise)."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != n:
        raise typer.BadParameter(f"{name} needs {n} comma-separated numbers")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise typer.BadParameter(f"{name}: not a number in {text!r}") from None


def parse_ints(text: str, n: int, name: str) -> list[int]:
    """Parse ``"a,b,c,d"`` into ``n`` integers."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != n:
        raise typer.BadParameter(f"{name} needs {n} comma-separated integers")
    try:
        return [int(p) for p in parts]
    except ValueError:
        raise typer.BadParameter(f"{name}: not an integer in {text!r}") from None
