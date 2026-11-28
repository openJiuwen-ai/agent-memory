"""Grants are versioned and the access seam never follows a symlink outside them."""

from __future__ import annotations

import os

import pytest

from jiuwen_memory_entry.local.sync.access import AccessDenied, SourceAccess
from jiuwen_memory_entry.local.sync.grant import change_grant, new_grant

pytestmark = pytest.mark.unit


def test_grant_version_changes_but_identity_is_stable(tmp_path):
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    version = grant.version
    changed = change_grant(grant, roots=[str(tmp_path / "other")])
    assert changed.version == version + 1
    assert changed.grant_id == grant.grant_id
    assert changed.roots == (str(tmp_path / "other"),)


def test_access_rejects_symlink_escape_and_records_it(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    os.symlink(outside, link)
    grant = new_grant([str(root)], types={"fact"}, target_space="init")
    access = SourceAccess(grant)
    with pytest.raises(AccessDenied):
        access.open_text(link, kind="fact")
    assert access.opened == []
    assert access.skipped and "escapes" in access.skipped[0][1]


def test_access_rejects_unauthorized_type(tmp_path):
    path = tmp_path / "MEMORY.md"
    path.write_text("fact", encoding="utf-8")
    grant = new_grant([str(tmp_path)], types={"fact"}, target_space="init")
    access = SourceAccess(grant)
    with pytest.raises(AccessDenied):
        access.open_text(path, kind="profile")
    assert access.opened == []
