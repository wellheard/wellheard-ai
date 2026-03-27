"""SignalWire Telephony Provider — Twilio-compatible API with SignalWire endpoints.

SignalWire uses the same Media Streams protocol as Twilio (mulaw 8kHz, JSON-wrapped
base64 audio, streamSid required). The only difference is the REST API base URL
and authentication — everything else is wire-compatible.

This extends TwilioTelephony to reuse 100% of the WebSocket/audio handling code,
and only overrides the REST API client initialization and outbound call method.
"""
import asyncio
import json
import structlog
import httpx
import requests
from typing import Optional, Callable, Awaitable

from .twilio_telephony import TwilioTelephony

logger = structlog.get_logger()


# ── Twilio-SDK-compatible adapter for warm_transfer.py ────────────────────
# The warm transfer manager expects a synchronous Twilio-like client with
# .calls.create(), .calls(sid).update(), .calls(sid).fetch() patterns.
# This adapter wraps SignalWire's LAML REST API with that interface.

class _CallResult:
    """Mimics a Twilio Call resource."""
    def __init__(self, data: dict):
        self.sid = data.get("sid", "")
        self.status = data.get("status", "")

class _CallInstance:
    """Mimics twilio.calls(sid) — supports .update() and .fetch()."""
    def __init__(self, base_url: str, auth: tuple, call_sid: str):
        self._url = f"{base_url}/Calls/{call_sid}.json"
        self._auth = auth
        self.sid = call_sid

    def update(self, status: str = None, twiml: str = None, **kwargs) -> '_CallResult':
        data = {}
        if status:
            data["Status"] = status
        if twiml:
            data["Twiml"] = twiml
        data.update(kwargs)
        resp = requests.post(self._url, data=data, auth=self._auth, timeout=10)
        if resp.status_code in (200, 201):
            return _CallResult(resp.json())
        logger.error("signalwire_call_update_failed",
            status=resp.status_code, error=resp.text)
        return _CallResult({"sid": self.sid, "status": "failed"})

    def fetch(self) -> '_CallResult':
        resp = requests.get(self._url, auth=self._auth, timeout=10)
        if resp.status_code == 200:
            return _CallResult(resp.json())
        return _CallResult({"sid": self.sid, "status": "unknown"})

class _CallsProxy:
    """Mimics twilio.calls — supports .create() and .__call__(sid)."""
    def __init__(self, base_url: str, auth: tuple, default_from: str):
        self._base_url = base_url
        self._auth = auth
        self._default_from = default_from

    def __call__(self, call_sid: str) -> _CallInstance:
        return _CallInstance(self._base_url, self._auth, call_sid)

    def create(self, to: str = "", from_: str = "", twiml: str = None,
               url: str = None, method: str = None, **kwargs) -> _CallResult:
        data = {
            "To": to,
            "From": from_ or self._default_from,
        }
        if twiml:
            data["Twiml"] = twiml
        if url:
            data["Url"] = url
        if method:
            data["Method"] = method
        data.update(kwargs)

        resp = requests.post(
            f"{self._base_url}/Calls.json",
            data=data, auth=self._auth, timeout=15,
        )
        if resp.status_code in (200, 201):
            result = _CallResult(resp.json())
            logger.info("signalwire_transfer_call_created",
                call_sid=result.sid, to=to)
            return result
        logger.error("signalwire_transfer_call_failed",
            status=resp.status_code, error=resp.text, to=to)
        raise Exception(f"SignalWire call create failed ({resp.status_code}): {resp.text}")

class SignalWireClient:
    """Twilio-SDK-compatible synchronous client for SignalWire LAML API.

    Provides .calls.create() / .calls(sid).update() / .calls(sid).fetch()
    interface that warm_transfer.py expects from a Twilio REST client.
    """
    def __init__(self, project_id: str, api_token: str, space_name: str, phone_number: str):
        base_url = f"https://{space_name}.signalwire.com/2010-04-01/Accounts/{project_id}"
        auth = (project_id, api_token)
        self.calls = _CallsProxy(base_url, auth, phone_number)


