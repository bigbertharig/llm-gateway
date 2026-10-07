# DeepSeek Harness Research

Research date: 2026-08-14

Official project: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)

## Summary

DeepSeek Harness is an agent runtime and user interface for coordinating model-backed agents, tools, sessions, and delegated work. It can use cloud models or self-hosted models exposed through supported APIs, including OpenAI-compatible endpoints.

It is adjacent to our County Map supervision design, but it is not a GPU-rig control plane. Harness operates at the provider, model, endpoint, agent, and session layers. It does not appear to discover GPUs, load models, allocate VRAM, manage inference-server ports, or unload idle models.

The practical division of responsibility would be:

```text
DeepSeek Harness
  UI and sessions
  coordinator agent
  child-agent delegation
  tools and plugins
  context compaction
  agent messaging and lifecycle

llm_orchestration shared runtime
  GPU discovery and leases
  model promotion and eviction
  model loading and unloading
  inference-server processes
  port ownership and reclamation
  health and thermal monitoring
  endpoint registration
```

Harness is currently a developer preview and its documentation warns that breaking changes should be expected. It is best treated as an adjacent project to monitor rather than an immediate dependency.

## Coordinator and Worker Agents

Harness has the mechanisms needed to run a coordinator with multiple child agents:

- The coordinator may use a cloud model or a capable local model.
- Self-hosted workers can be configured as custom providers and model endpoints.
- Child agents may use different providers and models.
- The coordinator can delegate work, send follow-up messages, interrupt agents, and inspect agent state.
- Children can be one-shot or continuable.
- Multiple distinct children can run concurrently, subject to provider isolation and configured capacity.
- Each child can have its own tool restrictions, persona, working directory, provider, and model.

The orchestration intelligence still comes from the coordinator model. Harness provides the mechanisms for delegation and lifecycle management, but it does not make a weak local model intrinsically good at decomposition, supervision, or tool use. A strong cloud coordinator would likely supervise specialized local workers more reliably than a small local coordinator.

References:

- [Subagent subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md)
- [Provider configuration](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md)
- [Harness architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)

## What Harness Does Not Manage

Harness sees configured providers, models, endpoints, and agents. It does not appear to understand the physical GPU placement underneath an endpoint.

It does not natively provide the following rig-management functions:

- Discover GPUs and available VRAM.
- Select a model based on memory fit.
- Load or unload GGUF or other model files.
- Assign a model server to a particular GPU.
- Start and stop llama.cpp, vLLM, or equivalent inference runtimes as a rig authority.
- Manage exclusive GPU or port leases.
- Reclaim unexpected processes occupying worker ports.
- Promote models from archive storage to the local SSD hot set.
- Monitor temperatures and enforce rig-level safety policy.
- Manage split placement across multiple GPUs.

These responsibilities should remain in `llm_orchestration` Shared tooling. Harness could consume registered endpoints after the runtime layer has made them ready.

A possible topology is:

```text
Harness coordinator
  child A -> http://local-worker-1:11435/v1
  child B -> http://local-worker-2:11436/v1
  child C -> cloud provider

llm_orchestration
  lease GPU resources
  start worker models
  verify endpoint health
  publish provider/model routes
  stop workers according to explicit policy
```

## User Interface Maturity

The web interface supports workspaces, model configuration, sessions, tasks, tools, delegation, and approvals. Advanced coordinator/child topology appears to rely substantially on Cordis profiles, presets, patches, and plugins.

The current system should not be assumed to provide a turnkey visual workflow such as:

```text
GPU 1 -> load Qwen -> assign Agent A
GPU 2 -> load Gemma -> assign Agent B
Cloud model -> supervise both
```

That workflow would require an integration between Harness and our GPU lease and model-runtime services.

References:

