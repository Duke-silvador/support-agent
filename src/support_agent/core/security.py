"""Prompt-injection detection for untrusted documents.

Policy: **flag, never rewrite.** The document text is stored verbatim so the
agent (and auditors) see exactly what a customer uploaded. Flags are metadata
and are surfaced in the API response and in traces. The prompt layer is what
actually neutralises instructions (documents are wrapped as data).
"""

from __future__ import annotations

import re

# name -> pattern. Keep patterns narrow to limit false positives on support docs
# (e.g. a help-centre article that legitimately says "ignore the previous step").
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}?"
            r"\b(previous|prior|above|earlier|all)\b[^.\n]{0,20}?"
            r"\b(instructions?|prompts?|rules?|directions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"\b(reveal|show|print|repeat|leak|output)\b[^.\n]{0,40}?"
            r"\b(system\s*prompt|hidden\s*instructions?|developer\s*message|"
            r"api[\s_-]?key|secret|credential|token)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_hijack",
        re.compile(r"(?im)^[ \t]*(system|developer|assistant|tool)[ \t]*:"),
    ),
    (
        "you_are_now",
        re.compile(
            r"\byou\s+are\s+now\b[^.\n]{0,40}\b(admin|unrestricted|developer|"
            r"dan|jailbroken|root)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_invocation",
        re.compile(
            r"\b(call|invoke|execute|run)\b[^.\n]{0,25}?"
            r"\b(tool|function)\b[^.\n]{0,25}?"
            r"\b(escalate_to_human|search_docs)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|upload|email|exfiltrate)\b[^.\n]{0,60}?"
            r"(https?://|@[a-z0-9.-]+\.[a-z]{2,}|webhook)",
            re.IGNORECASE,
        ),
    ),
)

MAX_FLAGS = 8


def scan_for_injection(text: str) -> list[str]:
    """Return sorted unique flag names found in ``text`` (empty list = clean)."""
    flags = {name for name, pattern in _PATTERNS if pattern.search(text)}
    return sorted(flags)[:MAX_FLAGS]


def is_suspicious(text: str) -> bool:
    return bool(scan_for_injection(text))
