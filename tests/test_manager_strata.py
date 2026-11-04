"""Registered Strata placement uses the measured rig profile and preserves the tier order."""
from llm_gateway.manager.core import Manager
from llm_gateway.manager import models


def test_qwen38_registered_pack_plans_on_gpu0_and_checks_host_headroom():
    manager = Manager.__new__(Manager)
    r = models.resolve("qwen3.8-flash-next:q2_0", "benchmark")
    gpu = {"index": 0, "total_mb": 24564, "used_mb": 19}
    args = {"ctx": 8192, "ctx_min": 4096, "context_mode": "working", "cards": None, "gpu_ids": None}
    good = manager._strata_plan(r, {"guard": {"mem_available_mb": 18000}}, [gpu], **args)
    assert good["fits"] and good["placement"] == "strata" and good["gpus"] == [0]
    assert good["total_size_bytes"] == 66423878624
    assert not manager._strata_plan(r, {"guard": {"mem_available_mb": 6000}}, [gpu], **args)["fits"]
    assert not manager._strata_plan(r, {"guard": {"mem_available_mb": 18000}}, [], **args)["fits"]
    assert not manager._strata_plan(r, {"guard": {"mem_available_mb": 18000}}, [gpu],
                                    **{**args, "gpu_ids": [1]})["fits"]


def test_qwen36_strata_context_is_bounded_by_validated_probe():
    manager = Manager.__new__(Manager)
    r = models.resolve("qwen3.6:35b-a3b-xl", "benchmark")
    gpu = {"index": 0, "total_mb": 24564, "used_mb": 19}
    args = {"ctx_min": 4096, "context_mode": "working", "cards": None, "gpu_ids": None}
    assert manager._strata_plan(r, {"guard": {"mem_available_mb": 18000}}, [gpu],
                                ctx=8192, **args)["fits"]
    assert not manager._strata_plan(r, {"guard": {"mem_available_mb": 18000}}, [gpu],
                                    ctx=16384, **args)["fits"]
