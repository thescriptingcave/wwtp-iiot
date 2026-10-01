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
import yaml
from storage.postgres import schema
from storage.seed import main as seed_main
from tools import notebook_data

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


# ── the regression this guard actually shipped ───────────────────────────────


def test_notebook_data_names_the_database_it_resets() -> None:
    """`tools/notebook_data.py` calls the seeder with `--reset`, so it must name one.

    **This is the bug the `--reset` guard caused when it was first written.** It
    fired correctly, and the first thing it broke was `make notebooks-data` — on a
    clean checkout, reported as

        wwtp_notebooks: created
        ERROR storage.seed refusing to --reset a database this run was not told about
        wwtp_notebooks: not available (UndefinedTable)
        make: *** [Makefile:496: notebooks-has-data] Error 2

    which reads as a seeding failure and is not one. Nobody noticed locally because
    every database already existed; it surfaced on a machine that had only just
    cloned the repository, which is the one environment where this target runs.

    So the call site is asserted, not just the guard. A refusal is only safe if
    every caller in the repository can satisfy it, and the way to know that is to
    check rather than to remember.
    """
    calls = _calls(Path(notebook_data.__file__), "seed")
    assert calls, "notebook_data no longer calls the seeder; this test has gone stale"
    for call in calls:
        assert "--reset" in call, (
            "notebook_data no longer resets, so this no longer matters -- and "
            "something else has changed"
        )
        assert '"--database"' in call or "'--database'" in call, (
            f"notebook_data calls the seeder with --reset and no --database: "
            f"{call[:120]}. The seeder will refuse, `make notebooks` will fail on a "
            f"clean machine, and the error will name the seeder rather than the "
            f"caller."
        )


# ── every caller, in every language, found automatically ─────────────────────
#
# The three that broke, in order:
#
#   1. `tools/notebook_data.py`  — found by a person, on a clean machine.
#   2. `compose.yaml`'s `seed`   — found by a person, on a *different* clean
#      machine, an hour later.
#   3. whichever one is next.
#
# Each was found the same way: somebody ran it on hardware nobody had run it on
# before. Two of the three were not in Python, so an `ast` walk over the source
# could not see them, and a test that enumerated the call sites I already knew
# about would have passed on all three.
#
# So this enumerates from the *other* end: it finds every place the repository
# invokes the seeder — the Makefile, the compose file, and every `.py` — and
# requires `--database` on each that passes `--reset`. A new caller is covered the
# moment it is written, and a caller in a language nobody thought of is a failure
# this test produces rather than a bug a person finds.


#: Files allowed to contain a broken seeder invocation, because their entire job is
#: to show a reader what not to do. Both quote `POSTGRES_DB=wwtp_ml ... --reset` as
#: a counter-example, and the finder correctly reports both.
#:
#: **This list is itself checked.** `test_a_quoting_fixture_actually_quotes_one`
#: fails if a file is added here without quoting a broken invocation, so the
#: exclusion cannot grow into a quiet hole in the check. An exclusion list that
#: only ever grows, with nothing verifying its members still need it, is how a gate
#: stops being a gate while still reporting green -- the outcome this whole file
#: exists to prevent.
#:
#: It was one entry when the second was added by exactly this failure: a new test
#: documenting that the workshop README used to teach a destructive command quoted
#: that command in its own docstring, and this check failed on its own fixture.
QUOTING_FIXTURES = (
    "tests/test_seed_target_database.py",
    "tests/test_readme_teaches_no_destructive_command.py",
)


def _is_a_quoting_fixture(where: str) -> bool:
    return any(where.startswith(name) for name in QUOTING_FIXTURES)


def test_a_quoting_fixture_actually_quotes_one() -> None:
    """Every name in `QUOTING_FIXTURES` must earn its place.

    Without this, `QUOTING_FIXTURES` could absorb any file at any time and the
    `--reset` rule would quietly stop applying to it. The failure this catches is
    invisible: the gate stays green and the coverage is gone.
    """
    offenders = []
    for name in QUOTING_FIXTURES:
        broken = [
            argv
            for where, argv, _kind in _invocations()
            if where.startswith(name)
            and _has(argv, "--reset")
            and not _has(argv, "--database")
        ]
        if not broken:
            offenders.append(name)
    assert not offenders, (
        "these files are excluded from the --reset rule but no longer contain a "
        "broken seeder invocation, so the exclusion is now pure lost coverage:\n  "
        + "\n  ".join(offenders)
        + "\nEither restore the counter-example or drop the name from "
        "QUOTING_FIXTURES."
    )


