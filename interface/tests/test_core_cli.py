"""CoreCliClient tests: envelope shapes, TTL spawn discipline, fail-closed
availability, and the strict M2 read whitelist.

The core binary is a FAKE: an executable python script under
interface/tests/fixtures/ that speaks the {ok, command, data, error}
envelope and can be told (via FAKE_CORE_MODE) to misbehave in the ways
a real core can.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interface.collectors.core_cli import CoreCliClient
from interface.snapshot import ServiceStatus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fake_core(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-stammtisch-core"
    binary.write_text((FIXTURES / "fake_core.py").read_text(encoding="utf-8"),
                      encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _client(fake: Path, root: Path, **kwargs) -> CoreCliClient:
    return CoreCliClient(root=root, binary=str(fake), **kwargs)


# ── envelope parsing ─────────────────────────────────────────────────


def test_ok_envelope_parsed_and_root_forwarded(fake_core: Path,
                                               tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    client = _client(fake_core, state_root)
    envelope = client.status()
    assert envelope.ok is True
    assert envelope.command == "status"
    assert envelope.error_message == ""
    assert envelope.returncode == 0
    # STAMMTISCH_HOME is how the root travels; the fake echoes it back.
    assert envelope.data["state_root"] == str(state_root)
    assert envelope.data["runs"] == []


def test_error_envelope_fails_closed(fake_core: Path, tmp_path: Path,
                                     monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_MODE", "error")
    envelope = _client(fake_core, tmp_path).status()
    assert envelope.ok is False
    assert envelope.returncode == 1
    assert "no run with id" in envelope.error_message
    assert envelope.data == {}


def test_garbage_output_fails_closed(fake_core: Path, tmp_path: Path,
                                     monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_MODE", "garbage")
    envelope = _client(fake_core, tmp_path).status()
    assert envelope.ok is False
    assert "unparseable JSON" in envelope.error_message


def test_nonzero_exit_without_envelope_uses_stderr(fake_core: Path,
                                                   tmp_path: Path,
                                                   monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_MODE", "exit")
    envelope = _client(fake_core, tmp_path).status()
    assert envelope.ok is False
    assert envelope.returncode == 3
    assert "human-readable failure" in envelope.error_message


def test_shape_violations_fail_closed() -> None:
    from interface.collectors import core_cli

    bad_payloads = [
        "[]",                        # not an object
        '{"command": "status"}',     # no boolean ok
        '{"ok": true, "data": []}',  # data not a dict
        '{"ok": false, "error": "text"}',  # error not a dict
        "",                          # no output at all
    ]
    for payload in bad_payloads:
        envelope = core_cli._parse_envelope("status", payload, "", 0)
        assert envelope.ok is False, payload


# ── TTL + spawn discipline ───────────────────────────────────────────


def test_ttl_single_spawn_per_window(fake_core: Path, tmp_path: Path) -> None:
    client = _client(fake_core, tmp_path)
    first = client.status(now=0.0)
    assert client.spawn_count == 1
    assert client.status(now=1.0) is first  # cached frame, same window
    client.status(now=14.9)
    assert client.spawn_count == 1
    second = client.status(now=15.0)  # window elapsed: exactly one respawn
    assert client.spawn_count == 2
    assert second.ok is True


def test_ttl_elapsed_window_respawns_once(fake_core: Path,
                                          tmp_path: Path) -> None:
    client = _client(fake_core, tmp_path)
    for tick in range(45):  # three 15s windows
        client.status(now=float(tick))
    assert client.spawn_count == 3


def test_spawn_budget_stops_spawning(fake_core: Path, tmp_path: Path) -> None:
    client = _client(fake_core, tmp_path, spawn_budget=1)
    assert client.status(now=0.0).ok is True
    assert client.spawn_count == 1
    exhausted = client.status(now=100.0)
    assert exhausted.ok is False
    assert "spawn budget exhausted" in exhausted.error_message
    assert client.spawn_count == 1  # budget held: no further spawn


def test_spawn_counting_crosschecked_via_log(fake_core: Path,
                                             tmp_path: Path,
                                             monkeypatch) -> None:
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("FAKE_CORE_LOG", str(log))
    client = _client(fake_core, tmp_path)
    for tick in (0.0, 5.0, 10.0, 20.0, 25.0):
        client.status(now=tick)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == client.spawn_count == 2


# ── fail-closed availability ─────────────────────────────────────────


def test_missing_binary_fails_closed_never_raises(tmp_path: Path) -> None:
    client = CoreCliClient(root=tmp_path, binary=str(tmp_path / "nowhere"))
    envelope = client.status()
    assert envelope.ok is False
    assert envelope.returncode == 127
    assert "binary not found" in envelope.error_message
    service = client.service_status()
    assert isinstance(service, ServiceStatus)
    assert (service.name, service.available) == ("core", False)


def test_service_status_available_with_binary(fake_core: Path,
                                              tmp_path: Path) -> None:
    service = _client(fake_core, tmp_path).service_status()
    assert service.name == "core"
    assert service.available is True
    assert str(fake_core) in service.detail


def test_default_binary_lookup_prefers_env(monkeypatch,
                                            tmp_path: Path) -> None:
    from interface.collectors import core_cli

    env_binary = tmp_path / "env-core"
    env_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    env_binary.chmod(0o755)
    monkeypatch.setenv("STAMMTISCH_BIN", str(env_binary))
    assert core_cli.find_core_binary() == str(env_binary)
    monkeypatch.setenv("STAMMTISCH_BIN", str(tmp_path / "missing"))
    # Unresolvable env falls through to build dirs / PATH, never to the
    # bogus env value itself.
    assert core_cli.find_core_binary() != str(tmp_path / "missing")


# ── strict whitelist ─────────────────────────────────────────────────


def test_write_ops_raise_not_implemented_and_never_spawn(
        fake_core: Path, tmp_path: Path) -> None:
    client = _client(fake_core, tmp_path)
    with pytest.raises(NotImplementedError):
        client.init()
    with pytest.raises(NotImplementedError):
        client.run("pipelines/examples/example.json")
    assert client.spawn_count == 0


def test_execute_rejects_non_whitelisted_op(fake_core: Path,
                                            tmp_path: Path) -> None:
    client = _client(fake_core, tmp_path)
    with pytest.raises(NotImplementedError, match="whitelist"):
        client._execute("reconcile", [])
    with pytest.raises(NotImplementedError, match="whitelist"):
        client._execute("inspect", ["example-run"])
    assert client.spawn_count == 0


# ── the one write op: delete (M5 audited path) ───────────────────────


def test_delete_spawns_with_exact_argv_and_ok_envelope(
        fake_core: Path, tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("FAKE_CORE_LOG", str(log))
    client = _client(fake_core, tmp_path)
    envelope = client.delete("example-run")
    assert envelope.ok is True
    assert envelope.command == "delete"
    assert envelope.data["removed"] is True, "core's verdict echoes"
    assert envelope.data["run_id"] == "example-run"
    assert envelope.error_message == ""
    assert client.spawn_count == 1
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("delete example-run --json"), (
        "one spawn, exact argv: op delete, args [run_id], --json")


def test_delete_is_never_cached_and_leaves_status_ttl_alone(
        fake_core: Path, tmp_path: Path) -> None:
    """Writes must always reach the core; the read lane's TTL accounting
    is unchanged around them (budget accounting pinned)."""
    client = _client(fake_core, tmp_path)
    client.status(now=0.0)          # 1 spawn (read)
    client.delete("run-a")          # 2 (write: never cached)
    client.delete("run-a")          # 3 (a repeat is a NEW spawn)
    client.status(now=1.0)          # cached: no spawn
    client.delete("run-b")          # 4
    assert client.spawn_count == 4


def test_delete_not_gated_on_the_read_spawn_budget(
        fake_core: Path, tmp_path: Path) -> None:
    """A confirmed delete must not be silently refused by the read lane's
    budget (its spawns still count for observability)."""
    client = _client(fake_core, tmp_path, spawn_budget=1)
    assert client.status(now=0.0).ok is True
    envelope = client.delete("example-run")
    assert envelope.ok is True
    assert client.spawn_count == 2


def test_delete_error_envelope_fails_closed(fake_core: Path, tmp_path: Path,
                                            monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_DELETE_FAIL", "ghost-run")
    envelope = _client(fake_core, tmp_path).delete("ghost-run")
    assert envelope.ok is False
    assert envelope.returncode == 1
    assert "no run with id 'ghost-run'" in envelope.error_message


def test_delete_mode_error_envelope(fake_core: Path, tmp_path: Path,
                                    monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_MODE", "error")
    envelope = _client(fake_core, tmp_path).delete("example-run")
    assert envelope.ok is False
    assert "no run with id" in envelope.error_message


def test_delete_empty_run_id_fails_closed_without_spawning(
        fake_core: Path, tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("FAKE_CORE_LOG", str(log))
    envelope = _client(fake_core, tmp_path).delete("")
    assert envelope.ok is False
    assert "no run id" in envelope.error_message
    assert not log.exists() or log.read_text(encoding="utf-8") == "", (
        "an empty id never spawns")


def test_delete_missing_binary_fails_closed_never_raises(
        tmp_path: Path) -> None:
    client = CoreCliClient(root=tmp_path, binary=str(tmp_path / "nowhere"))
    envelope = client.delete("example-run")
    assert envelope.ok is False
    assert envelope.returncode == 127
    assert "binary not found" in envelope.error_message


# ── the fake fixture itself ──────────────────────────────────────────


def test_fixture_refuses_non_whitelisted_ops(fake_core: Path) -> None:
    # The fake must refuse unsupported ops the way the real core would
    # (delete is now a supported op — the write lane under test above).
    import subprocess

    completed = subprocess.run([str(fake_core), "run", "x", "--json"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 3
    assert "unsupported op" in completed.stderr


def test_envelope_missing_command_field_is_rejected(tmp_path: Path) -> None:
    """Review A spec finding: {ok:true} without a command field used to
    pass with the op name substituted — strict means strict."""
    script = tmp_path / "fake-nocommand"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'ok': True, 'data': {'runs': []}}))\n",
        encoding="utf-8")
    script.chmod(0o755)
    client = CoreCliClient(root=tmp_path, binary=str(script))
    envelope = client._execute("status", [])
    assert envelope.ok is False
    assert "command" in (envelope.error_message or "")
