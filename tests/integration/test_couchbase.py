"""Integration tests against a running Couchbase.

The Couchbase half of this project had a running container and **zero executed
SDK calls** for its entire life. That is exactly the position the InfluxDB half
was in an hour before it turned out to hide six bugs, and the assumption that it
would be fine was not an assumption worth making.

Running it found four bugs in `storage/couchbase/client.py`, every one of them
fatal and every one of them raised loudly rather than failing quietly:

1. ``ClusterOptions(username=…, password=…)`` no longer exists. It requires an
   ``authenticator``, and the old keywords raise ``missing 1 required positional
   argument: 'authenticator'``.
2. ``wait_until_ready(timeout=30)`` raises ``'int' object has no attribute
   'total_seconds'``. The timeout is a ``timedelta``.
3. ``couchbase.queries`` is not a module. ``QueryOptions`` is in
   ``couchbase.options``.
4. **The default collection name did not exist.** Writing to a named collection
   that has not been created fails with ``AmbiguousTimeoutException`` whose
   ``retry_reasons`` is ``{'key_value_collection_outdated'}`` — a message that
   never mentions collections, scopes, or the fact that the name asked for is not
   there. It reads like a network timeout, and the SDK's retry logic spends its
   budget before the caller sees anything. All 80 documents failed.

   The fix is the ``_default`` collection, which always exists. That is not a
   concession: the keys are already namespaced by kind (``tag::``, ``equipment::``)
   and every document carries ``doc_type``, so a second level of names would be
   redundant.

The read path is a different story and is marked xfail below.

## Skipping

The tests skip when no Couchbase is reachable, so the suite stays runnable with
no databases.
"""

from __future__ import annotations

import datetime
import os
import socket

import pytest
from softplc.contract import contract
from storage.couchbase.metadata import (
    MetadataWriter,
    equipment_document,
    equipment_key,
    site_document,
    site_key,
    tag_document,
    tag_key,
)

C = contract()

CB_URL = os.environ.get("COUCHBASE_TEST_URL", "couchbase://127.0.0.1")
CB_USER = os.environ.get("COUCHBASE_TEST_USER", "")
CB_PASSWORD = os.environ.get("COUCHBASE_TEST_PASSWORD", "")
CB_BUCKET = os.environ.get("COUCHBASE_TEST_BUCKET", "wwtp")


def _reachable(url: str) -> bool:
    host = url.replace("couchbase://", "").replace("couchbases://", "")
    host = host.split("/")[0]
    host, _, port = host.partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 11210)), 2):
            return True
    except (OSError, ValueError):
        return False


def _configured() -> bool:
    return bool(CB_USER and CB_PASSWORD) and _reachable(CB_URL)


if _configured():
    # Set at import, not in a fixture. ``make_upsert`` reads the environment, and
    # a module-scoped fixture is set up *before* any function-scoped one - so a
    # `monkeypatch` autouse fixture set the variables too late, and every test
    # failed with "COUCHBASE_USER and COUCHBASE_PASSWORD must be set" while the
    # credentials were demonstrably in the environment a line above.
    os.environ.setdefault("COUCHBASE_URL", CB_URL)
    os.environ.setdefault("COUCHBASE_USER", CB_USER)
    os.environ.setdefault("COUCHBASE_PASSWORD", CB_PASSWORD)
    os.environ.setdefault("COUCHBASE_BUCKET", CB_BUCKET)


