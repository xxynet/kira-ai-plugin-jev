# JEV Reply Trigger

This repository contains the standalone `kira-ai-plugin-jev` plugin for KiraAI 3.x.
The framework lives in a separate checkout; do not modify its source for plugin tasks.

## Development

- Plugin entry point: `main.py`; metadata: `manifest.json`; configuration: `schema.json`.
- Reuse `ctx.message_history` and `ctx.persona_mgr` for framework data.
- Send only active persona name and content; exclude all three chat-rule fields.
- Preserve the hook order before chat handlers and the no-retry behavior.
- Use `core.plugin.logger`; do not log keys, request/response bodies, chat content, or private persona data.
- Keep default English and `locales.zh` configuration names and hints synchronized.
- Write new code comments in English.

## Local checks

Use Python 3.10 or later with the KiraAI checkout's dependencies installed.
Run commands on the host, not in a sandbox. Use PowerShell to edit files.

From this plugin directory:

```text
python -m pip install -r requirements-dev.txt
python -m pytest
```

Tests discover the enclosing framework checkout. For a standalone clone, set
`KIRAAI_ROOT` to the framework root and install its `requirements.txt` first.
Tests mock TypeSafe HTTP calls and isolate runtime files in a temporary directory.

## Repository scope

- No CI workflows are configured; keep validation local unless requested otherwise.
- Check Git status before edits and preserve existing user changes.
- Do not commit, push, or configure a remote unless requested.
- Create feature/fix branches with `feat/` or `fix/`, not `codex/`.
- The expected GitHub login is `xxynet`; use host `git` and `gh`, not a GitHub connector.
- The default integration branch for this plugin repository is `main`.
- Read `.github/PULL_REQUEST_TEMPLATE.md` before creating a PR and use `gh pr create`.
