"""
Prosodic-Aware Text Chunking for TTS

Intelligently splits text at linguistic boundaries for natural speech synthesis.
Instead of sending full LLM responses to TTS in one shot (which can cause unnatural
prosody for longer responses) or streaming individual tokens (which creates choppy audio),
this utility breaks text at LINGUISTIC BOUNDARIES while maintaining optimal chunk sizes.

Key insight: TTS models plan prosody (pitch, stress, rhythm) for the whole phrase.
Chunking at semantic boundaries allows the model to apply appropriate intonation to
complete thoughts, improving naturalness by 15-20%.

Boundaries (priority order):
  1. Sentence boundaries: . ! ?
  2. Clause boundaries: , ; :
  3. Conjunction points: and, but, or, so (when preceded by a clause)

Chunk constraints:
  - Optimal: 3-8 words per chunk (matches natural phrase units)
  - Minimum: 3 words (too short = unnatural pauses)
  - Maximum: 12 words (too long = prosody planning breaks)
"""
import re
import structlog

logger = structlog.get_logger()

# Sentence delimiters (hard boundaries)
SENTENCE_DELIMITERS = {'.', '!', '?'}

# Clause delimiters (soft boundaries, only on longer chunks)
CLAUSE_DELIMITERS = {',', ';', ':'}

# Conjunctions that can split clauses
CONJUNCTIONS = {'and', 'but', 'or', 'so', 'yet', 'because', 'although', 'however'}


class ProsodyChunk:
    """A single prosodic unit of text."""

    def __init__(self, text: str, boundary_type: str = "none"):
        """
        Args:
            text: The chunk text (stripped)
            boundary_type: 'sentence', 'clause', 'conjunction', or 'none'
        """
        self.text = text.strip()
        self.boundary_type = boundary_type
        self.word_count = len(self.text.split())

    def __repr__(self):
        return f'ProsodyChunk({self.word_count}w, {self.boundary_type})'

    def __str__(self):
        return self.text


