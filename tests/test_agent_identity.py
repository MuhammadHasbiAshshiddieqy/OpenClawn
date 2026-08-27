"""Tests untuk core/agent_identity.py — Non-Human Identity (TODO.md § Prioritas 9.2)."""

from core.agent_identity import agent_identity, config_hash


def _soul(**overrides) -> dict:
    base = {
        "meta": {"role": "dev", "name": "Dev Agent"},
        "system_prompt": {"content": "You are a dev agent."},
        "tools": {"allowed": ["file_read", "file_write", "code_run"]},
        "routing": {"prefer_local": False, "upgrade_keywords": ["refactor"]},
    }
    base.update(overrides)
    return base


def test_same_config_produces_same_hash():
    assert config_hash(_soul()) == config_hash(_soul())


def test_different_content_produces_different_hash():
    soul_a = _soul()
    soul_b = _soul(system_prompt={"content": "You are a DIFFERENT dev agent."})
    assert config_hash(soul_a) != config_hash(soul_b)


def test_tool_allowlist_change_produces_different_hash():
    """Perubahan permission (bukan cuma prompt) HARUS mengubah identitas — ini
    inti alasan fitur ini ada."""
    soul_a = _soul()
    soul_b = _soul(tools={"allowed": ["file_read", "file_write", "code_run", "shell_run"]})
    assert config_hash(soul_a) != config_hash(soul_b)


def test_policy_section_change_produces_different_hash():
    soul_a = _soul()
    soul_b = _soul(
        policy={"file_write": {"deny_if": [{"field": "path", "op": "prefix", "value": "/etc"}]}}
    )
    assert config_hash(soul_a) != config_hash(soul_b)


def test_key_order_does_not_affect_hash():
    """TOML tak menjamin urutan key bermakna — canonical JSON (sort_keys) harus
    membuat urutan dict berbeda tetap menghasilkan hash SAMA."""
    soul_a = {"a": 1, "b": 2, "c": {"x": 1, "y": 2}}
    soul_b = {"c": {"y": 2, "x": 1}, "b": 2, "a": 1}
    assert config_hash(soul_a) == config_hash(soul_b)


def test_config_hash_is_sha256_hex():
    h = config_hash(_soul())
    assert len(h) == 64
    int(h, 16)  # raises ValueError kalau bukan hex valid


def test_agent_identity_format():
    identity = agent_identity("dev", _soul())
    role, _, short_hash = identity.partition("@")
    assert role == "dev"
    assert len(short_hash) == 12
    assert short_hash == config_hash(_soul())[:12]


def test_agent_identity_stable_across_calls():
    """Panggilan berulang dengan config identik → identitas identik (lintas
    'restart', karena tak ada state tersimpan di modul ini)."""
    soul = _soul()
    assert agent_identity("dev", soul) == agent_identity("dev", soul)


def test_agent_identity_differs_by_role_even_with_same_config():
    """Dua role dengan isi soul.toml PERSIS sama (hipotetis) tetap harus
    menghasilkan identitas berbeda — role adalah bagian identitas, bukan cuma
    fingerprint konten."""
    soul = _soul()
    assert agent_identity("dev", soul) != agent_identity("qa", soul)


def test_agent_identity_changes_when_config_changes():
    soul_v1 = _soul()
    soul_v2 = _soul(tools={"allowed": ["file_read"]})  # permission dicabut
    assert agent_identity("dev", soul_v1) != agent_identity("dev", soul_v2)
