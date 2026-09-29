"""The lesson gate's port allocation, and the two ways it was wrong.

`tools/check_lessons.py` gives every snippet that starts its own server a port to
use. It got that wrong twice, and both times **the local run was green**:

* it returned a constant, and two lessons that both started a server collided;
* it then asked the OS for port 0 and treated a port below its own floor as
  unusable — returning the constant *again*. macOS's ephemeral range starts at
  49152 so the branch never fired; Linux's starts at 32768 so half of it did, and
  five snippets were all given 48500. `make lessons` was 87/87 on a Mac and the
  `unit` job failed on every push with `address already in use`.

So these tests are the interesting part, not the assertions: they make the Linux
ephemeral range happen on a Mac, because **the failure mode is an operating
system default and the machine you are standing on is not the one that fails.**
"""

from __future__ import annotations

import socket
import sys

import pytest
from tools import check_lessons


@pytest.fixture
def clean_registry():
    """`_HANDED_OUT` emptied either side, so the tests do not depend on order."""
    check_lessons._HANDED_OUT.clear()
    yield
    check_lessons._HANDED_OUT.clear()


@pytest.fixture
def low_ephemeral(monkeypatch: pytest.MonkeyPatch):
    """Make the OS hand back ephemeral ports *below* the floor, as Linux does.

    Not a mock of `free_port` — a mock would be asserting against the shape of
    the fix rather than its behaviour. This substitutes `socket.socket` so that
    `bind(("127.0.0.1", 0))` lands below `FIRST_SNIPPET_PORT`, which is the
    actual input that used to produce 48500 five times over. Every other `bind`,
    including the walk's verification of each candidate, behaves normally.
    """
    real_socket = socket.socket
    real_bind = real_socket.bind
    counter = {"n": 0}

    class _LowRangeSocket(real_socket):  # type: ignore[misc, valid-type]
        def bind(self, address):
            if address[1] == 0:
                counter["n"] += 1
                return real_bind(self, ("127.0.0.1", 40000 + counter["n"]))
            return real_bind(self, address)

    monkeypatch.setattr(socket, "socket", _LowRangeSocket)
    return counter


def test_a_port_below_the_floor_does_not_collapse_to_one_constant(
    clean_registry, low_ephemeral,
) -> None:
    """The CI failure, reproduced locally.

    Every call here receives an ephemeral port the original code considered
    unusable, so every one of them returned `FIRST_SNIPPET_PORT`. The assertion is
    that five calls give five different ports — the property whose absence
    produced five `Errno 98`s on the runner.
    """
    ports = [check_lessons.free_port() for _ in range(5)]

    assert len(set(ports)) == 5, (
        f"five snippets were given {sorted(set(ports))}; the second server to "
        f"bind fails, and the failure is reported against the wrong lesson"
    )
    assert all(p >= check_lessons.FIRST_SNIPPET_PORT for p in ports), (
        f"{[p for p in ports if p < check_lessons.FIRST_SNIPPET_PORT]} are below "
        f"the floor, where this project's real services live"
    )


def test_the_walk_actually_verifies_each_candidate(
    clean_registry, low_ephemeral,
) -> None:
    """A port is free because it was bound, not because a range said so.

    Held sockets, one per candidate, and the walk has to step over them. This is
    the difference between "48500, 48501, 48502…" by arithmetic — which is the
    bug again in a different costume — and a port known to be free.

    `low_ephemeral` is required: without it the OS offers a port above the floor,
    the walk never runs, and the test would pass without testing the walk.
    """
    held = []
    for candidate in range(
        check_lessons.FIRST_SNIPPET_PORT, check_lessons.FIRST_SNIPPET_PORT + 3
    ):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", candidate))
        held.append(sock)
    try:
        assert check_lessons.free_port() == check_lessons.FIRST_SNIPPET_PORT + 3
    finally:
        for sock in held:
            sock.close()


def test_two_calls_never_share_a_port(clean_registry, low_ephemeral) -> None:
    """A snippet that leaks its server must not break the next one.

    The walk verifies a port is free *now*, and the probe closes before the
    snippet binds. If a snippet never stops its server, the only thing preventing
    the next snippet from being handed the same port is the record of what has
    been handed out — and without it the failure lands on whichever lesson ran
    second, which is the shape of a bug in the lesson rather than in the gate.
    """
    first = check_lessons.free_port()
    # Hold it, as a leaked server would.
    leaked = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    leaked.bind(("127.0.0.1", first))
    leaked.listen(1)
    try:
        second = check_lessons.free_port()
        assert second != first
    finally:
        leaked.close()


def test_the_ephemeral_range_is_platform_dependent() -> None:
    """The premise of `test_a_port_below_the_floor…`, asserted rather than assumed.

    This is the sentence that explains why the gate was green locally: the
    *macOS* ephemeral range starts above the floor, and the *Linux* one does not.
    If a future platform makes the Linux case impossible this test fails, and
    someone reads why before deleting the branch that handles it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        ephemeral: int = probe.getsockname()[1]
    assert ephemeral >= 32768, (
        "the OS does not use the Linux 32768+ ephemeral range, so this suite's "
        "reproduction of the CI failure is not the CI failure"
    )
    if sys.platform == "darwin":
        assert ephemeral >= check_lessons.FIRST_SNIPPET_PORT, (
            "on macOS the branch under test cannot fire, which is exactly why "
            "the bug survived: make lessons is green here and red on the runner"
        )


