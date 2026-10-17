# kira-ai-plugin-jev

Judges every incoming message with TypeSafe's **Jev** model and marks messages
worth replying to as mentioned, so the chat plugin picks them up.

Requires **KiraAI 3.0.0 or later** and its `ctx.message_history` service.

Jev is a *System One* model, not a text LLM: it does not generate answers to
parse. The plugin calls the native `POST /v1/systemone` endpoint with a
`noul` question and consumes the typed response (a calibrated 0–1 probability
for "should the bot reply") directly.

## How it works

- Registers an `on.im_message` hook at `Priority.HIGH + 10`: after
  system-level guards (`SYS_HIGH`) and before chat plugins, whose handlers
  run at `HIGH` and read `is_mentioned`.
- Skips notice events, self messages, and messages that are already
  mentioned — Jev is only consulted for messages that would otherwise be
  ignored (typically unmentioned group messages).
- Sends one `systemone` request per message:
  - `state.persona` — the currently active persona's `name` and full `content`,
    read asynchronously through `ctx.persona_mgr.get_persona()` on every
    judgement so persona switches take effect immediately. Global, private,
    and group chat rules are never included in this field;
  - `state.target` — the current message as `发送者：内容〔群聊：群名〕`
    (or `〔私聊〕`), truncated to 2000 chars;
  - `state.conversation` — recent archived session messages within a
    12000-char budget, retaining the newest lines in chronological order.
    By default, up to 20 records are read asynchronously through
    `ctx.message_history`, configurable with `history_limit` (1–200),
    scoped to the session and bot account. The current archived message is
    used as a cursor to exclude it and concurrently newer messages. Incoming
    group messages retain sender names; sent bot replies use `机器人：` and
    private user messages use `对方：`. Notices, self messages, and outgoing
    messages without confirmed delivery are excluded;
  - `questions.should_reply` — a `noul` question with per-session-type
    instructions (`group_judge_prompt` / `private_judge_prompt`). Guidance
    about `state.persona` and a prompt-injection guard are appended automatically,
    including for custom prompts. Persona text is treated as identity data;
    conversation text is treated as untrusted data, not evaluation instructions.
- Triggers a reply (`event.message.is_mentioned = True`) when the returned
  `noul` probability is at least `threshold` (default 0.5).
- The answer is strictly validated (`type == "noul"`, finite value in
  [0, 1]); on timeout, HTTP error, or an invalid answer the message stays
  unmentioned (fails closed, no spurious replies). No retries, to avoid
  message backlog.

## Configuration

| Key                   | Type      | Default                 | Description                                                      |
| --------------------- | --------- | ----------------------- | ---------------------------------------------------------------- |
| `api_key`             | sensitive | (empty)                 | TypeSafe API key (console.typesafe.ai/keys). Required.            |
| `base_url`            | string    | `https://api.typesafe.ai/v1` | System One API base URL.                                     |
| `model`               | string    | `jev-latest`            | TypeSafe model id (not a KiraAI provider model).                  |
| `threshold`           | float     | `0.5`                   | Minimum noul probability (0–1) that triggers a reply.             |
| `history_limit`       | integer   | `20`                    | Max archive records queried (1–200); clamped to range, invalid values use 20. Filtering and char budget may reduce the included count. |
| `conversation_budget` | integer   | `12000`                 | Char budget of the `state.conversation` window; 0 disables it.    |
| `timeout`             | float     | `8.0`                   | Request timeout in seconds.                                       |
| `group_judge_prompt`  | textarea  | (built-in)              | noul instructions for group messages.                             |
| `private_judge_prompt`| textarea  | (built-in)              | noul instructions for private messages.                           |

## Notes

- The judge runs inline in the message pipeline, so each unmentioned message
  adds one System One round-trip (bounded by `timeout`) before the chat
  plugin buffers it.
- The conversation window uses the framework's message archive, including
  incoming messages still waiting in the buffer. It does not append the buffer
  or read LLM memory, so messages are not duplicated or limited to retained
  memory chunks. Archive retention settings determine which history is available.
- If history cannot be read, judging continues with the current message and
  active persona. Setting `conversation_budget` to 0 skips history queries
  entirely; persona data is still included. Persona text is not truncated by
  the conversation or per-message limits. If persona lookup fails, judging is
  skipped without changing the mention flag. No active persona produces empty
  persona fields.
- Voice/image-only messages are judged from their element placeholders, since
  media transcription happens later inside the chat plugin.

## Troubleshooting HTTP failures

HTTP errors log the numeric status and a fixed diagnostic hint, for example:

```text
[Jev] Judge request failed | HTTP 401 | Check the TypeSafe API key and whether it is valid for this endpoint.
```

- Verify `base_url` is `https://api.typesafe.ai/v1` and `model` is `jev-latest`
  when using the official API. The plugin appends `/systemone` to the base URL.
- `401`: check the TypeSafe API key. `403`: check permissions and network access.
- `404` or `405`: check the base URL and any reverse proxy routing.
- `400` or `422`: check the request against the API contract. A documented
  `max_tokens_exceeded` error is identified separately as a context overflow.
  Full persona content also consumes context, independently of the history budget.
- `451`: unavailable for legal reasons; check provider regional access policy and
  any gateway blocking. The status alone does not identify which system blocked it.
- `429`: check account rate limits and message frequency. `529`: service overload.

Status hints are troubleshooting guidance, not proof of the upstream cause.
Errors preserve the mention flag and are not retried. Logs contain the status and
locally defined hint, without response bodies, request contents, or credentials.
See the [official API reference](https://docs.typesafe.ai/api) for status meanings.
## Local development

This is a KiraAI plugin, not a standalone application. It requires Python 3.10+
with a KiraAI 3.x source checkout and that checkout's dependencies installed.
The plugin's runtime dependency is listed in `requirements.txt`.

When this repository is under `KiraAI/data/plugins/kira-ai-plugin-jev`, run from
the plugin directory:

```text
python -m pip install -r requirements-dev.txt
python -m pytest
```

For a standalone clone, point tests to the framework checkout. PowerShell example:

```powershell
$env:KIRAAI_ROOT = 'D:\path\to\KiraAI'
python -m pip install -r "$env:KIRAAI_ROOT/requirements.txt"
python -m pip install -r requirements-dev.txt
python -m pytest
```

On POSIX shells, set `export KIRAAI_ROOT=/path/to/KiraAI` before the same install
and test commands, using `"$KIRAAI_ROOT/requirements.txt"` for the framework list.
Tests mock all TypeSafe requests and use a temporary runtime directory; no API key
is needed. Validation runs locally; this repository has no CI workflows.

Before sharing changes, check `git status` and the diff. Keep credentials, runtime
data, test caches, and local virtual environments out of version control.