def _invocations() -> list[tuple[str, list[str], str]]:
    """`(where, argv, kind)` for every seeder invocation in the repository.

    Three sources, because the two that broke were in different files from the
    third: Python, the Makefile, and `compose.yaml`.

    **Each invocation is a list of arguments, not a line of text.** A compose
    `command:` is a YAML sequence, so `--database` and `--reset` are separate list
    elements, and a line-oriented check sees a line with a reset and no database
    next to a line with a database and no reset -- and passes. That was the first
    version, and removing the flag from `compose.yaml` did not fail the test.

    The `kind` is what the assertion reports, so a failure names the file rather
    than "a caller".
    """
    found: list[tuple[str, list[str], str]] = []

    for path in Path().rglob("*.py"):
        if any(p in (".venv", ".git", "node_modules") for p in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for call in _calls(path, "seed"):
            found.append((f"{path} (python)", _argv(call), "python"))
        # A direct `storage.seed.main` run, which is how the Makefile and compose
        # do it and which no Python call site would reveal.
        for line in text.splitlines():
            if "storage.seed.main" in line:
                found.append((f"{path} (python)", line.split(), "python"))

    for line in Path("Makefile").read_text(encoding="utf-8").splitlines():
        if "storage.seed.main" in line and not line.strip().startswith("#"):
            found.append(("Makefile", line.split(), "makefile"))

    for compose in (Path("compose.yaml"), Path("compose.yml")):
        if not compose.exists():
            continue
        services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
        for name, svc in services.items():
            command = svc.get("command") or []
            argv = [str(a) for a in command]
            if any("storage.seed.main" in a for a in argv):
                found.append((f"{compose}:{name}", argv, "compose"))

    return found


def _argv(call: str) -> list[str]:
    """A rendered Python call as an argument list.

    `ast.unparse` gives `seed(['--database', 'wwtp_ml', '--reset'])`, so the
    brackets and the trailing paren are stripped and the list literal's quoting
    removed. Deliberately simple: these are the project's own calls, and a call
    with an embedded newline in an argument would need this to be a real parser.
    """
    inner = call[call.index("(") + 1:call.rindex(")")]
    return [a.strip().strip("'\"") for a in inner.strip("[]").split(",") if a.strip()]


def _has(argv: list[str], flag: str) -> bool:
    """Is `flag` among the arguments, as `--flag` or `--flag=value`?"""
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def test_there_are_invocations_to_check() -> None:
    """The enumeration itself is a test, because an empty list passes everything.

    This is the failure mode of every "find all the X and check them" test: if the
    finder breaks — a renamed flag, a reformatted compose file — the list comes
    back empty and the check reports success. Three callers are known to exist, so
    three is the floor.
    """
    found = [f for f in _invocations()
             if not f[0].startswith("tests/test_seed_target_database.py")]
    assert len(found) >= 3, (
        f"only found {len(found)} seeder invocation(s): "
        f"{[where for where, _, _ in found]}. The finder has probably broken, "
        f"and an empty list passes every check built on it."
    )


def test_every_invocation_that_resets_names_its_database() -> None:
    """`--reset` without `--database` anywhere in the repository is a failure.

    The one rule. No escape hatch, because an escape hatch is something a recipe
    reaches for the first time it is inconvenient — and "inconvenient" is exactly
    the moment the guard exists.
    """
    offenders = [
        (where, argv)
        for where, argv, _kind in _invocations()
        if not _is_a_quoting_fixture(where)
        and _has(argv, "--reset") and not _has(argv, "--database")
    ]
    assert not offenders, (
        "these seeder invocations pass --reset without --database, and the seeder "
        "will refuse them:\n  "
        + "\n  ".join(f"{where}: {' '.join(argv)}" for where, argv in offenders)
    )
