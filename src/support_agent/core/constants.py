"""Shared constants: prompt markers and the canonical refusal answer.

Document blocks carry a per-request random nonce:

    <<<UNTRUSTED_DOC id="…" source="…" section="…" n="3f9c1a22">>>
    …document text…
    <<<END_UNTRUSTED_DOC n="3f9c1a22">>>

A document cannot forge a closing or opening marker because it cannot guess
the nonce, and the parser only accepts a closing tag whose nonce matches the
opening tag (regex back-reference). Marker-like text inside a document is also
neutralised when it is rendered, so a look-alike marker never reaches the model
verbatim.
"""

from __future__ import annotations

import re
import secrets

DOC_OPEN = '<<<UNTRUSTED_DOC id="{chunk_id}" source="{source}" section="{section}" n="{nonce}">>>'
DOC_CLOSE = '<<<END_UNTRUSTED_DOC n="{nonce}">>>'

UNTRUSTED_DOC_RE = re.compile(
    r'<<<UNTRUSTED_DOC id="(?P<chunk_id>[^"]*)" source="(?P<source>[^"]*)" '
    r'section="(?P<section>[^"]*)" n="(?P<nonce>[0-9a-f]{4,32})">>>\n'
    r"(?P<content>.*?)\n"
    r'<<<END_UNTRUSTED_DOC n="(?P=nonce)">>>',
    re.DOTALL,
)

# Bump when the prompt's semantics change (cached answers must not survive it).
PROMPT_VERSION = "p1"

# Must be returned verbatim when the documents do not contain the answer.
REFUSAL_ANSWER = (
    "I don't have enough information in the company documents to answer that. "
    "I can escalate this to a human colleague if you'd like."
)

ANSWER_JSON_INSTRUCTION = (
    'Reply with a single JSON object: {"answer": string, "citations": [chunk_id, ...]}. '
    "Only cite chunk ids that appear in the context. If the context does not contain "
    "the answer, set answer to the refusal sentence and citations to []."
)


def new_nonce() -> str:
    """128-bit nonce (32 hex chars): marker forgery is not a 2^32 guessing game."""
    return secrets.token_hex(16)
