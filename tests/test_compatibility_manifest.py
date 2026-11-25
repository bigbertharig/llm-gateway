"""An optional tested set warns on revisions and gates only used connections."""
import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools/check_compatibility.py"
spec = importlib.util.spec_from_file_location("check_compatibility", MODULE)
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)

TESTED = {
    "schema": compat.SCHEMA,
    "revisions": {product: "a" * 40 for product in compat.PRODUCTS},
    "artifacts": {"model-library": "b" * 64, "tool-catalog": "c" * 64},
}


def test_revision_mismatch_warns_without_blocking_live_capability():
    result = compat.assess(TESTED, {"llm-gateway": "d" * 40},
                           {"model-library": "b" * 64}, ["model-library"], True)
    assert result["blocked"] == []
    assert result["capabilities"]["model-library"] == "compatible"
    assert result["warnings"] == ["llm-gateway revision differs from tested set"]


def test_hash_mismatch_blocks_only_requested_live_capability():
    result = compat.assess(TESTED, {}, {"model-library": "d" * 64,
                                        "tool-catalog": "c" * 64},
                           ["model-library", "tool-catalog"], True)
    assert result["blocked"] == ["model-library"]
    assert result["capabilities"]["tool-catalog"] == "compatible"
    plan = compat.assess(TESTED, {}, {"model-library": "d" * 64},
                         ["model-library"], False)
    assert plan["blocked"] == []


def test_missing_optional_connection_does_not_affect_standalone():
    result = compat.assess(TESTED, {}, {}, [], True)
    assert result["standalone_allowed"]
    assert result["blocked"] == []
    missing = compat.assess(TESTED, {}, {}, ["tool-catalog"], True)
    assert missing["blocked"] == ["tool-catalog"]



def test_model_library_hash_is_canonical_and_tamper_checked(tmp_path):
    import hashlib
    import json
    payload = {"schema": "gpu-rig-model-library/1", "models": [{"name": "fixture"}]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode()).hexdigest()
    path = tmp_path / "library.json"
    path.write_text(json.dumps({"source_sha256": digest, **payload}))
    assert compat.artifact_hash("model-library", path) == digest
    path.write_text(json.dumps({"source_sha256": digest, **{**payload, "models": []}}))
    import pytest
    with pytest.raises(ValueError, match="invalid model-library source hash"):
        compat.artifact_hash("model-library", path)
