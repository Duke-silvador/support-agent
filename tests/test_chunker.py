from __future__ import annotations

from support_agent.rag.chunker import chunk_text


def test_empty_text_returns_no_chunks() -> None:
    assert chunk_text("   \n\n ", size=100, overlap=10) == []


def test_headings_become_section_metadata() -> None:
    text = "# Refunds\n\nWe refund within 30 days.\n\n## Exclusions\n\nAdd-ons are excluded."
    chunks = chunk_text(text, size=200, overlap=20)
    assert [c.heading for c in chunks] == ["Refunds", "Exclusions"]
    assert [c.index for c in chunks] == [0, 1]
    assert "30 days" in chunks[0].content
    assert "Add-ons" in chunks[1].content


def test_long_text_is_split_with_overlap_and_respects_budget() -> None:
    paragraph = "word " * 400  # ~2000 chars, one paragraph
    text = f"# Big\n\n{paragraph}"
    chunks = chunk_text(text, size=300, overlap=40)
    assert len(chunks) > 1
    for chunk in chunks:
        # Every window respects the configured size, including the tail case.
        assert len(chunk.content) <= 300
    assert all(chunk.heading == "Big" for chunk in chunks)


def test_high_overlap_never_produces_oversized_chunks() -> None:
    """overlap > size/2 used to leave a >size remainder after a slice."""
    text = "# Big\n\n" + "word " * 200
    chunks = chunk_text(text, size=10, overlap=9)
    assert chunks
    for chunk in chunks:
        assert len(chunk.content) <= 10
        assert chunk.content.strip()


def test_text_before_first_heading_is_kept() -> None:
    chunks = chunk_text("Preamble text here.\n\n# Title\n\nBody.", size=200, overlap=0)
    assert chunks[0].heading is None
    assert "Preamble" in chunks[0].content
    assert chunks[1].heading == "Title"


def test_invalid_overlap_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        chunk_text("hello", size=10, overlap=10)
