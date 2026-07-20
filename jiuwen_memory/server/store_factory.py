# -*- coding: UTF-8 -*-
"""Store 工厂

把 memory_server 启动时需要的 KV / DB / Vector store 从硬编码改为按 .env 装配。
具体 vector store 的依赖（pymilvus / elasticsearch / psycopg2 等）走懒导入，
只在选中对应类型时才 import，缺包时只影响该分支，不会影响默认的 chroma 启动。
"""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.foundation.store.base_db_store import BaseDbStore
from jiuwen_memory.foundation.store.base_kv_store import BaseKVStore
from jiuwen_memory.foundation.store.base_vector_store import BaseVectorStore


def _data_dir() -> str:
    """统一读取 MEMORY_DATA_DIR，默认为 ~/.jiuwenmemory/memory_data，并确保目录存在。

    留空（未设置 / 空串 / 纯空白）时回退默认值 —— ``os.getenv`` 的第二参数只在 key
    不存在时生效，``.env`` 里 ``KEY=`` 会注册成空串，必须显式判空才走默认，否则
    ``mkdir('')`` 静默 no-op 后返回空串，下游 register_store 会因 ``not file_root_dir``
    报错。
    """
    default_dir = str(Path.home() / ".jiuwenmemory" / "memory_data")
    data_directory = os.getenv("MEMORY_DATA_DIR", default_dir).strip() or default_dir
    Path(data_directory).mkdir(parents=True, exist_ok=True)
    return data_directory


def create_file_root_dir() -> str:
    """读取 FILE_MEMORY_DATA_DIR，默认为 ~/.jiuwenmemory/file_memory_data，并确保目录存在。

    供 index_backend='file' 时 FileMemoryIndex 的 root_dir 使用。与 _data_dir()
    隔离，避免 file 后端的 markdown + memory.db 和引擎的 sqlite_db.db 混在同一目录。
    留空回退默认值的理由同 ``_data_dir``。
    """
    default_dir = str(Path.home() / ".jiuwenmemory" / "file_memory_data")
    data_directory = os.getenv("FILE_MEMORY_DATA_DIR", default_dir).strip() or default_dir
    Path(data_directory).mkdir(parents=True, exist_ok=True)
    memory_logger.info("Using file memory root_dir=%s", data_directory)
    return data_directory


def create_async_engine_from_env() -> AsyncEngine:
    """根据 DB_URL（或默认 SQLite 路径）创建一个 AsyncEngine。

    KV(db 模式) 与 DB store 共用同一个 engine，避免重复建立连接池。
    """
    data_directory = _data_dir()
    db_url = os.getenv("DB_URL", "").strip()
    if not db_url:
        db_path = Path(data_directory) / "sqlite_db.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+aiosqlite:///{db_path}"

    memory_logger.info("Using DB engine url=%s", db_url)
    return create_async_engine(
        db_url,
        pool_pre_ping=True,
        echo=False,
    )


def create_kv_store(engine: AsyncEngine) -> BaseKVStore:
    """根据 KV_STORE_TYPE 构建 KV store。

    支持值: db | in_memory | shelve
    """
    kv_type = os.getenv("KV_STORE_TYPE", "db").strip().lower()
    memory_logger.info("Using kv store type=%s", kv_type)

    if kv_type == "db":
        from jiuwen_memory.foundation.store import DbBasedKVStore
        return DbBasedKVStore(engine)

    if kv_type == "in_memory":
        from jiuwen_memory.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
        return InMemoryKVStore()

    if kv_type == "shelve":
        from jiuwen_memory.foundation.store.kv.shelve_store import ShelveStore
        shelve_path = os.getenv("KV_SHELVE_PATH", "").strip()
        if not shelve_path:
            shelve_path = str(Path(_data_dir()) / "shelve_kv")
        return ShelveStore(db_path=shelve_path)

    raise ValueError(
        f"Unsupported KV_STORE_TYPE={kv_type!r}, "
        f"expected one of: db, in_memory, shelve"
    )


