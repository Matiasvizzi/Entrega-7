"""Checkpointer de LangGraph sobre Redis (async), compatible con CUALQUIER Redis.

El paquete oficial `langgraph-checkpoint-redis` exige Redis Stack (módulos RedisJSON y
RediSearch). Este checkpointer usa solo estructuras básicas (hashes y sorted sets), así que
funciona con un `redis:7` común, con Redis administrado o con un Redis local.

Modelo de datos (prefijo `lg:`; todo expira con JOB_TTL_S):
  lg:ckpt:{thread}:{ns}:{id}    hash  {ckpt_type, ckpt, meta_type, meta, parent}
  lg:idx:{thread}:{ns}          zset  ids de checkpoints (score 0; los ids de LangGraph son
                                      ordenables en el tiempo, así que el orden léxico = orden cronológico)
  lg:writes:{thread}:{ns}:{id}  hash  "{task_id}:{idx}" -> escrituras pendientes del checkpoint
                                      (aquí se guarda el interrupt() del nodo HITL)

Con esto `interrupt()` sobrevive a un reinicio de la API: al reanudar, el grafo recupera su
estado desde Redis, no desde la memoria del proceso.
"""
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from redis.asyncio import Redis

_SEP = b"\x00"


class RedisCheckpointSaver(BaseCheckpointSaver[str]):
    """`client` debe crearse SIN decode_responses (los checkpoints son binarios)."""

    def __init__(self, client: Redis, ttl_s: int | None = None) -> None:
        super().__init__()
        self.client = client
        self.ttl_s = ttl_s

    # ------------------------------------------------------------------ claves
    @staticmethod
    def _ids(config: RunnableConfig) -> tuple[str, str, str | None]:
        c = config["configurable"]
        return str(c["thread_id"]), c.get("checkpoint_ns", ""), c.get("checkpoint_id")

    @staticmethod
    def _k(kind: str, thread: str, ns: str, cid: str | None = None) -> str:
        return f"lg:{kind}:{thread}:{ns}" + (f":{cid}" if cid else "")

    async def _expire(self, *keys: str) -> None:
        if self.ttl_s:
            for k in keys:
                await self.client.expire(k, self.ttl_s)

    # --------------------------------------------------------------------- put
    async def aput(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
                   new_versions: ChannelVersions) -> RunnableConfig:
        thread, ns, parent = self._ids(config)
        cid = checkpoint["id"]
        ckpt_type, ckpt_blob = self.serde.dumps_typed(checkpoint)
        meta_type, meta_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        key, idx = self._k("ckpt", thread, ns, cid), self._k("idx", thread, ns)
        await self.client.hset(key, mapping={"ckpt_type": ckpt_type, "ckpt": ckpt_blob, "meta_type": meta_type,
                                             "meta": meta_blob, "parent": parent or ""})
        await self.client.zadd(idx, {cid: 0})
        await self._expire(key, idx)
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": cid}}

    async def aput_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                          task_path: str = "") -> None:
        thread, ns, cid = self._ids(config)
        key = self._k("writes", thread, ns, cid)
        for idx, (channel, value) in enumerate(writes):
            tipo, blob = self.serde.dumps_typed(value)
            field = f"{task_id}:{WRITES_IDX_MAP.get(channel, idx)}"
            payload = _SEP.join([tipo.encode(), task_path.encode(), channel.encode(), blob])
            if channel in WRITES_IDX_MAP:   # interrupt / error / scheduled: se sobrescriben
                await self.client.hset(key, field, payload)
            else:                           # escrituras normales: no se duplican si se reintentan
                await self.client.hsetnx(key, field, payload)
        await self._expire(key)

    # --------------------------------------------------------------------- get
    async def _cargar(self, thread: str, ns: str, cid: str) -> CheckpointTuple | None:
        datos = await self.client.hgetall(self._k("ckpt", thread, ns, cid))
        if not datos:
            return None
        g = lambda k: datos[k.encode()]  # noqa: E731  (el cliente devuelve claves/valores en bytes)
        checkpoint = self.serde.loads_typed((g("ckpt_type").decode(), g("ckpt")))
        metadata = self.serde.loads_typed((g("meta_type").decode(), g("meta")))
        parent = g("parent").decode()

        crudas = await self.client.hgetall(self._k("writes", thread, ns, cid))
        pendientes: list[tuple[str, int, str, Any]] = []
        for field, payload in crudas.items():
            task_id, idx = field.decode().rsplit(":", 1)
            tipo, _path, canal, blob = payload.split(_SEP, 3)
            pendientes.append((task_id, int(idx), canal.decode(), self.serde.loads_typed((tipo.decode(), blob))))
        pendientes.sort(key=lambda w: (w[0], w[1]))

        return CheckpointTuple(
            config={"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": cid}},
            checkpoint=checkpoint, metadata=metadata,
            parent_config={"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": parent}}
            if parent else None,
            pending_writes=[(t, c, v) for t, _i, c, v in pendientes],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread, ns, cid = self._ids(config)
        if cid is None:
            ultimo = await self.client.zrevrange(self._k("idx", thread, ns), 0, 0)
            if not ultimo:
                return None
            cid = ultimo[0].decode()
        return await self._cargar(thread, ns, cid)

    async def alist(self, config: RunnableConfig | None, *, filter: dict[str, Any] | None = None,
                    before: RunnableConfig | None = None, limit: int | None = None
                    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread, ns, _ = self._ids(config)
        antes = self._ids(before)[2] if before else None
        n = 0
        for raw in await self.client.zrevrange(self._k("idx", thread, ns), 0, -1):
            cid = raw.decode()
            if antes and cid >= antes:
                continue
            tupla = await self._cargar(thread, ns, cid)
            if tupla is None:
                continue
            if filter and not all(tupla.metadata.get(k) == v for k, v in filter.items()):
                continue
            yield tupla
            n += 1
            if limit is not None and n >= limit:
                return

    async def adelete_thread(self, thread_id: str) -> None:
        async for key in self.client.scan_iter(match=f"lg:*:{thread_id}:*", count=200):
            await self.client.delete(key)