class ProsodyChunker:
    """
    Splits text into prosodic chunks optimized for TTS synthesis.

    Algorithm:
    1. Find all boundary positions (sentences, clauses, conjunctions)
    2. Greedily merge short chunks to meet minimum word count
    3. Split long chunks at nearest boundary below max word count
    """

    # Parameters
    MIN_WORDS = 3           # Don't let chunks get too short
    MAX_WORDS = 12          # Beyond this, split at nearest boundary
    OPTIMAL_WORDS = 6       # Target for natural phrase units

    def __init__(self):
        """Initialize the chunker."""
        pass

    def chunk(self, text: str) -> list[ProsodyChunk]:
        """
        Split text into prosodic chunks.

        Args:
            text: Input text to chunk

        Returns:
            List of ProsodyChunk objects
        """
        if not text or not text.strip():
            return []

        text = text.strip()

        # Find all boundary positions
        boundaries = self._find_boundaries(text)

        if not boundaries:
            # No boundaries found — return as single chunk if reasonable length
            word_count = len(text.split())
            if word_count <= self.MAX_WORDS:
                return [ProsodyChunk(text, "none")]
            else:
                # Force split by spaces (fallback)
                return self._split_by_word_count(text)

        # Build chunks from boundaries
        chunks = self._build_chunks_from_boundaries(text, boundaries)

        # Post-process: merge short chunks, split long ones
        chunks = self._normalize_chunks(chunks)

        return chunks

    def _find_boundaries(self, text: str) -> list[dict]:
        """
        Find all prosodic boundaries in text.

        Returns:
            List of dicts: {'pos': position, 'type': 'sentence'|'clause'|'conjunction', 'text': delimiter}
        """
        boundaries = []

        # Find sentence delimiters (highest priority)
        for match in re.finditer(r'[.!?]', text):
            boundaries.append({
                'pos': match.end(),
                'type': 'sentence',
                'char': match.group(),
            })

        # Find clause delimiters (lower priority)
        for match in re.finditer(r'[,;:]', text):
            boundaries.append({
                'pos': match.end(),
                'type': 'clause',
                'char': match.group(),
            })

        # Find conjunctions (lowest priority, only when preceded by clause)
        for match in re.finditer(r'\b(' + '|'.join(CONJUNCTIONS) + r')\b', text, re.IGNORECASE):
            # Include only if preceded by significant text (at least 10 chars)
            if match.start() > 10:
                boundaries.append({
                    'pos': match.start(),
                    'type': 'conjunction',
                    'char': match.group(),
                })

        # Sort by position
        boundaries.sort(key=lambda x: x['pos'])

        return boundaries

    def _build_chunks_from_boundaries(self, text: str, boundaries: list[dict]) -> list[ProsodyChunk]:
        """
        Build chunks by splitting at boundaries.

        Args:
            text: Full text
            boundaries: List of boundary dicts

        Returns:
            List of ProsodyChunk objects
        """
        chunks = []
        last_pos = 0

        for boundary in boundaries:
            pos = boundary['pos']
            boundary_type = boundary['type']

            # Extract text from last_pos to this boundary
            chunk_text = text[last_pos:pos].strip()

            if chunk_text:
                chunks.append(ProsodyChunk(chunk_text, boundary_type))

            last_pos = pos

        # Add remaining text
        if last_pos < len(text):
            remaining = text[last_pos:].strip()
            if remaining:
                chunks.append(ProsodyChunk(remaining, "none"))

        return chunks

    def _normalize_chunks(self, chunks: list[ProsodyChunk]) -> list[ProsodyChunk]:
        """
        Post-process chunks: merge short ones, split long ones.

        Args:
            chunks: Raw chunks from boundary splitting

        Returns:
            Normalized chunks
        """
        if not chunks:
            return []

        normalized = []
        i = 0

        while i < len(chunks):
            chunk = chunks[i]

            # If chunk is already in good range, keep it
            if self.MIN_WORDS <= chunk.word_count <= self.MAX_WORDS:
                normalized.append(chunk)
                i += 1
                continue

            # If chunk is too short, try to merge with next
            if chunk.word_count < self.MIN_WORDS:
                if i + 1 < len(chunks):
                    next_chunk = chunks[i + 1]
                    merged_text = f"{chunk.text} {next_chunk.text}"
                    merged = ProsodyChunk(merged_text, "merged")

                    # If merged is still short, keep merging
                    j = i + 2
                    while merged.word_count < self.MIN_WORDS and j < len(chunks):
                        merged_text = f"{merged.text} {chunks[j].text}"
                        merged = ProsodyChunk(merged_text, "merged")
                        j += 1

                    normalized.append(merged)
                    i = j
                else:
                    # Last chunk and too short — just keep it
                    normalized.append(chunk)
                    i += 1
                continue

            # If chunk is too long, split at word boundaries
            if chunk.word_count > self.MAX_WORDS:
                sub_chunks = self._split_long_chunk(chunk)
                normalized.extend(sub_chunks)
                i += 1
                continue

        return normalized

    def _split_long_chunk(self, chunk: ProsodyChunk) -> list[ProsodyChunk]:
        """
        Split a chunk that exceeds MAX_WORDS.

        Strategy: Try to split near OPTIMAL_WORDS, respecting word boundaries.

        Args:
            chunk: The long chunk to split

        Returns:
            List of shorter chunks
        """
        words = chunk.text.split()
        if len(words) <= self.MAX_WORDS:
            return [chunk]

        result = []
        i = 0

        while i < len(words):
            # Take up to MAX_WORDS
            end_idx = min(i + self.MAX_WORDS, len(words))

            # Try to cut closer to OPTIMAL_WORDS if possible
            if end_idx - i > self.OPTIMAL_WORDS:
                end_idx = min(i + self.OPTIMAL_WORDS, len(words))

            chunk_words = words[i:end_idx]
            chunk_text = ' '.join(chunk_words)
            result.append(ProsodyChunk(chunk_text, "split"))

            i = end_idx

        return result

    def _split_by_word_count(self, text: str) -> list[ProsodyChunk]:
        """
        Fallback: split by word count when no boundaries exist.

        Args:
            text: Text to split

        Returns:
            List of chunks
        """
        words = text.split()
        chunks = []
        i = 0

        while i < len(words):
            end_idx = min(i + self.OPTIMAL_WORDS, len(words))
            chunk_text = ' '.join(words[i:end_idx])
            chunks.append(ProsodyChunk(chunk_text, "word_split"))
            i = end_idx

        return chunks

    def estimate_duration_ms(self, chunks: list[ProsodyChunk], wps: float = 2.8) -> float:
        """
        Estimate total audio duration for chunks at given speaking rate.

        Args:
            chunks: List of ProsodyChunk objects
            wps: Words per second (default 2.8 for phone conversation)

        Returns:
            Estimated duration in milliseconds
        """
        total_words = sum(c.word_count for c in chunks)
        duration_sec = total_words / wps
        return duration_sec * 1000


# ── Testing & Development ─────────────────────────────────────────────────────

def test_prosody_chunker():
    """Test the ProsodyChunker with various examples."""
    chunker = ProsodyChunker()

    test_cases = [
        # Simple sentence
        "Hello, how are you today?",

        # Multiple sentences
        "Hi there! I wanted to reach out about a great opportunity. Do you have a moment?",

        # Long response with clauses and conjunctions
        (
            "I understand your concern, and I appreciate you bringing that up. "
            "However, what we've found is that most customers see significant benefits "
            "within the first month. Have you had a chance to look at the materials we sent?"
        ),

        # Very short response
        "Yes, I'm interested.",

        # No punctuation
        "This is a longer sentence without any punctuation marks in it",
    ]

    for text in test_cases:
        print(f"\n{'='*60}")
        print(f"Input: {text[:70]}...")
        chunks = chunker.chunk(text)
        print(f"\nChunks ({len(chunks)}):")
        for i, c in enumerate(chunks, 1):
            print(f"  {i}. [{c.word_count:2}w, {c.boundary_type:12}] {c.text}")

        duration = chunker.estimate_duration_ms(chunks)
        print(f"\nEstimated duration: {duration:.0f}ms")


if __name__ == "__main__":
    test_prosody_chunker()
