"""Destination addresses: validation, normalisation, masking, and the allowlist policy.

Standard library only, so ``earmark.models`` and ``earmark.config`` can import it. Nothing
here puts an address into an exception message.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Self

ALL: Final = "all"
ELLIPSIS: Final = "…"
MAX_ADDRESS_CHARS: Final = 254
MAX_LOCAL_CHARS: Final = 64
MAX_DOMAIN_CHARS: Final = 253

_ATEXT: Final = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL: Final = re.compile(rf"{_ATEXT}(?:\.{_ATEXT})*")
_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")

REFUSES_ALL_MESSAGE: Final = (
    "no recipients are allowed yet: set EARMARK_ALLOWED_RECIPIENTS on the server"
)
NOT_ALLOWED_MESSAGE: Final = "to is not an allowed recipient (EARMARK_ALLOWED_RECIPIENTS)"


def normalize_address(value: str) -> str:
    """Strip; validate; return the local part unchanged, ``@``, and the domain lower-cased.

    Raises ``ValueError("not an email address")`` with no input in the message.
    """
    text = value.strip()
    if not text.isascii() or len(text) > MAX_ADDRESS_CHARS or text.count("@") != 1:
        raise ValueError("not an email address")
    local, _, domain = text.partition("@")
    if not 1 <= len(local) <= MAX_LOCAL_CHARS or _LOCAL.fullmatch(local) is None:
        raise ValueError("not an email address")
    if not 1 <= len(domain) <= MAX_DOMAIN_CHARS:
        raise ValueError("not an email address")
    labels = domain.split(".")
    if len(labels) < 2 or any(_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError("not an email address")
    if labels[-1].isdigit():
        raise ValueError("not an email address")
    return f"{local}@{domain.lower()}"


def mask_address(address: str) -> str:
    """``alice@example.org`` becomes ``a…@example.org``; a value without ``@`` becomes ``…``."""
    local, at, domain = address.partition("@")
    if not at or not local:
        return ELLIPSIS
    return f"{local[0]}{ELLIPSIS}@{domain}"


class RecipientRefused(Exception):  # noqa: N818  # a refusal, named for what it says
    """The destination is not allowed. ``message`` is safe to return to the client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class RecipientPolicy:
    allow_all: bool
    addresses: frozenset[str]  # normalised

    @classmethod
    def from_setting(cls, entries: Sequence[str]) -> Self:
        if len(entries) == 1 and entries[0].strip().lower() == ALL:
            return cls(allow_all=True, addresses=frozenset())
        return cls(allow_all=False, addresses=frozenset(normalize_address(e) for e in entries))

    @property
    def refuses_all(self) -> bool:
        return not self.allow_all and not self.addresses

    def allows(self, address: str) -> bool:
        """``address`` must already be normalised."""
        return self.allow_all or address in self.addresses

    def describe(self) -> str:
        if self.allow_all:
            return ALL
        return ", ".join(sorted(mask_address(a) for a in self.addresses))

    def check(self, address: str) -> None:
        """Raise ``RecipientRefused`` unless the normalised ``address`` is allowed."""
        if self.refuses_all:
            raise RecipientRefused(REFUSES_ALL_MESSAGE)
        if not self.allows(address):
            raise RecipientRefused(NOT_ALLOWED_MESSAGE)
