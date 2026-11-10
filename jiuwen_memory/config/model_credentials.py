# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Instance-owned encrypted outbound model credentials, addressed by immutable references."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.security.bootstrap import register_security
from jiuwen_memory.common.security.cryptography import CryptographyProducer
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.context import AssemblyContext


class ModelCredentials:
    def __init__(self, database):
        self.path = str(database)
        directory = Path(self.path).parent.stat()
        if os.name == "posix" and (directory.st_uid != os.geteuid() or directory.st_mode & 0o022):
            raise BackendError("model credential directory is writable by other users")
        register_security()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_credentials "
                "(reference TEXT PRIMARY KEY, ciphertext BLOB NOT NULL)"
            )
            existing = db.execute("SELECT COUNT(*) FROM model_credentials").fetchone()[0]
        key_path = Path(self.path).with_suffix(".model-key")
        if key_path.is_symlink():
            raise BackendError("invalid model credential key location")
        if existing and not key_path.is_file():
            raise BackendError("model credential key is missing")
        ctx = AssemblyContext()
        self.crypto = CryptographyProducer.build(
            "local",
            {
                "key_provider": {
                    "target": "local",
                    "params": {
                        "key_file": str(key_path),
                        "key_env": "",
                        "create_key_file": not existing,
                    },
                },
            },
            ctx=ctx,
        )
        self.crypto.health()
        identity = hashlib.sha256(str(Path(self.path).resolve()).encode()).hexdigest()
        self.scope = Scope(org="model-config", user=identity)

    def context(self, reference):
        return CryptoContext(
            scope=self.scope, purpose="model_credential", object_id=reference, format_version=1
        )

    def put(self, value):
        reference = uuid.uuid4().hex
        encrypted = self.crypto.encrypt(value.encode(), context=self.context(reference))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO model_credentials VALUES (?, ?)", (reference, encrypted))
        return reference

    def get(self, reference):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT ciphertext FROM model_credentials WHERE reference=?", (reference,)
            ).fetchone()
        if row is None:
            raise BackendError("model credential reference is unavailable")
        return self.crypto.decrypt(row[0], context=self.context(reference)).decode()

    def discard(self, reference):
        self.retire([reference])

    def retire(self, references):
        """Remove known retired/prepared objects, never another writer's pending reference."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM config_overlay WHERE id=1").fetchone()
            current = json.loads(row[0]) if row else {}
            live = {value for key, value in current.items() if key.endswith(".llm_api_key")}
            for reference in references:
                if reference not in live:
                    db.execute("DELETE FROM model_credentials WHERE reference=?", (reference,))
