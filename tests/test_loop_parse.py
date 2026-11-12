from __future__ import annotations

import json

from support_agent.agents.loop import parse_envelope


def test_parses_plain_json_envelope() -> None:
    answer, citations = parse_envelope(json.dumps({"answer": "30 days", "citations": ["chunk-1"]}))
    assert answer == "30 days"
    assert citations == ["chunk-1"]


def test_parses_fenced_json() -> None:
    raw = '```json\n{"answer": "30 days", "citations": ["a", "b"]}\n```'
    assert parse_envelope(raw) == ("30 days", ["a", "b"])


def test_parses_citation_objects() -> None:
    raw = json.dumps({"answer": "x", "citations": [{"chunk_id": "abc"}, {"id": "def"}]})
    assert parse_envelope(raw) == ("x", ["abc", "def"])


def test_falls_back_to_free_text() -> None:
    answer, citations = parse_envelope("Refunds take 30 days.")
    assert answer == "Refunds take 30 days."
    assert citations == []


def test_braces_inside_prose_do_not_break_parsing() -> None:
    raw = 'Result: {"answer": "use {braces} carefully", "citations": []}'
    assert parse_envelope(raw) == ("use {braces} carefully", [])


def test_empty_answer_object_is_ignored() -> None:
    answer, citations = parse_envelope('{"answer": "", "citations": ["x"]}')
    assert answer == '{"answer": "", "citations": ["x"]}'
    assert citations == []


def test_null_citations_are_treated_as_empty() -> None:
    assert parse_envelope('{"answer": "30 days", "citations": null}') == ("30 days", [])


def test_single_string_citation_is_wrapped() -> None:
    assert parse_envelope('{"answer": "x", "citations": "chunk-1"}') == ("x", ["chunk-1"])
