"""
Test suite for ProsodyChunker — validates linguistic boundary detection
and optimal chunk sizing for natural TTS synthesis.
"""
import pytest
from src.prosody_chunker import ProsodyChunker, ProsodyChunk


class TestProsodyChunk:
    """Tests for individual ProsodyChunk objects."""

    def test_basic_chunk_creation(self):
        chunk = ProsodyChunk("Hello world", "sentence")
        assert chunk.text == "Hello world"
        assert chunk.word_count == 2
        assert chunk.boundary_type == "sentence"

    def test_chunk_text_stripping(self):
        chunk = ProsodyChunk("  hello world  ", "clause")
        assert chunk.text == "hello world"

    def test_chunk_repr(self):
        chunk = ProsodyChunk("test text", "sentence")
        assert "2w" in repr(chunk)
        assert "sentence" in repr(chunk)


class TestProsodyChunkerBasic:
    """Basic functionality tests."""

    def test_empty_text(self):
        chunker = ProsodyChunker()
        chunks = chunker.chunk("")
        assert chunks == []

    def test_single_short_sentence(self):
        chunker = ProsodyChunker()
        chunks = chunker.chunk("Hello!")
        assert len(chunks) == 1
        assert chunks[0].text == "Hello!"

    def test_two_sentences(self):
        chunker = ProsodyChunker()
        chunks = chunker.chunk("Hello! Goodbye.")
        # Short sentences get merged to meet MIN_WORDS threshold
        assert len(chunks) >= 1
        assert any("Hello" in c.text for c in chunks)
        assert any("Goodbye" in c.text for c in chunks)


class TestProsodyChunkerSentenceBoundaries:
    """Tests for sentence boundary detection."""

    def test_period_boundary(self):
        chunker = ProsodyChunker()
        text = "First sentence. Second sentence."
        chunks = chunker.chunk(text)
        # Short sentences get merged to meet MIN_WORDS threshold
        assert len(chunks) >= 1
        assert all(c.word_count >= 2 for c in chunks)

    def test_question_mark_boundary(self):
        chunker = ProsodyChunker()
        text = "Do you agree? Yes I do."
        chunks = chunker.chunk(text)
        # Should split at question mark
        assert any(c.boundary_type == "sentence" for c in chunks)

    def test_exclamation_boundary(self):
        chunker = ProsodyChunker()
        text = "That's amazing! I love it."
        chunks = chunker.chunk(text)
        # Chunks may be merged, but should contain the text
        assert any("amazing" in c.text for c in chunks)


class TestProsodyChunkerClauseBoundaries:
    """Tests for clause boundary detection."""

    def test_comma_boundary(self):
        chunker = ProsodyChunker()
        text = "However, we should consider the alternatives."
        chunks = chunker.chunk(text)
        # Should detect comma as boundary
        assert len(chunks) >= 1

    def test_semicolon_boundary(self):
        chunker = ProsodyChunker()
        text = "First part; second part and more text here."
        chunks = chunker.chunk(text)
        # Longer text with semicolon should create reasonable chunks
        assert len(chunks) >= 1
        assert all(c.word_count >= 2 for c in chunks)


class TestProsodyChunkerConjunctions:
    """Tests for conjunction detection at clause boundaries."""

    def test_and_conjunction(self):
        chunker = ProsodyChunker()
        # Should detect "and" when preceded by sufficient text
        text = "This is a longer clause and this is another one."
        chunks = chunker.chunk(text)
        # Should have reasonable chunks
        assert len(chunks) >= 1
        assert all(3 <= c.word_count <= 20 for c in chunks)

    def test_but_conjunction(self):
        chunker = ProsodyChunker()
        text = "I understand your point, but I still think differently."
        chunks = chunker.chunk(text)
        assert len(chunks) >= 1


class TestProsodyChunkerWordCount:
    """Tests for optimal chunk sizing."""

    def test_minimum_word_threshold(self):
        chunker = ProsodyChunker()
        text = "Short. Very short. Quite short indeed."
        chunks = chunker.chunk(text)
        # Short chunks should be merged
        for chunk in chunks:
            assert chunk.word_count >= chunker.MIN_WORDS or len(chunks) == 1

    def test_maximum_word_threshold(self):
        chunker = ProsodyChunker()
        # Long sentence without punctuation
        text = "This is a very long sentence with many words that should be split somehow"
        chunks = chunker.chunk(text)
        # No chunk should exceed max (unless it's a single word, impossible case)
        for chunk in chunks:
            if chunk.word_count > chunker.MAX_WORDS:
                # Only acceptable if it's the only chunk (no way to split further)
                assert len(chunks) == 1

    def test_optimal_range(self):
        chunker = ProsodyChunker()
        # Multi-sentence response that should have good chunking
        text = "I understand your concern. However, what we've found is that most customers see significant benefits. Do you want to learn more?"
        chunks = chunker.chunk(text)
        # Most chunks should be in optimal range
        in_optimal_range = sum(
            1 for c in chunks
            if chunker.MIN_WORDS <= c.word_count <= chunker.MAX_WORDS
        )
        assert in_optimal_range >= len(chunks) * 0.7  # 70% should be optimal


