"""Private KV configuration is isolated from ordinary managed ports and hot rebinding."""

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config import AssemblyContext
from jiuwen_memory.config.config_source import ConfigSource, ConfigSourceProducer
from jiuwen_memory.storage.bootstrap import register_backends
from jiuwen_memory.storage.kv import KvProducer
from jiuwen_memory.storage.privacy import PrivacySnapshot, PrivacyStoreProducer
from jiuwen_memory.storage.privacy_impl import KVPrivacyStore
from jiuwen_memory.storage.privacy_impl.kv_privacy_store import STATE_KEY

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("target", "params"),
    [
        ("memory", {}),
        ("sqlite", {"db_path": ":memory:"}),
        ("redis", {"url": "redis://localhost:6379/2"}),
        ("postgres", {"dsn": "postgresql://localhost/private"}),
    ],
)
def test_existing_backends_build_from_private_inline_config(target, params):
    register_backends()
    # Enabled privacy would recurse if the private backend used the business KV builder.
    ctx = AssemblyContext.from_dict({"globals": {"privacy_enabled": True}})
    store = PrivacyStoreProducer.build(
        "kv", {"kv_store": {"target": target, "params": params}}, ctx
    )
    try:
        assert isinstance(store, KVPrivacyStore)
        assert ctx.instance_cache.instances.get(KvProducer, {}) == {}
    finally:
        store.close()


@pytest.mark.parametrize(
    "spec",
    ["default", {"target": "encrypted", "params": {"raw_kv_store": "default"}}],
)
def test_private_backend_cannot_alias_a_managed_public_port(spec):
    register_backends()
    with pytest.raises(ValidationError, match="inline"):
        PrivacyStoreProducer.build("kv", {"kv_store": spec}, AssemblyContext())


def test_private_connections_ignore_business_config_source(tmp_path):
    register_backends()
    path = tmp_path / "new-private-directory" / "private.sqlite"
    other = tmp_path / "wrong-business.sqlite"

    class BusinessConfig(ConfigSource):
        def fetch(self, key):
            return str(other) if key == "kv_store.db_path" else None

    ctx = AssemblyContext()
    ConfigSourceProducer.put("default", BusinessConfig(), ctx=ctx)
    store = PrivacyStoreProducer.build(
        "kv", {"kv_store": {"target": "sqlite", "params": {"db_path": str(path)}}}, ctx
    )
    scope = Scope(user="user")
    try:
        store.insert(scope, PrivacySnapshot("one", "r", "d", b"opaque"), {})
        assert path.exists() and not other.exists()
        ordinary = KvProducer.build("sqlite", {"db_path": ":memory:"}, ctx)
        try:
            ordinary.insert(scope, "business", b"yes")
            assert other.exists()
            assert not ordinary.exists(scope, STATE_KEY)
        finally:
            ordinary.close()
    finally:
        store.close()