requires_couchbase = pytest.mark.skipif(
    not _configured(),
    reason=(
        "no Couchbase. Set COUCHBASE_TEST_USER / COUCHBASE_TEST_PASSWORD and "
        "start one; see docs/GETTING-STARTED.md."
    ),
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cluster():
    """A live cluster, or skip. Yielded so the SDK's threads stay alive."""
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions

    c = Cluster(
        CB_URL,
        ClusterOptions(authenticator=PasswordAuthenticator(CB_USER, CB_PASSWORD)),
    )
    # A timedelta, not an int. Passing an int raises inside the SDK with an
    # AttributeError about ``total_seconds``, which is not a helpful thing to
    # read at 2am.
    c.wait_until_ready(timeout=datetime.timedelta(seconds=60))
    yield c
    c.close()


@pytest.fixture(scope="module")
def upsert(cluster):
    from storage.couchbase.client import make_upsert

    return make_upsert()


def _collection(cluster):
    return cluster.bucket(CB_BUCKET).scope("_default").collection("_default")


def _prefixed(prefix: str = "it_") -> str:
    """A key prefix unique to one test run, so reruns do not collide."""
    return f"{prefix}{os.getpid()}"


# ─── the write path, which is verified ────────────────────────────────────────


@requires_couchbase
def test_a_document_round_trips_through_couchbase(cluster, upsert) -> None:
    key = _prefixed() + "tag::AERATION:AHU-1:DO"
    upsert(key, tag_document(C.signals["AERATION:AHU-1:DO"]))

    got = _collection(cluster).get(key)
    assert got is not None, "the document did not land"
    body = got.value
    assert body["id"] == "AERATION:AHU-1:DO"
    assert body["unit"] == "mg/L"
    assert body["doc_type"] == "tag"


@requires_couchbase
def test_the_whole_contract_seeds_without_error(cluster, upsert) -> None:
    """80 documents: 1 site, 22 equipment, 57 tags.

    The number matters. A partial seed is the failure mode a metadata store can
    least afford, because it looks populated.
    """
    writer = MetadataWriter(upsert)
    stats = writer.seed_contract(C)
    assert stats.errors == 0, f"{stats.errors} documents failed"
    assert stats.sites == 1
    assert stats.equipment == len(C.equipment)
    assert stats.tags == len(C.signals)

    for doc_key in (site_key(str(C.site["id"])),
                    equipment_key("AHU-1"),
                    tag_key("AERATION:AHU-1:DO")):
        assert _collection(cluster).get(doc_key) is not None, doc_key


@requires_couchbase
def test_seeding_twice_updates_rather_than_duplicating(cluster, upsert) -> None:
    """Idempotent by key. A seeder that appended would be a second, disagreeing
    copy of the contract — and the copy nobody remembers to update."""
    writer = MetadataWriter(upsert)
    writer.seed_contract(C)
    key = tag_key("AERATION:AHU-1:DO")
    before = _collection(cluster).get(key).value

    writer.seed_contract(C)
    after = _collection(cluster).get(key).value
    assert after == before


@requires_couchbase
def test_upsert_overwrites_a_changed_document(cluster, upsert) -> None:
    """The behaviour the idempotency test depends on."""
    key = _prefixed() + "site::PLANT-A"
    doc = site_document(C)
    upsert(key, doc)
    doc["permit"] = {**doc["permit"], "eff_nh4_mg_l_30d_mean": 7.0}
    upsert(key, doc)
    assert _collection(cluster).get(key).value["permit"]["eff_nh4_mg_l_30d_mean"] == 7.0


@requires_couchbase
def test_no_live_state_reaches_an_equipment_document(cluster, upsert) -> None:
    """The contamination test, against the real store rather than a dict.

    ``state`` changes every scan. A document that changes every scan is a
    write-amplification problem wearing a metadata costume.
    """
    key = _prefixed() + equipment_key("AHU-1")
    upsert(key, equipment_document(C.equipment["AHU-1"], str(C.site["id"])))
    body = _collection(cluster).get(key).value
    for forbidden in ("state", "run_state", "running", "value", "last_value"):
        assert forbidden not in body, f"{forbidden!r} in an equipment document"


@requires_couchbase
def test_a_tag_document_holds_no_samples(cluster, upsert) -> None:
    key = _prefixed() + tag_key("EFFLUENT:FLOW:NH4")
    upsert(key, tag_document(C.signals["EFFLUENT:FLOW:NH4"]))
    body = _collection(cluster).get(key).value
    for forbidden in ("value", "values", "samples", "history", "ts"):
        assert forbidden not in body, f"{forbidden!r} in a tag document"


@requires_couchbase
def test_colon_separated_keys_survive_the_round_trip(cluster, upsert) -> None:
    """``tag::AERATION:AHU-1:DO`` has four colons and two double colons.

    A key that needs escaping, or a scope whose keys cannot contain ``::``, would
    fail here — and it would fail on the *write*, with a key-validation error that
    gives no hint which part of the key was wrong.
    """
    key = _prefixed() + "tag::AERATION:AHU-1:DO"
    upsert(key, {"doc_type": "tag", "id": "AERATION:AHU-1:DO"})
    assert _collection(cluster).get(key) is not None


# ─── the read path, which is not verified ─────────────────────────────────────


@pytest.mark.xfail(
    reason=(
        "N1QL row streaming does not work on a single-node Couchbase Community "
        "7.6.2 container in this environment. `cluster.query(...)` returns "
        "successfully; iterating `.rows()` then raises "
        "ServiceUnavailableException with 'Streaming operation failed', for "
        "every statement tried including `SELECT COUNT(*) FROM bucket` and a "
        "bare key lookup. The index service does not appear to be up. This looks "
        "like an environment problem rather than a project bug - the write path "
        "is verified above, and the statements themselves are ordinary N1QL - but "
        "it is not yet proven, so it is not claimed. See docs/LEARNING-LOG.md."
    ),
    strict=False,
)
@requires_couchbase
def test_documents_are_queryable_by_type(cluster, upsert) -> None:
    MetadataWriter(upsert).seed_contract(C)
    from couchbase.options import QueryOptions

    result = cluster.query(
        "SELECT doc_type, COUNT(*) AS n FROM `" + CB_BUCKET + "` "
        "GROUP BY doc_type",
        # request_plus, not the default: immediately after a bulk seed the default
        # consistency does not see the new documents or the new index, and fails
        # with a ServiceUnavailableException that never mentions indexing.
        QueryOptions(scan_consistency="request_plus"),
    )
    counts = {row["doc_type"]: row["n"] for row in result.rows()}
    assert counts["tag"] == len(C.signals)
    assert counts["equipment"] == len(C.equipment)
