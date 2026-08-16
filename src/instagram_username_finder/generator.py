"""Lazy, deterministic username candidate generation.

The generator performs no I/O. It treats the candidate space for a given length
as a fixed-width odometer over a sorted alphabet, which gives three useful
properties:

* **Lazy** - candidates are produced one at a time, never materialised.
* **Deterministic** - the same alphabet always yields the same order.
* **Addressable** - any index can be decoded in O(length), so a resumed scan
  jumps straight to where it stopped instead of replaying earlier work.
"""

from __future__ import annotations

from collections.abc import Iterator

from .models import Candidate


def is_valid_username(username: str) -> bool:
    """Apply Instagram's structural username rules.

    Periods may not lead, trail, or repeat. Everything else in the supported
    alphabet is structurally acceptable.
    """
    if not username:
        return False
    if username.startswith(".") or username.endswith("."):
        return False
    return ".." not in username


class UsernameGenerator:
    """Produces username candidates over a fixed alphabet."""

    def __init__(self, alphabet: str, *, skip_invalid: bool = True) -> None:
        if not alphabet:
            raise ValueError("alphabet must not be empty")
        if len(set(alphabet)) != len(alphabet):
            raise ValueError("alphabet must not contain duplicate characters")
        #: Sorted so ordering is stable no matter how the alphabet was supplied.
        self.alphabet = "".join(sorted(alphabet))
        self.base = len(self.alphabet)
        self.skip_invalid = skip_invalid

    def space_size(self, length: int) -> int:
        """Total number of raw index positions for ``length``.

        This counts structurally invalid candidates too, because indices address
        the raw space; skipped candidates simply are not yielded.
        """
        if length < 1:
            raise ValueError("length must be >= 1")
        return int(self.base**length)

    def username_at(self, length: int, index: int) -> str:
        """Decode a single index into its username."""
        total = self.space_size(length)
        if not 0 <= index < total:
            raise IndexError(f"index {index} out of range for length {length}")
        return "".join(self.alphabet[d] for d in self._digits(index, length))

    def generate(self, length: int, start_index: int = 0) -> Iterator[Candidate]:
        """Yield candidates of ``length``, starting at ``start_index``."""
        if start_index < 0:
            raise ValueError("start_index must be >= 0")
        total = self.space_size(length)
        if start_index >= total:
            return

        digits = self._digits(start_index, length)
        index = start_index
        while index < total:
            username = "".join(self.alphabet[d] for d in digits)
            if not self.skip_invalid or is_valid_username(username):
                yield Candidate(username=username, length=length, index=index)
            index += 1
            self._increment(digits)

    def iter_lengths(self, min_length: int, max_length: int) -> Iterator[int]:
        """Yield lengths shortest-first, which is the scan order."""
        if min_length < 1:
            raise ValueError("min_length must be >= 1")
        if max_length < min_length:
            raise ValueError("max_length must be >= min_length")
        yield from range(min_length, max_length + 1)

    # ------------------------------------------------------------------
    # odometer internals
    # ------------------------------------------------------------------
    def _digits(self, index: int, length: int) -> list[int]:
        digits = [0] * length
        position = length - 1
        while index and position >= 0:
            index, digits[position] = divmod(index, self.base)
            position -= 1
        return digits

    def _increment(self, digits: list[int]) -> None:
        for position in range(len(digits) - 1, -1, -1):
            digits[position] += 1
            if digits[position] < self.base:
                return
            digits[position] = 0
