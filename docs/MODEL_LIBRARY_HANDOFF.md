# Local-model evidence display

Orchestration imports optional Benchmarking evidence, resolves local profiles and publishes
the current model offers and evidence summary. Gateway renders that service response.
Gateway no longer owns `model_catalog.json`, `remote_profiles.json`, their generator or
load-observation history. Model roles are resolved by an Orchestration request.

Configure the connection in [standalone setup](STANDALONE.md). Import and runtime policy
belong in the [Orchestration control-plane guide](https://github.com/bigbertharig/llm_orchestration/blob/main/docs/CONTROL_PLANE.md).
