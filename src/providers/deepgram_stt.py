"""
Deepgram Nova-3 STT Provider
- Sub-300ms streaming latency
- 5.26% WER (best in class)
- $0.0043/min (batch) or $0.0077/min (streaming)
"""
import asyncio
import time
import json
import structlog
from typing import AsyncIterator, Optional
import websockets

from .base import STTProvider, ProviderHealth, LatencyTrace

logger = structlog.get_logger()


class DeepgramSTTProvider(STTProvider):
    """Deepgram Nova-3 speech-to-text with streaming WebSocket.

    IMPORTANT: Each call to transcribe_stream() reuses the persistent WebSocket.
    After sending audio + Finalize, we read results until we get a final transcript,
    then break. The next call picks up where the WebSocket left off.

    If the WebSocket is in a bad state (closed, stale), we reconnect automatically.
    """

    name = "deepgram_nova3"
    cost_per_minute = 0.0077  # Streaming rate

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "en"):
        self.api_key = api_key
        self.model = model
        self.language = language
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._health = ProviderHealth(provider_name=self.name)
        self._connected = False
        self._turn_count = 0

    async def connect(self) -> None:
        """Establish persistent WebSocket to Deepgram."""
        await self._connect_ws()

    async def _connect_ws(self) -> None:
        """Internal: create a fresh WebSocket connection to Deepgram."""
        url = (
            f"wss://api.deepgram.com/v1/listen"
            f"?model={self.model}"
            f"&language={self.language}"
            f"&encoding=linear16"
            f"&sample_rate=16000"
            f"&channels=1"
            f"&punctuate=true"
            f"&interim_results=true"      # Critical: enables partial transcripts
            f"&endpointing=400"            # 400ms silence + 250ms accumulation = 650ms total (good balance)
            f"&vad_events=true"
            f"&utterance_end_ms=1000"     # Emit UtteranceEnd 1s after speech ends
            f"&smart_format=true"
        )
        headers = {"Authorization": f"Token {self.api_key}"}

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                max_size=None,
            )
            self._connected = True
            self._turn_count = 0

            # Send a short silent warmup frame so Deepgram is ready
            # before real audio arrives (prevents empty first transcript)
            warmup_silence = b'\x00' * 640  # 20ms of silence at 16kHz 16-bit
            await self._ws.send(warmup_silence)

            logger.info("deepgram_stt_connected", model=self.model)
        except Exception as e:
            self._health.record_error()
            logger.error("deepgram_stt_connect_failed", error=str(e))
            raise

    def _ws_is_closed(self) -> bool:
        """Check if WebSocket is closed (compatible with websockets v13 and v14+)."""
        if not self._ws:
            return True
        # websockets v14+: ClientConnection has close_code but not .closed
        # websockets v13-: WebSocketClientProtocol has .closed
        if hasattr(self._ws, 'closed'):
            return self._ws.closed
        # v14+ fallback: close_code is None when connection is open
        if hasattr(self._ws, 'close_code'):
            return self._ws.close_code is not None
        return False

    async def _ensure_connected(self) -> None:
        """Ensure the WebSocket is healthy. Reconnect if needed."""
        if not self._ws or not self._connected:
            logger.info("deepgram_reconnecting", reason="not_connected")
            await self._connect_ws()
            return

        # Check if WebSocket is still open
        try:
            if self._ws_is_closed():
                logger.info("deepgram_reconnecting", reason="ws_closed")
                await self._connect_ws()
        except Exception:
            logger.info("deepgram_reconnecting", reason="ws_check_failed")
            await self._connect_ws()

    async def transcribe_stream(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[dict]:
        """
        Stream audio to Deepgram, yield partial and final transcripts.

        For each turn:
        1. Send all audio chunks
        2. Send Finalize to force final transcript
        3. Read results until we get is_final=True
        4. Return (WebSocket stays open for next turn)

        IMPORTANT: If the WebSocket seems stale or the previous turn left
        unread messages, we handle gracefully with timeouts.
        """
        await self._ensure_connected()
        self._turn_count += 1
        turn = self._turn_count

        trace = LatencyTrace(provider=self.name, operation="transcribe")

        logger.info("stt_turn_starting",
            turn=turn,
            ws_open=not self._ws_is_closed(),
        )

        # Start sender task to push audio chunks
        audio_bytes_sent = 0

        async def send_audio():
            nonlocal audio_bytes_sent
            try:
                async for chunk in audio_chunks:
                    if self._ws and self._connected:
                        await self._ws.send(chunk)
                        audio_bytes_sent += len(chunk)

                # Tell Deepgram this audio segment is done
                if self._ws and self._connected:
                    await self._ws.send(json.dumps({"type": "Finalize"}))
                    logger.info("stt_finalize_sent", turn=turn, audio_bytes=audio_bytes_sent)
            except Exception as e:
                logger.warning("stt_send_error", turn=turn, error=str(e))

        sender = asyncio.create_task(send_audio())

        try:
            # Read results with overall timeout
            deadline = time.time() + 15.0  # 15s max per turn
            got_final = False

            async for msg in self._ws:
                if time.time() > deadline:
                    logger.warning("stt_transcript_timeout", turn=turn)
                    break

                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [{}])

                    if alternatives and alternatives[0].get("transcript"):
                        alt = alternatives[0]
                        is_final = data.get("is_final", False)
                        confidence = alt.get("confidence", 0.0)
                        transcript = alt["transcript"]

                        if not trace.first_result_time:
                            trace.mark_first_result()

                        latency = trace.time_to_first_result_ms
                        self._health.record_success(latency)

                        logger.info("stt_result",
                            turn=turn,
                            is_final=is_final,
                            confidence=round(confidence, 3),
                            text=transcript[:80],
                        )

                        yield {
                            "text": transcript,
                            "is_final": is_final,
                            "confidence": confidence,
                            "latency_ms": latency,
                            "words": alt.get("words", []),
                        }

                        if is_final:
                            got_final = True
                            break

                    elif data.get("is_final", False):
                        # Final result but empty transcript (silence)
                        logger.info("stt_final_empty", turn=turn)
                        yield {
                            "text": "",
                            "is_final": True,
                            "confidence": 0.0,
                            "latency_ms": 0,
                        }
                        got_final = True
                        break

                elif msg_type == "SpeechStarted":
                    yield {"event": "speech_started", "timestamp": time.time()}

                elif msg_type == "UtteranceEnd":
                    yield {"event": "utterance_end", "timestamp": time.time()}

                elif msg_type == "Metadata":
                    # Ignore metadata messages
                    pass

            if not got_final:
                logger.warning("stt_no_final_transcript", turn=turn)
                # Yield empty final to unblock the pipeline
                yield {
                    "text": "",
                    "is_final": True,
                    "confidence": 0.0,
                    "latency_ms": 0,
                }

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("deepgram_ws_closed", turn=turn, code=e.code)
            self._health.record_error()
            self._connected = False
            # Yield empty final to unblock
            yield {"text": "", "is_final": True, "confidence": 0.0, "latency_ms": 0}
        except Exception as e:
            logger.error("stt_receive_error", turn=turn, error=str(e), error_type=type(e).__name__)
            yield {"text": "", "is_final": True, "confidence": 0.0, "latency_ms": 0}
        finally:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass

            # Send KeepAlive to prevent Deepgram from closing the WebSocket
            # between turns (avoids costly reconnections)
            try:
                if self._ws and self._connected and not self._ws_is_closed():
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception:
                pass

    async def transcribe_continuous(self, audio_chunks: AsyncIterator[bytes]) -> AsyncIterator[dict]:
        """
        Stream audio continuously to Deepgram, yield results in real-time.
        Unlike transcribe_stream(), does NOT send Finalize — relies on Deepgram's
        endpointing (300ms silence) to detect speech boundaries automatically.

        This is the correct architecture for live phone calls: audio flows
        continuously, Deepgram's VAD handles turn detection, and we react
        to speech_final/UtteranceEnd events to trigger LLM→TTS turns.

        Yields:
        - {"event": "speech_started"} — Deepgram detected speech
        - {"event": "utterance_end"} — Deepgram detected end of utterance
        - {"text": "...", "is_final": bool, "speech_final": bool, ...}
        """
        await self._ensure_connected()
        self._turn_count += 1
        turn = self._turn_count

        trace = LatencyTrace(provider=self.name, operation="continuous")
        audio_bytes_sent = 0

        logger.info("continuous_stt_starting", turn=turn, ws_open=not self._ws_is_closed())

        async def send_audio():
            nonlocal audio_bytes_sent
            try:
                chunk_count = 0
                async for chunk in audio_chunks:
                    if self._ws and self._connected and not self._ws_is_closed():
                        await self._ws.send(chunk)
                        audio_bytes_sent += len(chunk)
                        chunk_count += 1
                        if chunk_count in (1, 10, 100, 500, 1000, 5000):
                            logger.debug("continuous_audio_sent",
                                turn=turn, chunks=chunk_count,
                                bytes=audio_bytes_sent)
                    else:
                        logger.warning("continuous_ws_not_ready",
                            turn=turn, connected=self._connected)
                        break

                logger.info("continuous_audio_feeder_done",
                    turn=turn, total_chunks=chunk_count,
                    total_bytes=audio_bytes_sent)
            except Exception as e:
                logger.warning("continuous_send_error", turn=turn, error=str(e))

        sender = asyncio.create_task(send_audio())

        try:
            async for msg in self._ws:
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [{}])
                    is_final = data.get("is_final", False)
                    speech_final = data.get("speech_final", False)

                    if alternatives and alternatives[0].get("transcript"):
                        alt = alternatives[0]
                        transcript = alt["transcript"]
                        confidence = alt.get("confidence", 0.0)

                        if not trace.first_result_time:
                            trace.mark_first_result()
                        self._health.record_success(trace.time_to_first_result_ms)

                        # Only log final/speech_final at info level, partials at debug
                        if is_final or speech_final:
                            logger.info("continuous_stt_result",
                                turn=turn, is_final=is_final,
                                speech_final=speech_final,
                                confidence=round(confidence, 3),
                                text=transcript[:80])
                        else:
                            logger.debug("continuous_stt_result",
                                turn=turn, is_final=is_final,
                                speech_final=speech_final,
                                confidence=round(confidence, 3),
                                text=transcript[:80])

                        yield {
                            "text": transcript,
                            "is_final": is_final,
                            "speech_final": speech_final,
                            "confidence": confidence,
                            "latency_ms": trace.time_to_first_result_ms,
                        }

                    elif is_final:
                        # Final result but empty transcript (silence segment)
                        yield {
                            "text": "",
                            "is_final": True,
                            "speech_final": speech_final,
                            "confidence": 0.0,
                            "latency_ms": 0,
                        }

                elif msg_type == "SpeechStarted":
                    logger.info("continuous_speech_started", turn=turn)
                    yield {"event": "speech_started", "timestamp": time.time()}

                elif msg_type == "UtteranceEnd":
                    logger.info("continuous_utterance_end", turn=turn)
                    yield {"event": "utterance_end", "timestamp": time.time()}

                elif msg_type == "Metadata":
                    pass  # Ignore metadata

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("continuous_ws_closed", turn=turn, code=e.code)
            self._health.record_error()
            self._connected = False
        except Exception as e:
            logger.error("continuous_receive_error", turn=turn,
                error=str(e), error_type=type(e).__name__)
        finally:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
            logger.info("continuous_stt_ended", turn=turn,
                audio_bytes_sent=audio_bytes_sent)

    async def disconnect(self) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._connected = False
            logger.info("deepgram_stt_disconnected")

    def get_health(self) -> ProviderHealth:
        return self._health