def create_db_store(engine: AsyncEngine) -> BaseDbStore:
    """根据 DB_STORE_TYPE 构建 DB store。

    支持值: default | gauss
    """
    db_type = os.getenv("DB_STORE_TYPE", "default").strip().lower()
    memory_logger.info("Using db store type=%s", db_type)

    if db_type == "default":
        from jiuwen_memory.foundation.store.db.default_db_store import DefaultDbStore
        return DefaultDbStore(engine)

    if db_type == "gauss":
        # GaussDbStore 在 import 时会注册 gauss 方言，仅按需触发
        from jiuwen_memory.foundation.store.db.gauss_db_store import GaussDbStore
        return GaussDbStore(engine)

    raise ValueError(
        f"Unsupported DB_STORE_TYPE={db_type!r}, "
        f"expected one of: default, gauss"
    )


def create_vector_store() -> BaseVectorStore:
    """根据 VECTOR_STORE_TYPE 构建 Vector store。

    支持值: chroma | milvus | elasticsearch | gauss
    各分支懒导入对应实现，避免没装第三方包时影响其他分支启动。
    """
    vec_type = os.getenv("VECTOR_STORE_TYPE", "chroma").strip().lower()
    memory_logger.info("Using vector store type=%s", vec_type)

    if vec_type == "chroma":
        from jiuwen_memory.foundation.store.vector.chroma_vector_store import ChromaVectorStore
        persist_dir = os.getenv("VECTOR_CHROMA_PERSIST_DIR", "").strip() or _data_dir()
        return ChromaVectorStore(persist_directory=persist_dir)

    if vec_type == "milvus":
        from jiuwen_memory.foundation.store.vector.milvus_vector_store import MilvusVectorStore
        return MilvusVectorStore(
            milvus_uri=os.getenv("VECTOR_MILVUS_URI", "").strip(),
            milvus_token=os.getenv("VECTOR_MILVUS_TOKEN", "").strip() or None,
            database_name=os.getenv("VECTOR_MILVUS_DATABASE", "default"),
        )

    if vec_type == "elasticsearch":
        from jiuwen_memory.foundation.store.vector.es_vector_store import ElasticsearchVectorStore
        hosts_raw = os.getenv("VECTOR_ES_HOSTS", "").strip()
        hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()] or None

        # ES 8.x 默认开启安全特性（HTTPS + basic_auth），以下参数经 **kwargs 透传给
        # AsyncElasticsearch，仅在对应环境变量存在时才组装，避免覆盖 client 默认行为。
        # hosts 支持写 https://... 以启用 TLS；自签证书可关校验或指定 CA。
        kwargs: dict = {}
        es_username = os.getenv("VECTOR_ES_USERNAME", "").strip()
        es_password = os.getenv("VECTOR_ES_PASSWORD", "").strip()
        if es_username or es_password:
            kwargs["basic_auth"] = (es_username, es_password)

        es_api_key = os.getenv("VECTOR_ES_API_KEY", "").strip()
        if es_api_key:
            kwargs["api_key"] = es_api_key

        verify_certs_raw = os.getenv("VECTOR_ES_VERIFY_CERTS", "").strip().lower()
        if verify_certs_raw:
            kwargs["verify_certs"] = verify_certs_raw != "false"
        # 留空时不设置，沿用 client 默认（verify_certs=True）。

        ca_certs = os.getenv("VECTOR_ES_CA_CERTS", "").strip()
        if ca_certs:
            kwargs["ca_certs"] = ca_certs

        client_cert = os.getenv("VECTOR_ES_CLIENT_CERT", "").strip()
        if client_cert:
            kwargs["client_cert"] = client_cert

        client_key = os.getenv("VECTOR_ES_CLIENT_KEY", "").strip()
        if client_key:
            kwargs["client_key"] = client_key

        return ElasticsearchVectorStore(
            hosts=hosts,
            index_prefix=os.getenv("VECTOR_ES_INDEX_PREFIX", "agent_vector"),
            **kwargs,
        )

    if vec_type == "gauss":
        from jiuwen_memory.foundation.store.vector.gauss_vector_store import GaussVectorStore
        return GaussVectorStore(
            host=os.getenv("VECTOR_GAUSS_HOST", "localhost"),
            port=int(os.getenv("VECTOR_GAUSS_PORT", "5432")),
            database=os.getenv("VECTOR_GAUSS_DATABASE", "postgres"),
            user=os.getenv("VECTOR_GAUSS_USER", "postgres"),
            password=os.getenv("VECTOR_GAUSS_PASSWORD", ""),
        )

    raise ValueError(
        f"Unsupported VECTOR_STORE_TYPE={vec_type!r}, "
        f"expected one of: chroma, milvus, elasticsearch, gauss"
    )
