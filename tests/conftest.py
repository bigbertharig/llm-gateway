import pytest

@pytest.fixture(autouse=True)
def cloud_state(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CLOUD_SETTINGS_ROOT', str(tmp_path / 'cloud'))
