"""Claude Code: the turn model (memmachine_hook's docstring has the rules).

One turn is one `x-claude-code-prompt-id`: the search runs on its first
request, every request of the turn gets the same block at the same position,
and one pair per typed prompt is stored (a turn with subagents is held until
the session's next real prompt or an idle timeout, so a hand-back can replace a
placeholder answer). Only `x-claude-code-request-class: main` requests get
memory; titles, the auto-mode classifier, subagents and compaction pass through.

  MEMMACHINE_CLAUDE_CODE_AGENT_ID  default claude-code: this adapter's client name
                                   (X-MemMachine-Client, the stored agent_id)
"""

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Optional

import memmachine_hook as core

AGENT_ID = os.environ.get("MEMMACHINE_CLAUDE_CODE_AGENT_ID", "claude-code").strip() or "claude-code"

# Claude Code hint headers (CLAUDE_CODE_GATEWAY_HINT_HEADERS=1).
H_CLASS = "x-claude-code-request-class"
H_SESSION = "x-claude-code-session-id"
H_PROMPT = "x-claude-code-prompt-id"
H_AGENT = "x-claude-code-agent-id"

# In interactive mode a background subagent's report arrives as a main request
# with a new prompt-id. Claude Code 2.1.285 was seen using two shapes (samples in
# fixtures/):
#   A: "Another Claude session sent a message:" + <agent-message from="<agent id>">
#   B: a <system-reminder> holding <task-notification><task-id><agent id></task-id>
# Background Bash commands send B too, with a task id that is not a subagent's.
HANDBACK_PREFIX = "Another Claude session sent a message:"
_HANDBACK_FROM = re.compile(r'<agent-message\s+from="([^"]+)"')
_TASK_ID = re.compile(r"<task-notification>\s*<task-id>\s*([^<\s]+)\s*</task-id>")
# The tool that starts a subagent ("Task" in older Claude Code).
SUBAGENT_TOOLS = {"Agent", "Task"}

# Claude Code puts synthetic context (<system-reminder> and similar) into user
# messages as complete top-level XML-like blocks. Only complete paired blocks that
# start a line are removed, so an unmatched tag in the user's own text survives.
_WRAPPER_BLOCK = re.compile(
    r"(?ms)^[ \t]*<(?P<tag>[A-Za-z][\w:.-]*)(?:\s[^<>]*?)?>"
    r".*?</(?P=tag)\s*>[ \t]*(?:\r?\n)?"
)


def _strip_wrappers(text: str) -> str:
    while True:
        text, count = _WRAPPER_BLOCK.subn("", text)
        if count == 0:
            return text.strip()


@dataclass(frozen=True)
class Prompt:
    index: int                       # position in messages
    text: str                        # without wrapper blocks; "" if only wrappers
    is_handback: bool = False
    agent_id: Optional[str] = None   # the hand-back's sender

    @property
    def is_typed(self) -> bool:
        """Text the user typed, as opposed to a hand-back or wrapper-only message."""
        return bool(self.text) and not self.is_handback


def _prompt_at(messages, index: int) -> Optional[Prompt]:
    """messages[index] as a prompt, if it is a user message with text blocks."""
    message = messages[index]
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    raw = core._content_text(message.get("content"))
    if not raw.strip():
        return None   # tool results only
    task = _TASK_ID.search(raw)
    if task:
        return Prompt(index, "", True, task.group(1))
    text = _strip_wrappers(raw)
    if text.startswith(HANDBACK_PREFIX):
        match = _HANDBACK_FROM.search(raw)
        return Prompt(index, text, True, match.group(1) if match else None)
    return Prompt(index, text)


def newest_prompt(messages) -> Optional[Prompt]:
    """The newest user message with text: what a new prompt-id is about.

    Claude Code often puts `system` messages after the prompt, and tool results are
    user messages without text, so it is found from the end. It is never an older
    message: a new prompt-id whose newest text is only wrapper blocks is a system
    event, not something the user typed.
    """
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        prompt = _prompt_at(messages, index)
        if prompt is not None:
            return prompt
    return None


def find_prompt(messages) -> Optional[Prompt]:
    """The newest typed prompt or hand-back, skipping wrapper-only messages. Used to
    find a known turn's prompt again inside its own history."""
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        prompt = _prompt_at(messages, index)
        if prompt is not None and (prompt.text or prompt.is_handback):
            return prompt
    return None