class SignalWireTelephony(TwilioTelephony):
    """SignalWire telephony — inherits all Twilio WebSocket/audio handling.

    SignalWire's LAML API is wire-compatible with Twilio's REST API.
    Media Streams use the same mulaw 8kHz JSON protocol, same streamSid,
    same clear events. We only override the call creation to use httpx
    directly (avoiding the twilio SDK's hardcoded api.twilio.com endpoint).
    """

    def __init__(
        self,
        project_id: str,
        api_token: str,
        space_name: str,
        phone_number: str,
    ):
        # Initialize parent with project_id as account_sid and api_token as auth_token
        # This sets up all the mulaw conversion, send_clear, etc.
        # We don't actually use self.client for SignalWire calls — we use httpx
        self.project_id = project_id
        self.api_token = api_token
        self.space_name = space_name
        self.phone_number = phone_number
        self._active_streams = {}
        self._websockets = {}
        self._ratecv_state = None
        self._ratecv_state_up = None  # Persistent state for input upsampling (8k→16k)
        self._base_url = f"https://{space_name}.signalwire.com/2010-04-01/Accounts/{project_id}"

        # Set client to None — we use httpx for SignalWire REST calls
        self.client = None

    def generate_inbound_twiml(self, call_id: str, ws_url: str) -> str:
        """Generate TwiML for inbound call — same format as Twilio."""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Connect><Stream url="'
            f'{ws_url}/v1/media-stream/{call_id}'
            '"/></Connect></Response>'
        )

    async def make_outbound_call(
        self,
        to_number: str,
        call_id: str,
        ws_url: str,
        amd_enabled: bool = False,
        amd_callback_url: str = "",
    ) -> str:
        """Initiate outbound call via SignalWire LAML API.

        Returns SignalWire call SID (equivalent to Twilio call SID).
        """
        twiml = self.generate_inbound_twiml(call_id, ws_url)

        data = {
            "To": to_number,
            "From": self.phone_number,
            "Twiml": twiml,
        }

        if amd_enabled:
            data["MachineDetection"] = "Enable"
            data["AsyncAmd"] = "true"
            data["MachineDetectionTimeout"] = "8"
            if amd_callback_url:
                data["AsyncAmdStatusCallback"] = amd_callback_url
                data["AsyncAmdStatusCallbackMethod"] = "POST"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/Calls.json",
                data=data,
                auth=(self.project_id, self.api_token),
                timeout=15,
            )

        if resp.status_code not in (200, 201):
            error_body = resp.text
            logger.error("signalwire_call_failed",
                status=resp.status_code, error=error_body,
                to=to_number, call_id=call_id)
            raise Exception(f"SignalWire call failed ({resp.status_code}): {error_body}")

        result = resp.json()
        call_sid = result.get("sid", "")
        logger.info("signalwire_call_initiated",
            call_sid=call_sid, to=to_number, call_id=call_id,
            status=result.get("status"))
        return call_sid

    async def hangup_call(self, call_sid: str) -> bool:
        """Hang up an active call."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/Calls/{call_sid}.json",
                    data={"Status": "completed"},
                    auth=(self.project_id, self.api_token),
                    timeout=10,
                )
            return resp.status_code == 200
        except Exception as e:
            logger.error("signalwire_hangup_failed", call_sid=call_sid, error=str(e))
            return False

    async def transfer_call(self, call_sid: str, to_number: str) -> bool:
        """Transfer an active call to another number."""
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Dial>{to_number}</Dial></Response>'
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/Calls/{call_sid}.json",
                    data={"Twiml": twiml},
                    auth=(self.project_id, self.api_token),
                    timeout=10,
                )
            return resp.status_code == 200
        except Exception as e:
            logger.error("signalwire_transfer_failed", call_sid=call_sid, error=str(e))
            return False
