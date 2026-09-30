"""The seeder must seed the database it is told to, and only that one.

Seven places in the seed path meant "the database this run is seeding" and were
written as "the database the environment names". Six of them are now functions that
take a DSN; this file is why that is enforced rather than remembered, because the
failure is **invisible in the command that causes it** and each one surfaced as a
message pointing somewhere else entirely.

The mechanism, once, so the tests below do not each restate it: `tools/py.sh` sources
`.env` *after* the inherited environment, so

    POSTGRES_DB=wwtp_ml $(PY) -m storage.seed.main --reset

seeds **`wwtp`** — the plant's own database — and empties it. The recipe prints
`--reset` and the right database name. The Makefile documents that precedence
deliberately and is right to: a laptop's `POSTGRES_PORT` should not beat a
checked-in file. It is only wrong here, where the database name is the thing under
discussion.

So the database is an **argument**, because a command-line argument cannot be
overridden by a sourced file, and `--reset` without one is **refused** rather than
trusted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from storage.postgres import schema
from storage.seed import main as seed_main

#: Where each of the six used to connect from the environment, and what it must
#: receive instead. The names are the ones the log lines use, so a failure says which
#: call is wrong rather than "something in the seed path".
CALL_SITES = (
    "apply_schema",
    "seed_metadata",
    "refresh_aggregates",
    "apply_retention",
    "apply_refresh_policies",
    "_seed_bounds",
    "_report",
    "_prepare_database",
)


def _calls(path: Path, name: str) -> list[str]:
    """Every real call to `name` in the module at `path`, as source text.

    **`ast`, not a regular expression and not tokenize.** Both earlier attempts
    failed the same way, and it is the lesson this repository keeps relearning:

    * a `[^)]*` pattern stopped at the first `)` and matched nothing useful;
    * a non-greedy `.*?` matched from one call site, across the rest of the file, to
      another call's `conn=conn` — so the *file* satisfied the pattern rather than
      the *call*, and two mutations survived;
    * a tokenize pass that filtered comments but not strings then matched
      ``apply_schema()`` **inside a docstring explaining the bug this file
      exists to prevent.**

    A module's prose about a call is not a call, and this repository has a name for
    the difference.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == name
    ]


def _called_name(node: ast.Call) -> str | None:
    """`"re.sub"` or `"apply_schema"` — a dotted name, not just a bare one.

    The first version matched `ast.Name` only, so `re.sub(...)` — the call that
    actually rewrites the DSN, and therefore the one the test most needed to see —
    was invisible to it. A second version recursed by fabricating a `Call` around
    the attribute's value, which returns `"re"` rather than `"re.sub"`; the dotted
    form is a loop, not a recursion.
    """
    func = node.func
    parts: list[str] = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    return ".".join([func.id, *reversed(parts)])


def _defines(path: Path, name: str) -> str:
    """The source of `def name(...)` in the module at `path`, comments and all.

    The *definition* is read as text on purpose: the assertions are about the
    signature and about whether the body opens its own connection, and a signature
    is text. A body is better read as a tree, but these three functions are short
    and the assertions are about the presence of two specific statements, so the
    trade is stated here rather than pretended away.
    """
    source = path.read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    following = re.search(r"^def ", source[start + 1:], re.M)
    end = start + 1 + following.start() if following else len(source)
    return source[start:end]


# ── the two rules, as behaviour rather than as source text ──────────────────


def test_an_unknown_reset_database_is_refused() -> None:
    """`--reset` with no `--database` stops, before it opens a connection.

    Not "warns". The recipe that caused this printed the right database and did the
    wrong thing, so a line that scrolls past is not a control. The run has to fail.

    Safe to assert by running, because the guard fires *before* any connection: a
    regression that reordered it would write into whatever the environment names,
    which is the accident, and the test would then be the second casualty. So the
    second test below pins the ordering structurally.
    """
    assert seed_main.main(["--days", "1", "--reset"]) == 2


