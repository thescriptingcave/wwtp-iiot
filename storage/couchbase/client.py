"""The Couchbase SDK adapter.

The only place in the project that imports the ``couchbase`` package. Everything
else — every document shape, every key, every decision about what belongs in a
document — lives in ``metadata.py`` and is tested without a server.

The import is lazy for the same reason the InfluxDB one is: the metadata layer
should be usable, and testable, by someone who has not installed the storage
extras. A module-level import would make that impossible, and would make the
storage extras a hard dependency of the protocol code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

log = logging.getLogger("storage.couchbase.client")


def make_upsert(bucket_name: str | None = None) -> Callable[[str, dict[str, Any]], None]:
    """Build an ``upsert(key, document) -> None`` against a real bucket.

    Connects, ensures the primary index exists, and returns the callable the
    :class:`~storage.couchbase.metadata.MetadataWriter` expects. Every failure
    mode raises: this runs at seed time, where a caller that cannot tell the
    difference between "no documents written" and "the bucket does not exist"
    will report success either way.
    """
    from couchbase.cluster import Cluster, ClusterOptions
    from couchbase.collection import Collection
    from couchbase.queries import QueryOptions

    url = os.environ.get("COUCHBASE_URL", "couchbase://couchbase")
    user = os.environ.get("COUCHBASE_USER", "")
    password = os.environ.get("COUCHBASE_PASSWORD", "")
    bucket = bucket_name or os.environ.get("COUCHBASE_BUCKET", "wwtp")
    scope = os.environ.get("COUCHBASE_SCOPE", "_default")
    collection_name = os.environ.get("COUCHBASE_COLLECTION", "plant")

    if not user or not password:
        raise RuntimeError(
            "COUCHBASE_USER and COUCHBASE_PASSWORD must be set to write metadata"
        )

    cluster = Cluster(url, ClusterOptions(username=user, password=password))
    # wait_until_ready rather than connecting and hoping: the SDK's connect()
    # returns before the cluster has elected a topology, and the first upsert
    # then fails with a message about no endpoints, which sends you looking at
    # the network instead of at the timing.
    cluster.wait_until_ready(timeout=30)

    cb_bucket = cluster.bucket(bucket)
    cb_scope = cb_bucket.scope(scope)
    collection: Collection = cb_scope.collection(collection_name)

    # The primary index is what makes a N1QL query work at all, and it is not
    # created by default on a bucket made through the SDK.
    index = (f"CREATE PRIMARY INDEX IF NOT EXISTS "
             f"ON `{bucket}`.`{scope}`.`{collection_name}`")
    cluster.query(index, QueryOptions(scan_consistency="request_plus"))

    def upsert(key: str, document: dict[str, Any]) -> None:
        collection.upsert(key, document)

    upsert.keep_alive = cluster  # type: ignore[attr-defined]
    return upsert
