from llm_gateway.settings import rig_command_from_env


def test_site_target_builds_rig_command(monkeypatch):
    monkeypatch.delenv("RIG_COMMAND", raising=False)
    monkeypatch.setenv("RIG_SSH_TARGET", "operator@192.168.50.3")
    command = rig_command_from_env()
    assert command[0] == "ssh"
    assert command[-2:] == ("operator@192.168.50.3", "/usr/local/bin/rig")


def test_explicit_command_still_wins(monkeypatch):
    monkeypatch.setenv("RIG_SSH_TARGET", "operator@192.168.50.3")
    monkeypatch.setenv("RIG_COMMAND", "rig --example")
    assert rig_command_from_env() == ("rig", "--example")
