"""Open WebUI, and any client no other adapter claims: one request is one turn.

The request and store logic of the single-file hook this repo had before the
adapters, unchanged:
  - routes: /v1/chat/completions, /v1/messages, /v1/responses (`input`, a string
    or a list of {role, content}, instead of `messages`);
  - the prompt is the newest user message; the memory block is a system message
    inserted right before it (not at the front, which would change the prompt
    prefix every turn and invalidate a prefix cache);
  - no search and no block when the conversation already holds a <memory> block
    (the client sent back a history with one); the turn is still stored;
  - the answer is the reply's text; when a reasoning model returns no content
    (max_tokens ran out while it was thinking), its reasoning text instead;
  - the prompt and the answer are stored as two messages, every memory type,
    the answer produced by MEMMACHINE_AGENT_ID (memmachine_hook.Pair, style
    "openwebui");
  - session: `x-memmachine-session`, else LiteLLM's metadata session_id, else the
    person (a token-identified request) or the key's hash, else "default".

What the core does for every adapter: who the person is (proposed to Platform),
the Platform client, a refusal never failing the request, store retries
and dead letters, tracing.

This adapter is last in memmachine_hook.default_adapters() and claims every
request of its call types the adapters before it did not; a request naming it
(`x-memmachine-client: openwebui`, an Open WebUI connection header) is always
its own. Its label to Platform is `openwebui` for a request that names it or
carries Open WebUI's signed token, `other` for the rest.
"""

import memmachine_hook as core

CALL_TYPES = ("completion", "acompletion", "anthropic_messages", "aanthropic_messages",
              "aresponses", "responses")
MARKER = "<memory>"
INTRO = ("Below is information retrieved from previous sessions in the selected memory context. "
         "Use it only when relevant; do not assume every item describes the current user, and do "
         "not fabricate anything not shown here.\n")


def text_of(content) -> str:
    """A message's text: a string, or the `text` of its parts (chat and Responses)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text")) for p in content if isinstance(p, dict) and p.get("text"))
    return ""


def responses_messages(inp) -> list:
    """/v1/responses `input` as a messages list: a bare string is one user message."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}] if inp.strip() else []
    if isinstance(inp, list):
        return [m for m in inp if isinstance(m, dict)]
    return []


def last_user_index(messages):
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            return i
    return None


def has_memory_block(messages) -> bool:
    return any(isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
               and MARKER in m["content"] for m in messages)


def build_block(items) -> str:
    """The <memory> block: semantic results, then episodes and summaries, one line each."""
    lines = [i["text"] for i in items if isinstance(i, dict) and str(i.get("text") or "").strip()]
    if not lines:
        return None
    return f"{MARKER}\n{INTRO}" + "\n".join(f"- {line}" for line in lines) + "\n</memory>"


def responses_answer(response_obj) -> str:
    """The answer of a /v1/responses reply: the text of its `message` items
    (reasoning items are the model's thinking, not the answer)."""
    out = core._get(response_obj, "output")
    parts = []
    for item in out if isinstance(out, list) else []:
        if core._get(item, "type") != "message":
            continue
        content = core._get(item, "content")
        for c in content if isinstance(content, list) else []:
            text = core._get(c, "text")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def chat_answer(response_obj) -> str:
    """The reply's text; a reasoning model's reasoning text when there is none."""
    message = core._reply_message(response_obj)
    if message is None:
        return ""
    text = text_of(core._get(message, "content"))
    if not text.strip():
        text = text_of(core._get(message, "reasoning_content") or core._get(message, "reasoning"))
    return text


class OpenWebUIAdapter(core.Adapter):
    name = "openwebui"
    call_types = CALL_TYPES
    frontend = "openwebui"

    def claims(self, call_type, headers) -> bool:
        return call_type in self.call_types

    @staticmethod
    def _key(token, call_id):
        return (token, f"owui:{call_id}")

    async def pre_call(self, hook, user_api_key_dict, data, headers) -> None:
        if not core._in_scope(data.get("model")):
            return
        call_id = data.get("litellm_call_id")
        # /v1/responses carries the conversation as `input`; LiteLLM keeps it there.
        responses = "messages" not in data and "input" in data
        messages = responses_messages(data.get("input")) if responses else data.get("messages")
        if not call_id or not isinstance(messages, list):
            return
        index = last_user_index(messages)
        if index is None:
            return
        text = text_of(messages[index].get("content"))
        if not text.strip():
            return
        ident = hook.identity(user_api_key_dict, headers)
        if ident is None:
            return
        project = hook.project(user_api_key_dict, headers)
        if project is None:
            return
        token = core._header(headers, core.ASSERTION_HEADER)
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        session_id = (core._header(headers, "x-memmachine-session") or metadata.get("session_id")
                      or (ident.person if token else ident.key_id) or "default")
        # The label Platform sees: Open WebUI when it says so or signs its user.
        named = core._header(headers, core.H_FRONTEND).lower() == self.frontend
        turn = hook.new_turn(ident, "openwebui" if token or named else "other", str(session_id), call_id, text,
                             project, hook.clock(), prompt_ids={call_id},
                             style="openwebui", build=build_block)
        turn.in_flight = 1
        hook.sessions[self._key(getattr(user_api_key_dict, "token", None), call_id)] = turn
        hook._enforce_cap()
        if has_memory_block(messages):
            return
        hook.start_search(turn, call_id)
        before = len(messages)
        await hook.inject(turn, messages, index)
        if responses and len(messages) != before:
            data["input"] = messages

    def _pop(self, hook, kwargs):
        key = self._key(core.request_meta(kwargs).get("user_api_key_hash"), kwargs.get("litellm_call_id"))
        return hook.sessions.pop(key, None)

    def on_success(self, hook, kwargs, response_obj, headers) -> None:
        turn = self._pop(hook, kwargs)
        if turn is None:
            return
        responses = kwargs.get("call_type") in ("aresponses", "responses")
        answer = responses_answer(response_obj) if responses else chat_answer(response_obj)
        if answer.strip():
            turn.answer = answer
            turn.answer_request = kwargs.get("litellm_call_id") or ""
            hook._flush(turn)
            hook._save_pending()

    def on_failure(self, hook, kwargs, headers) -> None:
        self._pop(hook, kwargs)