class ClaudeCodeAdapter(core.Adapter):
    call_types = core.ANTHROPIC_CALL_TYPES
    frontend = "claude-code"

    @property
    def name(self) -> str:
        return AGENT_ID

    def claims(self, call_type, headers) -> bool:
        if call_type not in self.call_types:
            return False
        return any(core._header(headers, h) for h in (H_CLASS, H_SESSION, H_PROMPT)) \
            or core._header(headers, "user-agent").startswith("claude-cli/")

    async def pre_call(self, hook, user_api_key_dict, data, headers) -> None:
        request_class = core._header(headers, H_CLASS)
        session_id = core._header(headers, H_SESSION)
        prompt_id = core._header(headers, H_PROMPT)
        user = getattr(user_api_key_dict, "user_id", None)
        # Per key, not per user: a person's keys are separate sessions.
        token = getattr(user_api_key_dict, "token", None)
        key = (token, session_id)

        if request_class == "subagent":
            # Remember which turn started the subagent, whatever model it uses, so
            # its hand-back can be matched to the turn.
            turn = hook.sessions.get(key)
            agent_id = core._header(headers, H_AGENT)
            if turn is not None and agent_id and prompt_id in turn.prompt_ids:
                turn.agents.add(agent_id)
            return

        if not core._in_scope(data.get("model")):
            return
        if not request_class:
            if session_id and user not in hook._warned_users:
                hook._warned_users.add(user)
                core._log_error("pre_call", "no_hint_headers", user=user,
                                hint="set CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 in the client")
            return
        if request_class != "main" or not session_id or not prompt_id:
            return
        ident = hook.identity(user_api_key_dict, headers)
        if ident is None:
            return
        project = hook.project(user_api_key_dict, headers)
        if project is None:
            return

        messages = data.get("messages")
        now = hook.clock()
        turn = hook.sessions.get(key)

        if turn is not None and prompt_id in turn.prompt_ids:
            index = self._index_for(turn, prompt_id, messages)
        else:
            prompt = newest_prompt(messages)
            if prompt is None:
                return
            if prompt.is_handback:
                if turn is None or not prompt.agent_id or prompt.agent_id not in turn.agents:
                    # Not a subagent of the current turn: a late hand-back (the user
                    # moved on), a background Bash task, or another session. Nothing
                    # is injected or stored for it.
                    core._log_error("handback", "unmatched", user=user, session=session_id,
                                    prompt_id=prompt_id, agent_id=prompt.agent_id)
                    return
                turn.prompt_ids.add(prompt_id)
                turn.indices[prompt_id] = prompt.index
                index = prompt.index
            elif not prompt.is_typed:
                core._log("new prompt-id without typed text: not a prompt")
                return
            else:
                if turn is not None:
                    hook._flush(hook.sessions.pop(key))
                turn = hook.new_turn(ident, self.name, session_id, prompt_id, prompt.text, project, now,
                                     prompt_ids={prompt_id}, indices={prompt_id: prompt.index})
                hook.start_search(turn, data.get("litellm_call_id"))
                hook.sessions[key] = turn
                hook._enforce_cap()
                index = prompt.index

        turn.last_seen = now
        turn.in_flight += 1
        if key in hook.sessions:
            hook.sessions.move_to_end(key)
        await hook.inject(turn, messages, index)

    def _index_for(self, turn, prompt_id: str, messages) -> Optional[int]:
        """The prompt's position, kept for the whole turn so the injected block does
        not move. Found again if the history no longer has it there."""
        index = turn.indices.get(prompt_id)
        if isinstance(messages, list) and index is not None and index < len(messages):
            if _prompt_at(messages, index) is not None:
                return index
        prompt = find_prompt(messages)
        if prompt is None:
            return None
        turn.indices[prompt_id] = prompt.index
        return prompt.index

    def on_success(self, hook, kwargs, response_obj, headers) -> None:
        turn = self._finished(hook, kwargs, headers)
        if turn is None:
            return
        if core.tool_names(response_obj) & SUBAGENT_TOOLS:
            turn.spawned = True
        answer = core.final_answer(response_obj)
        if answer:
            turn.answer = answer
            turn.answer_request = kwargs.get("litellm_call_id") or ""
            if not (turn.spawned or turn.agents):
                # No hand-back can follow, so there is nothing to wait for. The
                # tool call is checked too: the subagent's first request races the
                # parent's next one.
                hook._flush(turn)
                turn.answer = None
            hook._save_pending()

    def on_failure(self, hook, kwargs, headers) -> None:
        self._finished(hook, kwargs, headers)

    def _finished(self, hook, kwargs, headers):
        """A main request of a turn got its reply (or failed): the turn's clock
        restarts from now. Returns the turn, or None if the request had none."""
        if not core._in_scope(core.request_model(kwargs)):
            return None
        if core._header(headers, H_CLASS) != "main":
            return None
        key = (core.request_meta(kwargs).get("user_api_key_hash"), core._header(headers, H_SESSION))
        turn = hook.sessions.get(key)
        if turn is None or core._header(headers, H_PROMPT) not in turn.prompt_ids:
            return None
        turn.in_flight = max(0, turn.in_flight - 1)
        turn.last_seen = hook.clock()
        return turn