def test_the_guard_precedes_any_connection() -> None:
    """The refusal has to come before the seeder opens anything.

    A test that *runs* `main(["--days", "1", "--reset"])` to check it returns 2 is
    one edit away from writing a million rows into the plant's database, because the
    thing it is testing is exactly the thing that would stop it. The ordering is
    therefore asserted structurally, and the behavioural test above is the
    belt-and-braces rather than the load-bearing one.
    """
    tree = ast.parse(Path(seed_main.__file__).read_text(encoding="utf-8"))
    main_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    def _line_of(predicate) -> int:
        return next(
            (n.lineno for n in ast.walk(main_fn) if predicate(n)),
            len("x") * 10**6,
        )

    guard = _line_of(lambda n: isinstance(n, ast.If) and "args.reset" in ast.unparse(n))
    connect = _line_of(
        lambda n: (isinstance(n, ast.Call)
                   and ast.unparse(n.func).endswith("psycopg.connect"))
    )
    assert guard < connect, (
        f"the --reset guard is at line {guard} and the first psycopg.connect inside "
        f"`main` is at line {connect}. The refusal has to come first, or it is a "
        f"warning rather than a gate."
    )


def test_naming_the_database_is_what_unlocks_the_reset() -> None:
    """`--database` is the only thing that changes the guard's mind, and it is used.

    Asserted by parsing rather than by running a real seed, because running one
    would write 4.2 M rows into whichever database the test environment names — and
    that is precisely the accident this file is about.
    """
    code = " ".join(Path(seed_main.__file__).read_text(encoding="utf-8").split())

    guard = re.search(r"if args\.reset and not (args\.\w+):", code)
    assert guard, "the --reset guard is gone; that is the thing being protected"
    assert guard.group(1) == "args.database", (
        f"the guard now keys on `{guard.group(1)}` rather than the explicit "
        f"--database. Any other key means the run can be talked into resetting a "
        f"database nobody named."
    )

    # **Under an `if`, not merely present.** The first version asserted that a
    # `re.sub("dbname=...", args.database, ...)` call exists somewhere, which is
    # true even when the line above it is `if False:` — and the mutation that does
    # exactly that survived. A call can be present and unreachable, and a test that
    # only asks "is it there" cannot tell the two apart.
    tree = ast.parse(Path(seed_main.__file__).read_text(encoding="utf-8"))
    rewriting = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _called_name(node) == "re.sub"
        and "dbname=" in ast.unparse(node)
    ]
    assert rewriting, (
        "--database is parsed and it unlocks the reset, but the DSN is never "
        "rewritten — so the guard passes and the seed still goes wherever the "
        "environment says"
    )
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(sub in ast.unparse(node.body) for sub in ("dbname=",))
    ]
    assert guarded, "the rewrite is not conditional on --database"
    for node in guarded:
        assert ast.unparse(node.test) == "args.database", (
            f"the rewrite is conditional on `{ast.unparse(node.test)}` rather than on "
            f"--database. Anything else means --database is accepted, unlocks the "
            f"reset, and is then ignored."
        )
    assert all("args.database" in ast.unparse(call) for call in rewriting), (
        "a re.sub rewrites the DSN but not from --database"
    )


# ── the call sites, so a seventh cannot be added by accident ─────────────────


@pytest.mark.parametrize("name", CALL_SITES)
def test_each_call_site_receives_the_seeded_database(name: str) -> None:
    """Every one of the eight, at its call site in the seeder, gets a connection.

    A test with a database would catch the *behaviour* — and there is one in
    `tests/integration` — but it would catch it by writing four million rows and
    reading an error about a foreign key. These are structural, they run in a
    millisecond, and they name which of the eight is missing.

    The set is closed on purpose. Each entry was a separate fix, and each was found
    by the *next* call failing with a message about something else entirely.
    """
    calls = _calls(Path(seed_main.__file__), name)
    assert calls, f"`{name}` is not called at all; this file has gone stale"
    for call in calls:
        assert "conn" in call or "dsn_str" in call, (
            f"`{name}` is called with no connection and no DSN: {call[:100]}. It "
            f"will open its own from the environment — a different database the "
            f"moment `--database` is used — and the log will still say it worked."
        )


