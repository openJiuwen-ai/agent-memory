# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Instance-owned encrypted outbound model credentials, addressed by immutable references."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.security.bootstrap import register_security
from jiuwen_memory.common.security.cryptography import (
    AuthenticationFailedError,
    CryptographyProducer,
)
from jiuwen_memory.common.security.types import CryptoContext
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.binding import _canonical_local_path
from jiuwen_memory.config.context import AssemblyContext


class ModelCredentials:
    def __init__(self, database, instance_id: str):
        self.path = str(database)
        self.instance_id = instance_id
        directory = Path(self.path).parent.stat()
        if os.name == "posix" and (directory.st_uid != os.geteuid() or directory.st_mode & 0o022):
            raise BackendError("model credential directory is writable by other users")
        register_security()
        with closing(sqlite3.connect(self.path)) as db, db:
            # Concurrent SDK startups must wait until the key file is fully written.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_credentials "
                "(reference TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, instance_id TEXT NOT NULL)"
            )
            existing = db.execute("SELECT COUNT(*) FROM model_credentials").fetchone()[0]
            self.crypto = self._load_crypto(existing)
        database = Path(self.path).resolve()
        legacy_id = str(database)
        database_id = _canonical_local_path(database)
        self.scope = self._scope_for(database_id)
        self._legacy_scope = self._scope_for(legacy_id) if legacy_id != database_id else None

    def _scope_for(self, database_id):
        # Keep the original default context and the separation between named instances.
        key_scope = database_id
        if self.instance_id != "default":
            key_scope = f"{database_id}\0{self.instance_id}"
        identity = hashlib.sha256(key_scope.encode()).hexdigest()
        return Scope(org="model-config", user=identity)

    def _load_crypto(self, existing):
        key_path = Path(self.path).with_suffix(".model-key")
        if key_path.is_symlink():
            raise BackendError("invalid model credential key location")
        if existing and not key_path.is_file():
            raise BackendError("model credential key is missing")
        ctx = AssemblyContext()
        crypto = CryptographyProducer.build(
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
        crypto.health()
        return crypto

    def context(self, reference):
        return CryptoContext(
            scope=self.scope, purpose="model_credential", object_id=reference, format_version=1
        )

    def put(self, value):
        reference = uuid.uuid4().hex
        encrypted = self.crypto.encrypt(value.encode(), context=self.context(reference))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO model_credentials (reference, ciphertext, instance_id) "
                "VALUES (?, ?, ?)",
                (reference, encrypted, self.instance_id),
            )
        return reference

    def get(self, reference):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT ciphertext FROM model_credentials WHERE reference=? AND instance_id=?",
                (reference, self.instance_id),
            ).fetchone()
        if row is None:
            raise BackendError("model credential reference is unavailable")
        ciphertext, = row
        context = self.context(reference)
        try:
            return self.crypto.decrypt(ciphertext, context=context).decode()
        except AuthenticationFailedError:
            if self._legacy_scope is None:
                raise
        legacy = replace(context, scope=self._legacy_scope)
        value = self.crypto.decrypt(ciphertext, context=legacy)
        migrated = self.crypto.encrypt(value, context=context)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE model_credentials SET ciphertext=? "
                "WHERE reference=? AND instance_id=? AND ciphertext=?",
                (migrated, reference, self.instance_id, ciphertext),
            )
        return value.decode()

    def discard(self, reference):
        self.retire([reference])

    def retire(self, references):
        """Remove known retired/prepared objects, never another writer's pending reference."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM config_overlay WHERE instance_id=?", (self.instance_id,),
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            live = {value for key, value in current.items() if key.endswith(".llm_api_key")}
            for reference in references:
                if reference not in live:
                    db.execute(
                        "DELETE FROM model_credentials WHERE reference=? AND instance_id=?",
                        (reference, self.instance_id),
                    )
