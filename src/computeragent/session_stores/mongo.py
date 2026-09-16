"""``MongoSessionStore`` — durable transcripts, one document per session.

Port of the TS ``@open-gitagent/session-store-mongo``. Document shape::

    { _id: <session_id>, projectKey, entries: [...], updatedAt,
      ownerUser?, ownerGroup? }

Requires the ``[mongo]`` extra (``pymongo>=4.9``, whose ``AsyncMongoClient``
supersedes motor).

Single-writer by design. ``append`` reads the document, filters already-seen
uuids, then pushes — correct when one process owns a session, which is the
harness's model. Concurrent writers to the same session can interleave between
the read and the push; that limitation is inherited from the TS store and
documented there too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..protocol_types import SessionKey, SessionStoreEntry

logger = logging.getLogger("computeragent.session_stores.mongo")

#: Deliberately NOT the database AgentOS reads. Falling back silently would put
#: transcripts somewhere nothing looks, so the constructor says so out loud.
DEFAULT_DATABASE = "computeragent_sessions"
DEFAULT_COLLECTION = "sessions"

#: The driver's own default is 30s, which turns an unreachable cluster into a
#: 30-second stall before the session fails — and because the engine awaits
#: `load` at session start, that surfaces as half a minute of dead air followed
#: by an error. Ten seconds is long enough for a healthy cluster.
DEFAULT_TIMEOUT_MS = 10_000


class MongoSessionStore:
    kind = "mongo"

    def __init__(
        self,
        *,
        url: str,
        database: str | None = None,
        collection: str = DEFAULT_COLLECTION,
        owner_user: str | None = None,
        owner_group: str | None = None,
        client: Any | None = None,
        **client_options: Any,
    ) -> None:
        if not url and client is None:
            raise ValueError("MongoSessionStore: url is required")
        if not database:
            logger.warning(
                "no database configured — defaulting to %r. This is NOT the database AgentOS "
                "reads; set database= to that one, or resumed sessions will not find their "
                "prior turns.",
                DEFAULT_DATABASE,
            )
        self._database = database or DEFAULT_DATABASE
        self._collection = collection
        self._owner_user = owner_user
        self._owner_group = owner_group

        if client is not None:
            self._client = client
        else:
            try:
                from pymongo import AsyncMongoClient
            except ImportError as exc:  # pragma: no cover - guarded by the extra
                raise ImportError(
                    "MongoSessionStore requires the [mongo] extra. "
                    "Install with: pip install 'computer-agent-py[mongo]'"
                ) from exc

            options: dict[str, Any] = {
                "serverSelectionTimeoutMS": DEFAULT_TIMEOUT_MS,
                "connectTimeoutMS": DEFAULT_TIMEOUT_MS,
            }
            options.update(client_options)
            self._client = AsyncMongoClient(url, **options)

    def _coll(self) -> Any:
        return self._client[self._database][self._collection]

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if not entries:
            return
        coll = self._coll()
        doc = await coll.find_one({"_id": {"$eq": _doc_id(key)}})
        seen = {
            e["uuid"]
            for e in (doc or {}).get("entries", [])
            if isinstance(e, dict) and isinstance(e.get("uuid"), str)
        }
        # Entries without a uuid (titles, tags, mode markers) are not
        # deduplicable and always append — dropping them would lose content.
        fresh = [e for e in entries if not e.get("uuid") or e["uuid"] not in seen]
        if not fresh:
            return

        update: dict[str, Any] = {
            "$push": {"entries": {"$each": fresh}},
            "$set": {"projectKey": key["project_key"], "updatedAt": _now()},
            "$setOnInsert": {"_id": _doc_id(key)},
        }
        # Trust-on-first-use ownership, stamped with `$set` guarded on the row
        # not already having an owner — NOT `$setOnInsert`.
        #
        # `$setOnInsert` looks right and is wrong: this is frequently not the
        # write that CREATES the document. With telemetry ingest enabled the
        # server's projection upserts the sessions row on session_started,
        # before any transcript entry exists, so by the first append the
        # document is already there and `$setOnInsert` never fires. The row then
        # stays unowned, and since an unowned row matches everyone, tenancy
        # enforcement silently becomes a no-op. Observed end-to-end in TS.
        if not (doc or {}).get("ownerUser"):
            claim = {
                k: v
                for k, v in (("ownerUser", self._owner_user), ("ownerGroup", self._owner_group))
                if v
            }
            update["$set"].update(claim)

        await coll.update_one({"_id": {"$eq": _doc_id(key)}}, update, upsert=True)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        doc = await self._coll().find_one({"_id": {"$eq": _doc_id(key)}})
        entries = (doc or {}).get("entries")
        # `None`, not `[]`: the contract distinguishes "never written" from a
        # transcript that is genuinely empty, and the SDK starts a fresh session
        # only for the former.
        return list(entries) if entries else None

    async def delete(self, key: SessionKey) -> None:
        await self._coll().delete_one({"_id": {"$eq": _doc_id(key)}})

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        cursor = (
            self._coll().find({"projectKey": {"$eq": project_key}}).sort("updatedAt", -1).limit(50)
        )
        out: list[dict[str, Any]] = []
        async for doc in cursor:
            updated = doc.get("updatedAt")
            mtime = int(updated.timestamp() * 1000) if isinstance(updated, datetime) else 0
            out.append({"session_id": str(doc["_id"]), "mtime": mtime})
        return out

    async def close(self) -> None:
        with_close = getattr(self._client, "close", None)
        if callable(with_close):
            result = with_close()
            if hasattr(result, "__await__"):
                await result


def _doc_id(key: SessionKey) -> str:
    """One document per transcript. A sub-agent's ``subpath`` is part of the
    identity — folding it into the parent's document would corrupt both."""
    subpath = key.get("subpath")
    return f"{key['session_id']}::{subpath}" if subpath else key["session_id"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mongo_session_store_builder(**defaults: Any):
    """Registry builder. Per-call descriptor options merge over these defaults,
    so one registration can serve several databases::

        register_session_store("mongo", mongo_session_store_builder(url=MONGO_URL))
        ComputerAgent(..., session_store={"kind": "mongo",
                                          "options": {"database": "tenant_a"}})
    """

    def _build(options: Any = None) -> MongoSessionStore:
        merged = {**defaults, **(dict(options) if isinstance(options, dict) else {})}
        return MongoSessionStore(**merged)

    return _build


__all__ = [
    "DEFAULT_COLLECTION",
    "DEFAULT_DATABASE",
    "DEFAULT_TIMEOUT_MS",
    "MongoSessionStore",
    "mongo_session_store_builder",
]
