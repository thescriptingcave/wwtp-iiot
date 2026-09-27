"""The Couchbase SDK adapter.

The only place in the project that imports the ``couchbase`` package. Everything
else — every document shape, every key, every decision about what belongs in a
document — lives in ``metadata.py`` and is tested without a server.

The import is lazy for the same reason the InfluxDB one is: the metadata layer
should be usable, and testable, by someone who has not installed the storage
extras. A module-level import would make that impossible, and would make the
storage extras a hard dependency of the protocol code.

## The SDK's shape, as measured against 4.6.3

Every one of these was wrong in the first version of this file, and every one of
them was found by running it rather than by reading it. The pattern is worth
noting: **none of the failures were silent.** Each raised a `TypeError` or a
`ModuleNotFoundError` naming the wrong thing, so a single run found all of them.
That is not a defence of the SDK — it is a reminder that a wrapper written from
memory is a wrapper that has not been run.

* ``ClusterOptions`` no longer takes ``username``/``password``. It requires an
  ``authenticator``, and passing the old keywords raises
  ``ClusterOptionsBase.__init__() missing 1 required positional argument:
  'authenticator'``.
* It also moved. ``from couchbase.cluster import ClusterOptions`` is deprecated in
  favour of ``couchbase.options``.
* ``wait_until_ready(timeout=30)`` raises ``'int' object has no attribute
  'total_seconds'``. The timeout is a ``timedelta``.
* ``couchbase.queries`` does not exist. ``QueryOptions`` is in
  ``couchbase.options``.
* **A named collection does not exist until it is created**, and writing to one
  fails with ``AmbiguousTimeoutException`` whose ``retry_reasons`` is
  ``{'key_value_collection_outdated'}``. Nothing in that message mentions
  collections, scopes, or the fact that the name you asked for is not there — it
  reads like a network timeout, and the SDK's own retry logic spent its budget
  first. Every document in this project is therefore written to the scope's
  ``_default`` collection, which always exists.

  That is not a workaround. The keys are already namespaced by kind —
  ``equipment::``, ``tag::``, ``site::``, ``event::`` — and every document
  carries a ``doc_type``, so a second level of collection names would be
  redundant. ``COUCHBASE_COLLECTION`` still overrides it for anyone who has
  created a collection and wants it.
* ``cluster.buckets`` is a **method**, not a property, and the manager's methods
  are named ``create_bucket``/``get_all_buckets``. Bucket creation additionally
  requires a ``CreateBucketSettings`` object from ``couchbase.management.buckets``;
  passing keyword arguments raises ``'str' object has no attribute 'get'``,
  which is an internal error that says nothing useful about the cause.

The bucket is assumed to exist. This project does not create buckets at run time:
a bucket is a schema decision, and a seeder that quietly created one would be a
seeder that could paper over a missing one. ``docs/GETTING-STARTED.md`` has the
command.
"""

from __future__ import annotations

import datetime
import logging
import os
from collections.abc import Callable
from typing import Any

log = logging.getLogger("storage.couchbase.client")

#: How long to wait for the cluster before giving up. A first boot initialises its
#: topology for a while after the container reports healthy, and the SDK's connect
#: returns before that finishes — so the first upsert fails with a message about no
#: endpoints, which sends you looking at the network instead of at the timing.
READY_TIMEOUT_S = 60


def make_upsert(
    bucket_name: str | None = None,
) -> Callable[[str, dict[str, Any]], None]:
    """Build an ``upsert(key, document) -> None`` against a real bucket.

    Connects, ensures a primary index exists, and returns the callable the
    :class:`~storage.couchbase.metadata.MetadataWriter` expects. Every failure mode
    raises: this runs at seed time, where a caller that cannot tell the difference
    between "no documents written" and "the bucket does not exist" will report
    success either way.
    """
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions, QueryOptions

    url = os.environ.get("COUCHBASE_URL", "couchbase://couchbase")
    user = os.environ.get("COUCHBASE_USER", "")
    password = os.environ.get("COUCHBASE_PASSWORD", "")
    bucket = bucket_name or os.environ.get("COUCHBASE_BUCKET", "wwtp")
    scope = os.environ.get("COUCHBASE_SCOPE", "_default")
    # ``_default`` and not a name of our own: a named collection has to be created
    # through the management API first, and writing to one that does not exist
    # fails with a timeout that never mentions collections.
    collection_name = os.environ.get("COUCHBASE_COLLECTION", "_default")

    if not user or not password:
        raise RuntimeError(
            "COUCHBASE_USER and COUCHBASE_PASSWORD must be set to write metadata"
        )

    cluster = Cluster(
        url,
        ClusterOptions(authenticator=PasswordAuthenticator(user, password)),
    )
    cluster.wait_until_ready(timeout=datetime.timedelta(seconds=READY_TIMEOUT_S))

    cb_bucket = cluster.bucket(bucket)
    collection = cb_bucket.scope(scope).collection(collection_name)

    # A primary index is what makes any N1QL query work, and it is not created by
    # default. IF NOT EXISTS so this is safe to run on every seed.
    index = (f"CREATE PRIMARY INDEX IF NOT EXISTS "
             f"ON `{bucket}`.`{scope}`.`{collection_name}`")
    cluster.query(index, QueryOptions(scan_consistency="request_plus"))

    def upsert(key: str, document: dict[str, Any]) -> None:
        collection.upsert(key, document)

    # Held so the cluster outlives the callable. Without it the SDK's background
    # threads are torn down when this function returns, and the first few writes
    # fail with a connection error that has nothing to do with the document.
    upsert.keep_alive = cluster  # type: ignore[attr-defined]
    return upsert