@pytest.mark.parametrize("name", [
    "apply_retention", "apply_refresh_policies", "refresh_aggregates",
])
def test_the_callees_accept_a_connection_and_open_at_most_one(name: str) -> None:
    """A connection threaded to a call site is worth nothing if the callee ignores it.

    The other half of the same bug, and the one that failed *quietly*: the seeder
    passed a connection, and `apply_refresh_policies` opened its own on the next
    line anyway. The log said "refresh policy on reading_1m" and the policies went
    to the plant's database.
    """
    raw = _defines(Path(schema.__file__), name)
    body = " ".join(raw.split())

    # A regex rather than a literal, because the literal has to guess how the
    # signature is spaced and `" ".join(text.split())` does not insert a space
    # before a colon. Guessing at formatting in an assertion is how an assertion
    # ends up passing for the wrong reason.
    assert re.search(r"conn\s*:\s*Connection\s*\|\s*None\s*=\s*None", raw), (
        f"{name} takes no `conn` parameter, so a caller passing one is a TypeError "
        f"or — worse — a silent fallback to the environment"
    )
    assert "own = conn is None" in body, (
        f"{name} does not distinguish a borrowed connection from one it opened, so "
        f"it either closes a caller's connection or leaks its own"
    )
    # **The borrow has to be honoured, in both spellings.** Checking only for
    # `with connect() as conn` left a mutation that wrote `conn = connect()` —
    # same bug, different syntax — and it passed: the signature was right and
    # `own = conn is None` was right, so a callee that took a connection and then
    # ignored it looked correct to every assertion here.
    assert re.search(r"conn\s*=\s*conn\s+or\s+connect\(\)", body), (
        f"{name} does not use the connection it was handed; it opens its own. "
        f"That is the bug, and a `conn` parameter does not prevent it."
    )
    assert "with connect() as conn" not in body, (
        f"{name} still opens its own connection from the environment; that is the "
        f"bug, and it survives the parameter"
    )


# ── and the guard the Makefile depends on ───────────────────────────────────


def test_the_workshop_target_uses_the_argument_not_the_environment() -> None:
    """`make workshop-seed` passes `--reset`, so the spelling is load-bearing.

    A test on Makefile text is normally the wrong kind of test. It is the right kind
    here because **the command in the recipe is not the command that runs**:
    `POSTGRES_DB=x $(PY) ...` runs with `POSTGRES_DB=wwtp`, and the only way to see
    the difference is to read what the recipe says.

    Comment lines are dropped first, because the recipe explains this rule in prose
    and a test that read its own explanation would pass for the wrong reason.
    """
    makefile = Path("Makefile").read_text(encoding="utf-8")
    seed = re.search(r"^workshop-seed:.*?(?=^[a-z][\w-]*:)", makefile, re.M | re.S)
    assert seed, "no workshop-seed target; the Makefile moved"
    recipe = " ".join(
        ln for ln in seed.group(0).splitlines()
        if not ln.strip().startswith("#")
    )
    recipe = " ".join(recipe.split())

    assert "--reset" in recipe, (
        "workshop-seed no longer resets, so the --reset guard no longer matters and "
        "something else has changed"
    )
    assert "storage.seed.main --database" in recipe, (
        "workshop-seed does not pass --database, so it will be refused by the "
        "guard. That is the guard working; this test says so rather than leaving a "
        "reader to work out why the target stopped."
    )
    assert not re.search(r"POSTGRES_DB=\S+.*storage\.seed\.main", recipe), (
        "workshop-seed sets POSTGRES_DB in front of $(PY). `.env` is sourced after "
        "the inherited environment, so that names the plant's database and --reset "
        "empties it."
    )
