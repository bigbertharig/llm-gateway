"""Transport regression checks for rigctl manager actions."""

import importlib.util
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "rigctl", Path(__file__).resolve().parents[1] / "tools" / "rigctl.py"
)
rigctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rigctl)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return b"{}"


def test_bodyless_post_sends_empty_json_object():
    with patch.object(rigctl.urllib.request, "urlopen", return_value=Response()) as open_url:
        code, _ = rigctl.call("POST", "/v1/manager/profiles/demo/unload")
    assert code == 200
    assert open_url.call_args.args[0].data == b"{}"


def test_get_has_no_request_body():
    with patch.object(rigctl.urllib.request, "urlopen", return_value=Response()) as open_url:
        code, _ = rigctl.call("GET", "/v1/manager/status")
    assert code == 200
    assert open_url.call_args.args[0].data is None