"""Qwen Chat message compatibility without moving later instructions into the past."""
from __future__ import annotations

import copy
from typing import Any


def instruction_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str) and set(part) <= {"type", "text"}
        for part in content
    ):
        return "".join(part["text"] for part in content)
    return None


def normalize_chat_request(payload: dict[str, Any], mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Only leading instructions are merged; later ones become user context in place.

    Tool messages, tool call IDs/arguments, and all request-level options are copied
    unchanged. Unsupported instruction shapes are passed through as a whole so no
    content is silently discarded; the upstream can report its validation error.
    """
    audit: dict[str, Any] = {"mode": mode, "version": 1, "changed": False, "changes": []}
    if mode == "off":
        return payload, audit
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(m, dict) or not isinstance(m.get("role"), str) for m in messages
    ):
        audit["skip_reason"] = "invalid_messages"
        return payload, audit
    instruction_roles = {"system", "developer"}
    prefix = 0
    while prefix < len(messages) and messages[prefix].get("role") in instruction_roles:
        prefix += 1
    needs_change = prefix > 1 or any(
        message.get("role") == "developer"
        or (index > 0 and message.get("role") == "system")
        for index, message in enumerate(messages)
    )
    if not needs_change:
        return payload, audit
    for index, message in enumerate(messages):
        if message.get("role") in instruction_roles and (
            instruction_text(message) is None or set(message) - {"role", "content", "name"}
        ):
            audit.update(skip_reason="unsupported_instruction_shape", skipped_message_index=index)
            return payload, audit

    forwarded = copy.deepcopy(payload)
    normalized: list[dict[str, Any]] = []
    if prefix:
        if prefix == 1 and messages[0]["role"] == "system":
            normalized.append(copy.deepcopy(messages[0]))
        else:
            sections = []
            for index, message in enumerate(messages[:prefix]):
                source = message["role"]
                if message.get("name"):
                    source += ":" + str(message["name"])
                sections.append(f"[Framework {source}; original messages[{index}]]\n"
                                + instruction_text(message))
            normalized.append({"role": "system", "content": "\n\n".join(sections)})
            audit["changes"].append({"action": "merge_leading_instructions", "indices": list(range(prefix))})
    for index, original in enumerate(messages[prefix:], start=prefix):
        message = copy.deepcopy(original)
        if message.get("role") in instruction_roles:
            source_role = message["role"]
            message["role"] = "user"
            message["content"] = (
                f"[Framework context; original role={source_role}; original messages[{index}]]\n"
                + instruction_text(original)
                + "\n[End framework context]"
            )
            audit["changes"].append({"action": "instruction_to_user_context", "index": index,
                                     "original_role": source_role})
        normalized.append(message)
    forwarded["messages"] = normalized
    audit["changed"] = True
    return forwarded, audit
