"""Vonage Voice API Telephony Provider — WebSocket audio streaming integration.

Vonage connects calls to WebSocket endpoints and streams raw PCM audio.
Unlike Twilio (mulaw 8kHz), Vonage supports 16kHz PCM natively, so no
audio conversion is needed — PCM flows directly to/from the AI pipeline.

Vonage uses NCCO (Nexmo Call Control Objects) instead of TwiML.
Docs: https://developer.vonage.com/en/voice/voice-api/overview
"""
import asyncio
import base64
import json
import time
import structlog
import httpx
from typing import Optional, Callable, Awaitable

logger = structlog.get_logger()


class VonageTelephony:
    """Manages Vonage calls with real-time audio via WebSocket streaming.

    Key difference from Twilio: Vonage streams raw PCM 16kHz audio over
    WebSocket, eliminating the need for mulaw↔PCM conversion. This reduces
    latency and CPU overhead.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        application_id: str,
        private_key: str,
        phone_number: str,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.application_id = application_id
        self.private_key = private_key
        self.phone_number = phone_number
        self._active_streams = {}  # call_id -> stream info
        self._websockets = {}  # call_id -> websocket (for sending clear/stop)

    def _generate_jwt(self) -> str:
        """Generate a JWT for Vonage API authentication.

        Vonage uses JWTs signed with the application's private key.
        """
        import jwt as pyjwt
        import time
        import uuid

        payload = {
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,  # 1 hour
            "jti": str(uuid.uuid4()),
            "application_id": self.application_id,
        }
        return pyjwt.encode(payload, self.private_key, algorithm="RS256")

    def generate_answer_ncco(self, call_id: str, ws_url: str) -> list:
        """Generate NCCO (Nexmo Call Control Object) for connecting to WebSocket.

        Vonage uses NCCO instead of TwiML. The 'connect' action with a
        WebSocket endpoint streams audio bidirectionally.

        Audio format: signed 16-bit PCM at 16kHz (audio/l16;rate=16000)
        This matches our pipeline's native format — no conversion needed.
        """
        return [
            {
                "action": "connect",
                "endpoint": [
                    {
                        "type": "websocket",
                        "uri": f"{ws_url}/v1/media-stream/{call_id}",
                        "content-type": "audio/l16;rate=16000",
                        "headers": {
                            "call_id": call_id,
                        },
                    }
                ],
            }
        ]

    async def make_outbound_call(
        self,
        to_number: str,
        call_id: str,
        ws_url: str,
        amd_enabled: bool = False,
        amd_callback_url: str = "",
    ) -> str:
        """Initiate outbound call via Vonage Voice API.

        Returns Vonage call UUID.
        """
        ncco = self.generate_answer_ncco(call_id, ws_url)

        # Vonage Voice API endpoint
        url = "https://api.nexmo.com/v1/calls"
        headers = {
            "Authorization": f"Bearer {self._generate_jwt()}",
            "Content-Type": "application/json",
        }

        body = {
            "to": [{"type": "phone", "number": to_number.replace("+", "")}],
            "from": {"type": "phone", "number": self.phone_number.replace("+", "")},
            "ncco": ncco,
        }

        if amd_enabled:
            body["machine_detection"] = "hangup"  # or "continue"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=body)

            if resp.status_code not in (200, 201):
                logger.error(
                    "vonage_call_failed",
                    status=resp.status_code,
                    body=resp.text,
                    to=to_number,
                )
                raise RuntimeError(f"Vonage call failed: {resp.status_code} {resp.text}")

            data = resp.json()
            call_uuid = data.get("uuid", "")

            logger.info(
                "outbound_call_initiated",
                call_uuid=call_uuid,
                to=to_number,
                call_id=call_id,
                provider="vonage",
            )
            return call_uuid

    async def send_clear(self, call_id: str) -> bool:
        """Stop buffered audio playback.

        Vonage doesn't have Twilio's explicit 'clear' event. Instead, we
        can send silence bytes or simply stop sending and let the WebSocket
        buffer drain. For true interruption handling, we close and re-open
        the audio stream or send empty frames.

        In practice with Vonage's WebSocket, audio is delivered frame-by-frame
        with minimal buffering, so interruption handling is simpler than Twilio.
        """
        ws = self._websockets.get(call_id)
        if not ws:
            logger.debug("clear_no_ws", call_id=call_id)
            return False

        try:
            # Send 200ms of silence to flush any buffered audio
            silence = b"\x00" * 6400  # 200ms of 16kHz 16-bit PCM silence
            await ws.send_bytes(silence)
            logger.info("vonage_clear_sent", call_id=call_id)
            return True
        except Exception as e:
            logger.warning("vonage_clear_failed", call_id=call_id, error=str(e))
            return False

    def reset_audio_state(self):
        """Reset audio state — no-op for Vonage since no mulaw conversion state.

        Vonage streams PCM 16kHz natively, so there's no ratecv/filter state
        to reset (unlike Twilio which needs mulaw↔PCM conversion state).
        """
        pass

    async def handle_media_stream(
        self,
        websocket,
        call_id: str,
        on_audio: Callable[[bytes], Awaitable[None]],
        get_audio: Callable[[], Awaitable[Optional[bytes]]],
    ):
        """Handle Vonage WebSocket audio stream.

        Vonage sends raw binary PCM 16kHz audio frames over WebSocket.
        Unlike Twilio which wraps audio in JSON with base64 encoding,
        Vonage sends/receives raw binary frames directly.

        Protocol:
          - First message: JSON with metadata (uuid, content-type, etc.)
          - Subsequent messages: Raw binary PCM audio frames
          - Bidirectional: we receive caller audio and send AI audio
        """
        send_task = None
        receive_task = None
        stream_ready = asyncio.Event()

        # Store websocket reference
        self._websockets[call_id] = websocket

        try:
            send_task = asyncio.create_task(
                self._send_audio_loop(websocket, get_audio, stream_ready)
            )
            receive_task = asyncio.create_task(
                self._receive_audio_loop(websocket, call_id, on_audio, stream_ready)
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
            logger.error("vonage_media_stream_error", call_id=call_id, error=str(e))
        finally:
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
        stream_ready: asyncio.Event,
    ):
        """Receive audio from Vonage WebSocket.

        Vonage sends:
        1. First message: JSON with call metadata
        2. All subsequent messages: Raw binary PCM 16kHz audio

        No conversion needed — PCM 16kHz goes directly to the pipeline.
        """
        first_message = True
        try:
            async for message in websocket.iter_bytes():
                if first_message:
                    # First binary message might be metadata or audio
                    # Vonage can also send a text JSON message first
                    first_message = False
                    stream_ready.set()
                    self._active_streams[call_id] = {"connected": True}
                    logger.info("vonage_media_stream_started", call_id=call_id)

                # Raw PCM 16kHz audio — pass directly to pipeline
                if len(message) > 0:
                    try:
                        await on_audio(message)
                    except Exception as e:
                        logger.warning(
                            "on_audio_callback_error",
                            call_id=call_id,
                            error=str(e),
                        )

        except Exception as e:
            if "1000" not in str(e) and "1001" not in str(e):
                logger.error("vonage_receive_error", call_id=call_id, error=str(e))
            else:
                logger.info("vonage_media_stream_closed", call_id=call_id)

    async def _send_audio_loop(
        self,
        websocket,
        get_audio: Callable[[], Awaitable[Optional[bytes]]],
        stream_ready: asyncio.Event,
    ):
        """Send audio to Vonage WebSocket.

        Sends raw PCM 16kHz audio directly — no mulaw conversion needed.
        Paced at 20ms intervals for smooth playback.
        """
        try:
            # Wait for stream to be ready
            try:
                await asyncio.wait_for(stream_ready.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("vonage_stream_timeout", msg="Never received first audio from Vonage")
                return

            logger.info("vonage_send_loop_ready")

            # 20ms frame pacing for consistent audio delivery
            FRAME_INTERVAL_S = 0.02
            next_send_time = time.time()

            while True:
                pcm_data = await get_audio()

                # None = call ended
                if pcm_data is None:
                    break

                # Skip empty audio
                if not pcm_data or len(pcm_data) < 2:
                    await asyncio.sleep(0.02)
                    next_send_time = time.time() + FRAME_INTERVAL_S
                    continue

                # Ensure even byte count (16-bit samples)
                if len(pcm_data) % 2 != 0:
                    pcm_data = pcm_data[: len(pcm_data) - 1]
                    if len(pcm_data) < 2:
                        continue

                # Pace: wait until the next 20ms slot
                now = time.time()
                if now < next_send_time:
                    await asyncio.sleep(next_send_time - now)
                next_send_time = max(time.time(), next_send_time) + FRAME_INTERVAL_S

                # Send raw PCM directly — no mulaw conversion needed!
                await websocket.send_bytes(pcm_data)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("vonage_send_error", error=str(e))

    async def hangup_call(self, call_uuid: str):
        """Hang up an active Vonage call."""
        url = f"https://api.nexmo.com/v1/calls/{call_uuid}"
        headers = {
            "Authorization": f"Bearer {self._generate_jwt()}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(url, headers=headers, json={"action": "hangup"})
            if resp.status_code == 204:
                logger.info("vonage_hangup_success", call_uuid=call_uuid)
            else:
                logger.warning(
                    "vonage_hangup_failed",
                    call_uuid=call_uuid,
                    status=resp.status_code,
                )

    async def transfer_call(self, call_uuid: str, transfer_ncco: list):
        """Transfer an active call using a new NCCO."""
        url = f"https://api.nexmo.com/v1/calls/{call_uuid}"
        headers = {
            "Authorization": f"Bearer {self._generate_jwt()}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                url,
                headers=headers,
                json={"action": "transfer", "destination": {"type": "ncco", "ncco": transfer_ncco}},
            )
            if resp.status_code == 204:
                logger.info("vonage_transfer_success", call_uuid=call_uuid)
            else:
                logger.warning(
                    "vonage_transfer_failed",
                    call_uuid=call_uuid,
                    status=resp.status_code,
                )
