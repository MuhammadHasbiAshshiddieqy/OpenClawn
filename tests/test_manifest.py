"""Test untuk infra/manifest.py — clawn.yaml sebagai lapisan deklaratif DI ATAS
soul.toml (TODO.md § Prioritas 3). PyYAML dipilih untuk PARSING clawn.yaml;
PENULISAN ke soul.toml pakai text-based section replace (bukan serializer TOML
generik) — system_prompt & section lain HARUS tetap byte-identik, hanya
[policy.*] yang disentuh.
"""

import pytest

from infra.manifest import (
    ManifestError,
    apply_manifest,
    generate_policy_toml_block,
    load_manifest,
)


def test_load_manifest_parses_team_roles(tmp_path):
    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text(
        """
team:
  pm:
    policy:
      pdf_write:
        approval_required_if:
          - field: content
            op: contains
            value: confidential
"""
    )
    manifest = load_manifest(str(manifest_path))
    assert "pm" in manifest["team"]
    assert manifest["team"]["pm"]["policy"]["pdf_write"]["approval_required_if"][0]["value"] == (
        "confidential"
    )


def test_load_manifest_missing_file_raises_manifest_error(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(str(tmp_path / "does_not_exist.yaml"))


def test_load_manifest_invalid_yaml_raises_manifest_error(tmp_path):
    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text("team: [unclosed")
    with pytest.raises(ManifestError):
        load_manifest(str(manifest_path))


def test_load_manifest_missing_team_key_raises_manifest_error(tmp_path):
    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text("not_team: {}")
    with pytest.raises(ManifestError):
        load_manifest(str(manifest_path))


def test_generate_policy_toml_block_renders_deny_if():
    policy = {"file_write": {"deny_if": [{"field": "path", "op": "prefix", "value": "/etc"}]}}
    block = generate_policy_toml_block(policy)
    assert "[policy.file_write]" in block
    assert 'field = "path"' in block
    assert 'op = "prefix"' in block
    assert 'value = "/etc"' in block


def test_generate_policy_toml_block_renders_approval_required():
    policy = {"shell_run": {"approval_required_if": [{"op": "always"}]}}
    block = generate_policy_toml_block(policy)
    assert "[policy.shell_run]" in block
    assert "approval_required_if" in block
    assert 'op = "always"' in block


def test_generate_policy_toml_block_handles_numeric_value():
    policy = {"http_request": {"deny_if": [{"field": "timeout", "op": "gt", "value": 300}]}}
    block = generate_policy_toml_block(policy)
    assert "value = 300" in block  # angka TANPA quote, beda dari string


def test_generate_policy_toml_block_empty_policy_returns_empty_string():
    assert generate_policy_toml_block({}) == ""


def _write_soul(path, content):
    path.write_text(content)


def test_apply_manifest_appends_policy_to_soul_without_existing_policy(tmp_path):
    """soul.toml TANPA section [policy] sama sekali → block baru ditambahkan,
    system_prompt & section lain tetap byte-identik."""
    roles_dir = tmp_path / "roles" / "pm"
    roles_dir.mkdir(parents=True)
    soul_path = roles_dir / "soul.toml"
    original = (
        '[meta]\nrole = "pm"\n\n'
        '[system_prompt]\ncontent = """\nHello multi-line\nwith "quotes" too\n"""\n\n'
        '[tools]\nallowed = ["file_read"]\n'
    )
    _write_soul(soul_path, original)

    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text(
        """
team:
  pm:
    policy:
      file_write:
        deny_if:
          - {field: path, op: prefix, value: /etc}
"""
    )

    apply_manifest(str(manifest_path), roles_dir=str(tmp_path / "roles"))

    updated = soul_path.read_text()
    assert "Hello multi-line" in updated
    assert 'with "quotes" too' in updated
    assert '[tools]\nallowed = ["file_read"]' in updated
    assert "[policy.file_write]" in updated
    assert 'value = "/etc"' in updated


def test_apply_manifest_replaces_existing_policy_section(tmp_path):
    """soul.toml SUDAH punya [policy.*] dari apply sebelumnya → diganti bersih
    (bukan menumpuk duplikat), section lain tetap tidak tersentuh."""
    roles_dir = tmp_path / "roles" / "dev"
    roles_dir.mkdir(parents=True)
    soul_path = roles_dir / "soul.toml"
    original = (
        '[meta]\nrole = "dev"\n\n'
        "[policy.old_tool]\n"
        'deny_if = [{ field = "x", op = "eq", value = "y" }]\n\n'
        '[contract]\noutput_type = "DevOutput"\n'
    )
    _write_soul(soul_path, original)

    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text(
        """
team:
  dev:
    policy:
      new_tool:
        deny_if:
          - {field: a, op: eq, value: b}
"""
    )

    apply_manifest(str(manifest_path), roles_dir=str(tmp_path / "roles"))

    updated = soul_path.read_text()
    assert "[policy.new_tool]" in updated
    assert "[policy.old_tool]" not in updated  # diganti, bukan ditumpuk
    assert '[contract]\noutput_type = "DevOutput"' in updated  # section lain utuh


def test_apply_manifest_role_not_in_manifest_leaves_soul_untouched(tmp_path):
    """Role yang TIDAK disebut di clawn.yaml team{} → soul.toml-nya tak disentuh
    sama sekali (opt-in per-role, bukan all-or-nothing). Role LAIN (pm) yang
    memang disebut manifest tetap diproses seperti biasa di request yang sama."""
    qa_dir = tmp_path / "roles" / "qa"
    qa_dir.mkdir(parents=True)
    qa_soul = qa_dir / "soul.toml"
    original = '[meta]\nrole = "qa"\n'
    _write_soul(qa_soul, original)

    pm_dir = tmp_path / "roles" / "pm"
    pm_dir.mkdir(parents=True)
    _write_soul(pm_dir / "soul.toml", '[meta]\nrole = "pm"\n')

    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text("team:\n  pm:\n    policy: {}\n")  # tidak menyebut 'qa'

    apply_manifest(str(manifest_path), roles_dir=str(tmp_path / "roles"))

    assert qa_soul.read_text() == original


def test_apply_manifest_missing_soul_file_raises_manifest_error(tmp_path):
    """Role disebut di manifest tapi soul.toml-nya tidak ada — error jelas,
    bukan crash generik atau membuat file baru diam-diam."""
    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text("team:\n  ghost_role:\n    policy: {}\n")

    with pytest.raises(ManifestError):
        apply_manifest(str(manifest_path), roles_dir=str(tmp_path / "roles"))


def test_apply_manifest_role_without_policy_key_is_noop_for_that_role(tmp_path):
    """Role ada di manifest tapi tanpa key 'policy' (mis. hanya 'model') →
    tidak menambah/menghapus section [policy.*] apa pun di soul.toml-nya."""
    roles_dir = tmp_path / "roles" / "pm"
    roles_dir.mkdir(parents=True)
    soul_path = roles_dir / "soul.toml"
    original = '[meta]\nrole = "pm"\n'
    _write_soul(soul_path, original)

    manifest_path = tmp_path / "clawn.yaml"
    manifest_path.write_text("team:\n  pm:\n    model: gemini-2.5-flash\n")

    apply_manifest(str(manifest_path), roles_dir=str(tmp_path / "roles"))

    assert soul_path.read_text() == original