- [Web UI guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/index.md)
- [Configuration catalog](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/config-catalog.md)
- [Profiles and bundles](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md#profiles-and-bundles)

## Sessions and Model Calls

Inference APIs are technically stateless: each HTTP request independently sends a prompt to the model endpoint. Harness provides logical conversation continuity by storing an append-only session event log and deriving the model-visible history from that log for each step.

A continuing agent session behaves approximately like this:

```text
request 1: system + user
request 2: system + user + assistant + tool call + tool result
request 3: accumulated model-visible history
```

Therefore, each tool step is a new inference request but not necessarily a new logical chat. Harness reconstructs the relevant conversation history before making the request.

A newly spawned one-shot child normally starts with fresh child context containing its assignment and configured instructions. A continuable child has its own durable session and can receive later messages from the coordinator. Coordinator and child contexts are separate and are compacted independently.

Reference: [Architecture and turn flow](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md#turn-flow)

## Context-Window Management

Harness includes an optional compaction capability, with automatic behavior enabled by default in the basic compaction provider.

Under normal token pressure it can:

1. Estimate the assembled request size.
2. Trigger compaction at a fraction of the routed model's context capacity.
3. Optionally prune oversized tool results without a model call.
4. Select an older balanced range of the conversation.
5. Ask a configured model to summarize that range.
6. Replace the old model-visible range with a summary checkpoint.
7. Preserve a recent portion of the conversation verbatim.
8. Keep the original durable event history even though the model-visible projection is compacted.

Default basic-compaction policy:

- `thresholdRatio: 0.8`
- `retainRatio: 0.16`
- `maxTokens: 8192` for the summarization call
- `compactionRetries: 1` extra attempt
- `maxOverflowRetries: 1`
- `auto: true`

For a correctly configured 32K model, the ratio defaults imply approximately:

```text
Context capacity:       32,000 tokens
Compaction threshold:   25,600 tokens
Recent verbatim budget:  5,120 tokens
Older selected history: replaced by a summary checkpoint
```

The exact calculation uses the configured routed context window. A deployment using 32,768 rather than 32,000 tokens would produce correspondingly different absolute values.

References:

- [Compaction subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/compaction.md)
- [Basic compaction provider](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)
- [Tool-result pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)

## Overflow Recovery

Harness also has a recovery path for a provider-confirmed context-window overflow:

- It can bypass the normal pressure threshold.
- It can prune oversized tool results first when the optional pruner is installed.
- It attempts a larger balanced reduction while retaining the newest indivisible unit.
- It can rebuild and retry the failed request after the conversation surface advances.
- The default maximum overflow retry count is one.

There are limitations:

- The adapter must recognize and normalize the provider's overflow error.
- A custom local server may return an error format Harness does not recognize.
- Compaction cannot shrink an oversized system prompt or tool-schema envelope.
- It cannot always split an indivisible message or tool-call/result pair.
- A summary may omit details that were present in the original history.
- Token metering may be estimated rather than exactly tokenized for some providers.

Context compaction is therefore a practical pressure-management mechanism, not perfect long-term memory.

## Required Local-Model Configuration

Every local model route should advertise its real combined context capacity and a conservative output cap. The relevant model metadata resembles:

```yaml
models:
  - id: local-worker
    contextWindow: 32768
    maxTokens: 4096
```

If context-capacity metadata is missing, normal automatic pressure compaction may be unable to resolve an absolute threshold. Harness may still react to a recognized provider overflow, but waiting for a failed request is less reliable.

Small-context workers should have explicit per-model policies. For example:

```yaml
- name: '@deepseek-ai/dsh-compaction-basic'
  config:
    thresholdRatio: 0.8
    retainRatio: 0.16
    modelPolicies:
      - provider: local
        model: small-context
        thresholdRatio: 0.7
        retainTokens: 2048
```

Reference: [Harness configuration catalog](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/config-catalog.md)

## Implications for Our System

Harness already handles ordinary session continuity, child-session separation, context pressure, summarization checkpoints, and some overflow recovery. We would not need to build those mechanisms merely to connect a local endpoint.

We would still need to enforce the following on our side:

- Correct model and endpoint registration.
- Accurate context-window and output-limit metadata.
- Explicit GPU, process, and port ownership.
- Bounded tool outputs.
- Durable plans, files, checkpoints, and structured artifacts for important state.
- Worker cold-start and warm-up policy.
- Health checks before exposing an endpoint to Harness.
- Clear failure behavior when a requested worker cannot be loaded.

Important task state should not exist only in conversation history. Compaction summaries are intentionally lossy. Plans, decisions, test results, and handoff state should remain in durable workspace artifacts that a fresh or compacted agent can inspect.

## Assessment

DeepSeek Harness is sufficiently developed to coordinate agents after model endpoints have been provisioned. It is not a replacement for our shared GPU and runtime control plane.

Its most relevant ideas for us are:

- A polished common UI over multiple agent sessions.
- Provider-independent coordinator and child model selection.
- Durable event-backed sessions.
- Continuable child agents and explicit agent lifecycle controls.
- Tool and plugin sharing.
- Context compaction based on each routed model's declared capacity.
- Separation between the agent interface and replaceable implementation plugins.

The clearest future integration boundary would be a Harness plugin or provider that requests workers from `llm_orchestration`, waits for a healthy registered endpoint, delegates work, and releases the worker according to an explicit policy. Until the project stabilizes, its architecture and UI are more useful as reference material than as a dependency to adopt.
