"""
Audio utility functions for the voice pipeline.
Codec handling, resampling, and buffer management.
"""
import struct
import numpy as np
from typing import Optional


def pcm_to_float32(pcm_bytes: bytes, sample_width: int = 2) -> np.ndarray:
    """Convert PCM bytes to float32 numpy array."""
    if sample_width == 2:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        return samples.astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(pcm_bytes, dtype=np.int32)
        return samples.astype(np.float32) / 2147483648.0
    raise ValueError(f"Unsupported sample width: {sample_width}")


def float32_to_pcm(audio: np.ndarray, sample_width: int = 2) -> bytes:
    """Convert float32 numpy array to PCM bytes."""
    audio = np.clip(audio, -1.0, 1.0)
    if sample_width == 2:
        return (audio * 32767).astype(np.int16).tobytes()
    elif sample_width == 4:
        return (audio * 2147483647).astype(np.int32).tobytes()
    raise ValueError(f"Unsupported sample width: {sample_width}")


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Simple linear interpolation resampling."""
    if src_rate == dst_rate:
        return audio
    ratio = dst_rate / src_rate
    new_length = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_length)
    return np.interp(indices, np.arange(len(audio)), audio)


def calculate_rms(audio: np.ndarray) -> float:
    """Calculate RMS energy of audio signal."""
    return float(np.sqrt(np.mean(audio ** 2)))


def is_silence(audio_bytes: bytes, threshold: float = 0.01, sample_width: int = 2) -> bool:
    """Check if audio chunk is silence based on RMS energy."""
    audio = pcm_to_float32(audio_bytes, sample_width)
    return calculate_rms(audio) < threshold


class AudioRingBuffer:
    """
    Ring buffer for audio data.
    Drops old data rather than accumulating latency.
    Prefers timeliness over completeness.
    """

    def __init__(self, max_seconds: float = 5.0, sample_rate: int = 16000, sample_width: int = 2):
        self.max_bytes = int(max_seconds * sample_rate * sample_width)
        self._buffer = bytearray()

    def write(self, data: bytes):
        self._buffer.extend(data)
        # Drop oldest data if buffer is full
        if len(self._buffer) > self.max_bytes:
            excess = len(self._buffer) - self.max_bytes
            self._buffer = self._buffer[excess:]

    def read(self, num_bytes: Optional[int] = None) -> bytes:
        if num_bytes is None:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:num_bytes])
        self._buffer = self._buffer[num_bytes:]
        return data

    def clear(self):
        self._buffer.clear()

    @property
    def available(self) -> int:
        return len(self._buffer)
