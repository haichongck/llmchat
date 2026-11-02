# llmchat — a lightweight local LLM chat UI with a built-in Ollama manager

[中文文档](README.md) · [![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A **single-page frontend + single-file backend** (Python standard library only, zero `pip install`). It puts *chatting* and *Ollama operations* on the same page.

**Why it exists:** Open WebUI is a great chat portal, but it cannot start/stop the Ollama service and has no per-model launch parameters. GPUStack manages instances it deploys itself. llmchat fills that gap — **service control + per-model launch parameters + VRAM visibility + chat with images**, all on `127.0.0.1`.

## Features

**Chat** — multi-session with local persistence, streaming, Markdown/tables/code, KaTeX math, collapsible reasoning, per-session system prompt, context compaction, live context-usage bar.

**Images** — click **＋**, drag & drop, or paste. Images are downscaled to **1024 px for the model** (below that OCR accuracy collapses) while history keeps a **256 px thumbnail** (~85% storage saved).

**Ollama manager** — service status and **start / stop / restart**, loaded models with VRAM and *effective* context plus **unload / pin**, model library with **load / delete**, **pull** with progress, GPU VRAM bars, last 40 log lines.

**Per-model launch parameters** — `num_ctx`, `num_gpu`, `num_batch`, `num_thread`, `temperature`, `top_p`, `top_k`, `repeat_penalty`, `num_predict`, `keep_alive`, plus arbitrary extra options. Stored server-side in `model-params.json`. Loading applies them; if the model is already resident it is **unloaded first** so the new parameters actually take effect. The chat header only *displays* the effective values, so nothing silently overrides your launch config.

**Custom models** — any OpenAI-compatible endpoint (vLLM, LM Studio, cloud APIs).

## Quick start

```bash
git clone <this-repo> llmchat && cd llmchat
python3 server.py            # http://127.0.0.1:17001
```

Requires Python 3.8+. Frontend libraries are vendored, so it works offline.

### Docker

```bash
docker build -t llmchat .
# host networking so the container can reach a loopback-only Ollama
docker run -d --name llmchat --network host \
  -e LLMCHAT_PORT=17101 -e LLMCHAT_SERVICE_CONTROL=0 llmchat
```

Or `docker compose up -d`. Inside a container there is no systemd, so service control is disabled; model management, parameters, VRAM and logs still work.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `LLMCHAT_PORT` | `17001` | listen port |
| `LLMCHAT_HOST` | `127.0.0.1` | listen address (do not expose publicly) |
| `LLMCHAT_ORIGIN` | `http://127.0.0.1:<PORT>` | same-origin check base (set when behind a reverse proxy) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `SYSTEMCTL_BIN` | `/usr/bin/systemctl` | systemctl path |
| `OLLAMA_UNIT` | `ollama` | systemd unit name |
| `LLMCHAT_ADMIN` | `1` | `0` disables the whole manager and `/__admin/*` |
| `LLMCHAT_SERVICE_CONTROL` | `1` | `0` disables start/stop/restart only |
| `LLMCHAT_PROXY_ALLOW` | `127.0.0.1,localhost,::1` | allowlist for the `/__proxy` relay |

## Enabling service control (optional)

```bash
sudo tee /etc/sudoers.d/llmchat-ollama >/dev/null <<'EOF'
youruser ALL=(root) NOPASSWD: /usr/bin/systemctl start ollama, /usr/bin/systemctl stop ollama, /usr/bin/systemctl restart ollama
EOF
sudo chmod 440 /etc/sudoers.d/llmchat-ollama && sudo visudo -c -f /etc/sudoers.d/llmchat-ollama
```

## Security model

- Loopback-only by default. Remote access → SSH tunnel: `ssh -L 17001:127.0.0.1:17001 user@host`
- `/__admin/*` requires `X-Admin-Token` (random per start) **and** an Origin/Referer same-origin check, and never returns wildcard CORS headers
- `/__proxy` has a destination-host allowlist to avoid being used as an SSRF pivot
- Stopping Ollama kills every local model (including embedding models) — the UI asks for confirmation
- No accounts, no multi-user isolation: single-user, single-machine by design

## Architecture

```
llmchat/
├── index.html        single-file frontend (~64 KB, UI + logic)
├── server.py         static server + /__proxy relay + /__admin API (~17 KB, stdlib only)
├── vendor/           marked / DOMPurify / KaTeX (offline)
├── scripts/smoke.sh  smoke test (used by CI)
└── model-params.json runtime-generated, gitignored
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).
