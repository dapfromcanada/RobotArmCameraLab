"""Generic decorator-based registry (drivers, lights, metrics, processors).

Adding an implementation is one new module that decorates itself, plus one
config entry; nothing else is edited.

Example::

    LIGHT_DRIVERS: Registry[type[LightController]] = Registry("light driver")

    @LIGHT_DRIVERS.register("simulated")
    class SimulatedLight(LightController): ...

    LIGHT_DRIVERS.get("simulated")
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

NAME_RE = re.compile(r"[a-z0-9_-]{1,64}")


class RegistryError(KeyError):
    """Unknown or duplicate registry name."""

    def __str__(self) -> str:  # KeyError would repr() the message
        return str(self.args[0]) if self.args else ""


class Registry[T]:
    """A name → object map filled by the :meth:`register` decorator."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str) -> Callable[[T], T]:
        """Decorator registering the decorated object under ``name``.

        Raises:
            RegistryError: if ``name`` is invalid or already registered.
        """
        if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
            raise RegistryError(f"invalid {self.kind} name {name!r}: must match {NAME_RE.pattern}")

        def decorator(obj: T) -> T:
            if name in self._items:
                raise RegistryError(f"duplicate {self.kind} name {name!r}")
            self._items[name] = obj
            return obj

        return decorator

    def get(self, name: str) -> T:
        """Return the object registered as ``name``.

        Raises:
            RegistryError: naming the known entries, if ``name`` is unknown.
        """
        try:
            return self._items[name]
        except (KeyError, TypeError):
            known = ", ".join(self.names()) or "none"
            raise RegistryError(f"unknown {self.kind} {name!r}; known: {known}") from None

    def names(self) -> list[str]:
        """Return the registered names, sorted."""
        return sorted(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._items)
