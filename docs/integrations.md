# Integration inventory

- Muse: the Desktop muse runtime monitor reads status and health data. It is not a verified job-submission interface. The Deskhand handoff explicitly says the Muse connector is not verified. Coordinator is unavailable.
- GPT / ChatGPT: no supported account adapter, account usage interface, or effort control has been established. No browser automation or paid API fallback is enabled.
- Ollama: the local service was started and `/api/tags` returned qwen3.5:4b, qwen3.5:9b, qwen3.5:27b, qwen3.8:27b, and embeddinggemma:latest on 2026-11-13. A short manual inference test against qwen3.5:4b returned the requested response. The app can list models and send an explicitly entered manual question; worker/reviewer execution is not enabled. See the official [list models](https://docs.ollama.com/api/tags) and [chat](https://docs.ollama.com/api/chat) documentation.
- Jetstream Financial and shared memory: no interface was identified during the initial Desktop inventory. Switcher and shared memory are unavailable.
- Usage: unknown for all cloud providers. Unknown balances are excluded from routine routing; no 20% reserve can be claimed as enforced without a trustworthy balance.

Recheck integrations before enabling a provider. Do not infer support from this inventory.
