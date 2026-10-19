# fixtures

`interactive-2.1.285.jsonl` is the `hints-interactive` session recorded by a
probe gateway that logs every hook call (Claude Code 2.1.285, LiteLLM 1.102.0, GLM-5.2):
a plain question, a tool turn, a background subagent and its hand-back,
`/compact`, and one prompt after it.

One line per hook call, in order:
- `pre_call`: `model`, the `x-claude-code-*` headers and the last four messages.
- `success`: the reply's `content` and `tool_calls`, `request` (the line of the
  `pre_call` it answers) and that request's headers.

Edits from the recording: other headers dropped; home-directory paths replaced;
strings were cut at 300 characters by the probe, so a cut wrapper block such as
`<system-reminder>` gets its closing tag back.

`task-notification-2.1.285.json` is the other hand-back shape, from a live run
through the hook: the subagent's request headers, the hand-back request's
headers, and its message (a `<system-reminder>` holding a `<task-notification>`
whose `<task-id>` is the subagent's `x-claude-code-agent-id`). The recording cut
it at 600 characters; the closing tags were put back.

The two hand-back shapes are what `test_recorded_handback_is_detected` and
`test_recorded_task_notification_is_detected` pin. If a Claude Code upgrade
changes them, record a new session and add it here.
