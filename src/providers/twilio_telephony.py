"""Twilio Telephony Provider — WebSocket Media Streams integration."""
import asyncio
import base64
import json
import time
import audioop
import structlog
from typing import Optional, Callable, Awaitable

try:
    from twilio.rest import Client
    from twilio.twiml.voice_response import VoiceResponse, Connect
except ImportError:
    raise ImportError("twilio not installed. Run: pip install twilio")

logger = structlog.get_logger()


class TwilioTelephony:
    """Manages Twilio calls with real-time audio via Media Streams."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        api_key_sid: Optional[str] = None,
        api_key_secret: Optional[str] = None,
    ):
        self.client = Client(account_sid, auth_token)
        self.phone_number = phone_number
        self.api_key_sid = api_key_sid
        self.api_key_secret = api_key_secret
        self._active_streams = {}  # call_id -> stream info
        self._websockets = {}  # call_id -> websocket (for sending clear messages)
        self._ratecv_state = None  # Persistent state for audioop.ratecv output (prevents clicks)
        self._ratecv_state_up = None  # Persistent state for input upsampling (8k→16k)

    async def send_clear(self, call_id: str) -> bool:
        """Send a 'clear' event to Twilio to immediately stop playing buffered audio.

        This is CRITICAL for interruption handling. When the user speaks over the AI,
        we clear our own output queue but Twilio may still have 1-3 seconds of audio
        buffered. The 'clear' event tells Twilio to discard all buffered audio
        instantly, so the user doesn't hear the AI keep talking after interrupting.

        Returns True if clear was sent successfully.
        """
        ws = self._websockets.get(call_id)
        stream_info = self._active_streams.get(call_id)
        if not ws or not stream_info:
            logger.debug("clear_no_stream", call_id=call_id)
            return False

        stream_sid = stream_info.get("stream_sid")
        if not stream_sid:
            logger.debug("clear_no_stream_sid", call_id=call_id)
            return False

        try:
            clear_msg = json.dumps({
                "event": "clear",
                "streamSid": stream_sid,
            })
            await ws.send_text(clear_msg)
            logger.info("twilio_clear_sent", call_id=call_id, stream_sid=stream_sid)
            return True
        except Exception as e:
            logger.warning("twilio_clear_failed", call_id=call_id, error=str(e))
            return False

    def generate_inbound_twiml(self, call_id: str, ws_url: str) -> str:
        """Generate TwiML for inbound call that connects to our WebSocket."""
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=f"{ws_url}/v1/media-stream/{call_id}")
        response.append(connect)
        return str(response)

    async def make_outbound_call(self, to_number: str, call_id: str, ws_url: str,
                                amd_enabled: bool = False,
                                amd_callback_url: str = "") -> str:
        """Initiate outbound call, returns Twilio call SID."""
        twiml = self.generate_inbound_twiml(call_id, ws_url)

        call_kwargs = {
            "to": to_number,
            "from_": self.phone_number,
            "twiml": twiml,
        }

        if amd_enabled:
            call_kwargs["machine_detection"] = "Enable"
            call_kwargs["async_amd"] = "true"
            call_kwargs["machine_detection_timeout"] = 8
            call_kwargs["machine_detection_speech_threshold"] = 1500
            call_kwargs["machine_detection_speech_end_threshold"] = 800
            call_kwargs["machine_detection_silence_timeout"] = 3000
            if amd_callback_url:
                call_kwargs["async_amd_status_callback"] = amd_callback_url
                call_kwargs["async_amd_status_callback_method"] = "POST"

        call = self.client.calls.create(**call_kwargs)
        logger.info("outbound_call_initiated",
            call_sid=call.sid, to=to_number, call_id=call_id,
        )
        return call.sid

    async def handle_media_stream(
        self,
        websocket,
        call_id: str,
        on_audio: Callable[[bytes], Awaitable[None]],
        get_audio: Callable[[], Awaitable[Optional[bytes]]],
    ):
        """Handle Twilio Media Stream WebSocket connection.

        Converts mulaw 8kHz audio from Twilio to PCM 16kHz for pipeline,
        and converts PCM 16kHz responses back to mulaw 8kHz for Twilio.

        CRITICAL: The send loop must wait for the streamSid (received in
        the `start` event) before sending any audio. Twilio silently drops
        media messages that don't include the correct streamSid.
        """
        # Shared state between send and receive loops
        stream_sid: dict = {"value": None}  # Mutable container for sharing
        stream_sid_ready = asyncio.Event()
        send_task = None
        receive_task = None

        # Store websocket reference for clear messages during barge-in
        self._websockets[call_id] = websocket

        try:
            send_task = asyncio.create_task(
                self._send_audio_loop(websocket, get_audio, stream_sid, stream_sid_ready)
            )
            receive_task = asyncio.create_task(
                self._receive_audio_loop(websocket, call_id, on_audio, stream_sid, stream_sid_ready)
            )

            done, pending = await asyncio.wait(
                [send_task, receive_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error("media_stream_error", call_id=call_id, error=str(e))
        finally:
            # Clean up websocket and stream references
            self._websockets.pop(call_id, None)
            self._active_streams.pop(call_id, None)
            if send_task and not send_task.done():
                send_task.cancel()
            if receive_task and not receive_task.done():
                receive_task.cancel()

    async def _receive_audio_loop(
        self,
        websocket,
        call_id: str,
        on_audio: Callable[[bytes], Awaitable[None]],
        stream_sid: dict,
        stream_sid_ready: asyncio.Event,
    ):
        """Receive audio from Twilio and convert to PCM 16kHz."""
        try:
            async for message in websocket.iter_text():
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")

                if event == "connected":
                    logger.info("twilio_media_stream_connected", call_id=call_id)

                elif event == "start":
                    # Capture the streamSid — REQUIRED for sending audio back
                    start_data = data.get("start", {})
                    sid = start_data.get("streamSid", "")
                    call_sid_from_stream = start_data.get("callSid", "")
                    stream_sid["value"] = sid
                    stream_sid_ready.set()  # Signal the send loop
                    self._active_streams[call_id] = {
                        "stream_sid": sid,
                        "call_sid": call_sid_from_stream,
                    }
                    logger.info("twilio_media_stream_started",
                        call_id=call_id, stream_sid=sid,
                        call_sid=call_sid_from_stream)

                elif event == "media":
                    payload = data.get("media", {}).get("payload")
                    if payload:
                        mulaw_data = base64.b64decode(payload)
                        pcm_16k = self.mulaw_8k_to_pcm_16k(mulaw_data)
                        try:
                            await on_audio(pcm_16k)
                        except Exception as e:
                            logger.warning("on_audio_callback_error",
                                call_id=call_id, error=str(e))

                elif event == "stop":
                    logger.info("twilio_media_stream_stopped", call_id=call_id)
                    break

        except Exception as e:
            logger.error("receive_audio_error", call_id=call_id, error=str(e))

    async def _send_audio_loop(
        self,
        websocket,
        get_audio: Callable[[], Awaitable[Optional[bytes]]],
        stream_sid: dict,
        stream_sid_ready: asyncio.Event,
    ):
        """Send audio to Twilio, converting PCM 16kHz to mulaw 8kHz.

        CRITICAL: Waits for streamSid before sending any audio.
        Every outgoing message MUST include streamSid or Twilio drops it.
        """
        try:
            # Wait for the stream to be ready (streamSid received)
            # Timeout after 30s — if Twilio never sends start event, something is wrong
            try:
                await asyncio.wait_for(stream_sid_ready.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("stream_sid_timeout", msg="Never received streamSid from Twilio")
                return

            sid = stream_sid["value"]
            logger.info("send_loop_ready", stream_sid=sid)

            # Strict 20ms pacer: ensures consistent frame delivery to Twilio
            # Without pacing, bursts of audio cause jitter artifacts
            FRAME_INTERVAL_S = 0.02  # 20ms per frame
            next_send_time = time.time()

            while True:
                pcm_data = await get_audio()

                # None = call ended
                if pcm_data is None:
                    break

                # Skip empty audio (no data to send yet)
                if not pcm_data or len(pcm_data) < 2:
                    await asyncio.sleep(0.02)  # 20ms to avoid busy loop
                    next_send_time = time.time() + FRAME_INTERVAL_S
                    continue

                # Validate PCM frame before conversion — catch garbled data early
                # Valid PCM 16kHz frames should be multiples of 2 bytes (16-bit samples)
                if len(pcm_data) % 2 != 0:
                    logger.warning("odd_pcm_frame_size", size=len(pcm_data))
                    pcm_data = pcm_data[:len(pcm_data) - 1]  # Trim to even
                    if len(pcm_data) < 2:
                        continue

                # Convert PCM 16kHz to mulaw 8kHz for Twilio
                # Always use the proven PCM conversion pipeline
                try:
                    mulaw_data = self.pcm_16k_to_mulaw_8k(pcm_data)
                except Exception as conv_err:
                    logger.warning("pcm_to_mulaw_conversion_error", error=str(conv_err))
                    continue  # Skip bad audio frames

                # Pace: wait until the next 20ms slot
                now = time.time()
                if now < next_send_time:
                    await asyncio.sleep(next_send_time - now)
                next_send_time = max(time.time(), next_send_time) + FRAME_INTERVAL_S

                payload = base64.b64encode(mulaw_data).decode("utf-8")

                # Send to Twilio — MUST include streamSid
                message = json.dumps({
                    "event": "media",
                    "streamSid": sid,
                    "media": {"payload": payload},
                })
                await websocket.send_text(message)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("send_audio_error", error=str(e))

    def mulaw_8k_to_pcm_16k(self, mulaw_data: bytes) -> bytes:
        """Convert mulaw 8kHz (Twilio) to PCM 16-bit 16kHz (pipeline).
        Maintains ratecv state across frames to prevent clicks at frame boundaries."""
        pcm_8k = audioop.ulaw2lin(mulaw_data, 2)
        pcm_16k, self._ratecv_state_up = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, self._ratecv_state_up)
        return pcm_16k

    def reset_audio_state(self):
        """Reset ratecv state — call after barge-in or audio clear to avoid
        stale state causing artifacts at the start of new audio."""
        self._ratecv_state = None
        self._ratecv_state_up = None
        self._lpf_hist = [0, 0]  # Reset low-pass filter state too

    def pcm_16k_to_mulaw_8k(self, pcm_data: bytes) -> bytes:
        """Convert PCM 16-bit 16kHz (pipeline) to mulaw 8kHz (Twilio).

        Pipeline: 4-tap FIR low-pass filter → downsample → mulaw encode.
        The low-pass filter prevents aliasing artifacts when downsampling
        from 16kHz to 8kHz (Nyquist: must remove >4kHz before decimation).
        Without it, audioop.ratecv introduces harsh metallic artifacts.
        Maintains ratecv state across frames to prevent click/pop at boundaries.
        """
        import struct
        n_samples = len(pcm_data) // 2
        if n_samples > 1:
            samples = struct.unpack(f'<{n_samples}h', pcm_data[:n_samples * 2])

            # 4-tap FIR low-pass filter: y[n] = (x[n] + 2*x[n-1] + x[n-2]) / 4
            # Windowed sinc approximation — ~12dB attenuation at Nyquist (4kHz)
            # Uses persistent state across frames for seamless boundaries
            if not hasattr(self, '_lpf_hist'):
                self._lpf_hist = [0, 0]  # [x[n-2], x[n-1]]

            filtered = []
            h1, h2 = self._lpf_hist  # h1 = x[n-2], h2 = x[n-1]
            for s in samples:
                # Triangular-weighted 3-point FIR: [1, 2, 1] / 4
                out = (h1 + (h2 << 1) + s) >> 2
                # Clamp to int16 range
                out = max(-32768, min(32767, out))
                filtered.append(out)
                h1 = h2
                h2 = s
            self._lpf_hist = [h1, h2]

            pcm_data = struct.pack(f'<{n_samples}h', *filtered)

        # Step 2: Downsample 16kHz → 8kHz
        pcm_8k, self._ratecv_state = audioop.ratecv(
            pcm_data, 2, 1, 16000, 8000, self._ratecv_state)
        # Step 3: Encode as mulaw
        mulaw_data = audioop.lin2ulaw(pcm_8k, 2)
        return mulaw_data