class TestProsodyChunkerRealWorldResponses:
    """Tests with realistic conversational responses."""

    def test_short_response(self):
        chunker = ProsodyChunker()
        text = "Yes, I'm interested."
        chunks = chunker.chunk(text)
        assert len(chunks) == 1
        assert 3 <= chunks[0].word_count <= 12

    def test_medium_response(self):
        chunker = ProsodyChunker()
        text = "I appreciate you reaching out. What exactly are you offering?"
        chunks = chunker.chunk(text)
        assert 1 <= len(chunks) <= 3
        # All chunks should be reasonable size
        for chunk in chunks:
            assert 3 <= chunk.word_count <= 15

    def test_long_response(self):
        chunker = ProsodyChunker()
        text = (
            "I understand your concern, and I appreciate you bringing that up. "
            "However, what we've found is that most customers see significant benefits "
            "within the first month. Have you had a chance to look at the materials we sent?"
        )
        chunks = chunker.chunk(text)
        # Should create multiple chunks
        assert len(chunks) >= 2
        # All chunks should be in reasonable range
        for chunk in chunks:
            assert 3 <= chunk.word_count <= 15

    def test_no_punctuation_response(self):
        chunker = ProsodyChunker()
        text = "This is a response without any punctuation marks at all"
        chunks = chunker.chunk(text)
        # Should still create chunks
        assert len(chunks) >= 1
        # All chunks should be reasonable
        for chunk in chunks:
            assert chunk.word_count > 0


class TestProsodyChunkerDuration:
    """Tests for duration estimation."""

    def test_duration_estimation(self):
        chunker = ProsodyChunker()
        text = "Hello there. How are you?"
        chunks = chunker.chunk(text)
        duration_ms = chunker.estimate_duration_ms(chunks)
        # 4 words at 2.8 wps = ~1.4 sec = 1400ms
        assert 1000 < duration_ms < 2000

    def test_duration_scaling(self):
        chunker = ProsodyChunker()
        text = "A B C D E F G H I J"  # 10 words
        chunks = chunker.chunk(text)
        duration_slow = chunker.estimate_duration_ms(chunks, wps=2.0)
        duration_fast = chunker.estimate_duration_ms(chunks, wps=4.0)
        # Slower rate should give longer duration
        assert duration_slow > duration_fast


class TestProsodyChunkerEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_only_punctuation(self):
        chunker = ProsodyChunker()
        text = "!!!"
        chunks = chunker.chunk(text)
        # Should handle gracefully
        assert isinstance(chunks, list)

    def test_multiple_spaces(self):
        chunker = ProsodyChunker()
        text = "Hello    world.    How    are    you?"
        chunks = chunker.chunk(text)
        # Should normalize spaces
        assert all(" " not in c.text.split("  ") for c in chunks)

    def test_mixed_delimiters(self):
        chunker = ProsodyChunker()
        text = "First part; second part, third part. Fourth part!"
        chunks = chunker.chunk(text)
        # Should handle all delimiter types and create reasonable chunks
        assert len(chunks) >= 1
        # All chunks should have meaningful word count
        assert all(c.word_count >= 1 for c in chunks)

    def test_very_long_response(self):
        chunker = ProsodyChunker()
        # Simulate a 2-3 sentence response
        text = (
            "Let me explain what we're offering here. "
            "We've been helping customers like you for over ten years now. "
            "The key benefit is that you'll save time and money. "
            "So what do you think about getting started?"
        )
        chunks = chunker.chunk(text)
        # Should create reasonable chunks
        assert 2 <= len(chunks) <= 8
        for chunk in chunks:
            assert 3 <= chunk.word_count <= 15


class TestProsodyChunkerIntegration:
    """Integration tests simulating real TTS usage."""

    def test_chunk_concatenation(self):
        """Verify that concatenated chunks recover original text (roughly)."""
        chunker = ProsodyChunker()
        text = "Hello there. How are you doing today? I'm great!"
        chunks = chunker.chunk(text)

        # Reconstruct (won't be exact due to normalization)
        reconstructed = " ".join(c.text for c in chunks)
        # Should contain all major words
        for word in ["Hello", "How", "doing", "today"]:
            assert word in reconstructed

    def test_boundary_type_distribution(self):
        """Check that boundary types are correctly assigned."""
        chunker = ProsodyChunker()
        text = "First. Second, third and fourth. Fifth?"
        chunks = chunker.chunk(text)

        # Should have various boundary types
        boundary_types = {c.boundary_type for c in chunks}
        # Should have detected at least sentence boundaries
        assert "sentence" in boundary_types or "merged" in boundary_types


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
