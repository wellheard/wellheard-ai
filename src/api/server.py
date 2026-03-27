"""
WellHeard AI - FastAPI Server
Unified REST + WebSocket API for voice AI integration.

Endpoints:
  POST   /v1/calls              - Start a new voice AI call
  DELETE /v1/calls/{call_id}    - End an active call
  GET    /v1/calls/{call_id}    - Get call status & metrics
  GET    /v1/health             - Platform health check
  GET    /v1/dashboard          - Platform-wide metrics
  WS     /v1/ws/{call_id}      - Real-time call events stream
"""
import asyncio
import uuid
import time
import structlog
from typing import Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Response, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from contextlib import asynccontextmanager

from config.settings import settings, PipelineMode
from .models import (
    StartCallRequest, EndCallRequest, CallResponse, CallMetricsResponse,
    HealthResponse, DashboardResponse, ErrorResponse, CallStatus,
    PipelineMode as APIPipelineMode,
)
from ..pipelines.budget_pipeline import BudgetPipeline
from ..pipelines.quality_pipeline import QualityPipeline
from ..pipelines.orchestrator import AgentConfig
from ..monitoring.metrics import metrics_collector
from ..providers.twilio_telephony import TwilioTelephony
from ..providers.vonage_telephony import VonageTelephony
from ..providers.signalwire_telephony import SignalWireTelephony
from ..warm_transfer import WarmTransferManager
from ..transfer_endpoints import router as transfer_router
from ..tenant_endpoints import router as tenant_router
from ..call_bridge import CallBridge
from ..memory import ConversationMemory
from ..dispositions import DispositionEngine, DispositionSignals, CallDisposition
from ..monitor import HealthMonitor, AlertLevel, configure_monitor
from ..ab_testing import (
    get_ab_test_manager, initialize_default_experiments, Variant,
)

logger = structlog.get_logger()

# Shared instances
conversation_memory = ConversationMemory()
disposition_engine = DispositionEngine()
health_monitor: HealthMonitor | None = None
security = HTTPBearer()

# ── Active calls registry ─────────────────────────────────────────────────
active_calls: dict[str, dict] = {}
call_websockets: dict[str, list[WebSocket]] = {}


# ── Auth ──────────────────────────────────────────────────────────────────
async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global health_monitor
    logger.info("wellheard_starting", version="1.0.0")

    # Initialize A/B test manager and default experiments
    try:
        await initialize_default_experiments()
        logger.info("ab_test_manager_initialized")
    except Exception as e:
        logger.warning("ab_test_manager_init_failed", error=str(e))

    # Start health monitor (background task)
    try:
        health_monitor = configure_monitor(
            check_interval_seconds=300,
            alert_email="jj@crowns.cc",
        )
        await health_monitor.start()
        logger.info("health_monitor_started")
    except Exception as e:
        logger.warning("health_monitor_start_failed", error=str(e))

    yield

    # Cleanup: stop monitor and end all active calls
    if health_monitor:
        try:
            await health_monitor.stop()
        except Exception:
            pass

    for call_id in list(active_calls.keys()):
        try:
            call_data = active_calls[call_id]
            if call_data.get("orchestrator"):
                await call_data["orchestrator"].end_call()
        except Exception:
            pass
    logger.info("wellheard_stopped")


# ── Memory persistence helpers ───────────────────────────────────────────

async def _load_lead_data(lead_id: str) -> dict:
    """Load lead fields needed for memory injection."""
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        db_url = getattr(settings, "database_url", "sqlite:///wellheard.db")
        engine = create_engine(db_url)
        with Session(engine) as session:
            row = session.execute(
                text("SELECT * FROM leads WHERE id = :id"), {"id": lead_id}
            ).mappings().first()
            if row:
                return dict(row)
    except Exception as e:
        logger.warning("lead_load_fallback", error=str(e))
        # If DB not set up yet, check active_calls for any cached lead_data
    return {}


async def _save_memory_to_db(
    lead_id: str,
    call_id: str,
    lead_updates: dict,
    call_log_updates: dict,
    transcript: list,
    duration: float,
    call_data: dict,
):
    """Persist memory updates to Lead and CallLog in the database."""
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        import json

        db_url = getattr(settings, "database_url", "sqlite:///wellheard.db")
        engine = create_engine(db_url)

        with Session(engine) as session:
            # Update Lead memory fields
            if lead_updates:
                set_clauses = []
                params = {"lead_id": lead_id}
                for key, value in lead_updates.items():
                    if isinstance(value, (list, dict)):
                        params[key] = json.dumps(value)
                    else:
                        params[key] = value
                    set_clauses.append(f"{key} = :{key}")

                # Also increment attempt_count and update last_called_at
                set_clauses.append("attempt_count = attempt_count + 1")
                set_clauses.append("last_called_at = CURRENT_TIMESTAMP")
                set_clauses.append("updated_at = CURRENT_TIMESTAMP")

                sql = f"UPDATE leads SET {', '.join(set_clauses)} WHERE id = :lead_id"
                session.execute(text(sql), params)

            # Insert CallLog with memory fields
            call_log_params = {
                "id": call_id,
                "company_id": call_data.get("company_id", ""),
                "campaign_id": call_data.get("campaign_id", ""),
                "lead_id": lead_id,
                "call_sid": call_data.get("call_sid", ""),
                "duration_seconds": duration,
                "pipeline_mode": call_data.get("pipeline_mode", ""),
                "transcript": json.dumps(transcript),
                "call_summary": call_log_updates.get("call_summary", ""),
                "objections_detected": json.dumps(call_log_updates.get("objections_detected", [])),
                "sentiment": call_log_updates.get("sentiment", ""),
                "next_action": call_log_updates.get("next_action", ""),
            }

            cols = ", ".join(call_log_params.keys())
            vals = ", ".join(f":{k}" for k in call_log_params.keys())
            sql = f"INSERT OR REPLACE INTO call_logs ({cols}) VALUES ({vals})"
            session.execute(text(sql), call_log_params)

            session.commit()
            logger.info("memory_db_saved", lead_id=lead_id, call_id=call_id)

    except Exception as e:
        logger.error("memory_db_error", error=str(e), lead_id=lead_id)


def create_app() -> FastAPI:
    app = FastAPI(
        title="WellHeard AI",
        description="Voice AI Platform API - Budget ($0.021/min) & Quality ($0.032/min) pipelines",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register the transfer router
    app.include_router(transfer_router)
    app.include_router(tenant_router)

    # ── POST /v1/calls - Start a new call ─────────────────────────────────
    @app.post("/v1/calls", response_model=CallResponse, tags=["Calls"])
    async def start_call(request: StartCallRequest, api_key: str = Depends(verify_api_key)):
        """
        Start a new voice AI call.

        Choose pipeline:
        - **budget**: ~$0.021/min (Deepgram STT + Groq Llama + Deepgram Aura TTS)
        - **quality**: ~$0.032/min (Deepgram STT + Gemini Flash + Cartesia Sonic TTS)
        """
        call_id = str(uuid.uuid4())

        # Create the appropriate pipeline
        # Configure the agent — resolve preset configs by agent_id
        from ..inbound_handler import OUTBOUND_CONFIG, INBOUND_CONFIG
        AGENT_PRESETS = {
            "outbound_sdr_becky": OUTBOUND_CONFIG,
            "inbound_sdr_becky": INBOUND_CONFIG,
            "inbound_final_expense": INBOUND_CONFIG,  # legacy alias
            "final-expense-becky": OUTBOUND_CONFIG,   # API alias
            "default": OUTBOUND_CONFIG,                # Default outbound = Becky SDR
        }

        preset = AGENT_PRESETS.get(request.agent.agent_id)
        if preset and request.agent.system_prompt == "You are a helpful AI assistant. Be concise and natural.":
            # User requested a preset agent — load its full config
            agent_config = AgentConfig(
                agent_id=preset.get("agent_id", request.agent.agent_id),
                system_prompt=preset["system_prompt"],
                voice_id=preset.get("voice_id", ""),
                language=preset.get("language", "en"),
                temperature=preset.get("temperature", 0.7),
                max_tokens=preset.get("max_tokens", 150),
                interruption_enabled=preset.get("interruption_enabled", True),
                greeting=preset.get("greeting", ""),
                pitch_text=preset.get("pitch_text", ""),
                transfer_config=preset.get("transfer_config"),
                tools=request.agent.tools,
                speed=preset.get("speed", 1.0),
            )
            logger.info("agent_preset_loaded",
                call_id=call_id,
                preset=request.agent.agent_id,
                greeting=agent_config.greeting[:50],
                has_pitch=bool(agent_config.pitch_text),
            )
        else:
            agent_config = AgentConfig(
                agent_id=request.agent.agent_id,
                system_prompt=request.agent.system_prompt,
                voice_id=request.agent.voice_id,
                language=request.agent.language,
                temperature=request.agent.temperature,
                max_tokens=request.agent.max_tokens,
                interruption_enabled=request.agent.interruption_enabled,
                greeting=request.agent.greeting,
                tools=request.agent.tools,
            )

        # Create pipeline — auto-upgrade to quality when Cartesia voice is configured
        use_quality = (
            request.pipeline == APIPipelineMode.QUALITY
            or (agent_config.voice_id and agent_config.voice_id != "")
        )
        if use_quality:
            orchestrator = QualityPipeline.create()
            estimated_cost = QualityPipeline.ESTIMATED_COST_PER_MINUTE
        else:
            orchestrator = BudgetPipeline.create()
            estimated_cost = BudgetPipeline.ESTIMATED_COST_PER_MINUTE

        # Apply A/B test overrides if experiment_name is provided in request
        ab_test_experiment = getattr(request, 'ab_test_experiment', None)
        if ab_test_experiment:
            try:
                manager = await get_ab_test_manager()
                variant = await manager.assign_variant(call_id, ab_test_experiment)
                if variant:
                    exp_status = await manager.get_experiment_status(ab_test_experiment)
                    if variant == Variant.A:
                        overrides = exp_status["variant_a"]["config"]
                    else:
                        overrides = exp_status["variant_b"]["config"]

                    # Apply non-None overrides to agent_config
                    if overrides.get("temperature") is not None:
                        agent_config.temperature = overrides["temperature"]
                    if overrides.get("max_tokens") is not None:
                        agent_config.max_tokens = overrides["max_tokens"]
                    if overrides.get("speed") is not None:
                        # Speed will be handled during TTS synthesis (see CallBridge)
                        active_calls_data = {"ab_test_speed_override": overrides["speed"]}
                    if overrides.get("system_prompt") is not None:
                        agent_config.system_prompt = overrides["system_prompt"]

                    logger.info("ab_test_variant_applied",
                        call_id=call_id,
                        experiment=ab_test_experiment,
                        variant=variant.value,
                        overrides=overrides)
            except Exception as e:
                logger.warning("ab_test_override_failed",
                    call_id=call_id,
                    experiment=ab_test_experiment,
                    error=str(e))

        # Store call data
        active_calls[call_id] = {
            "orchestrator": orchestrator,
            "agent_config": agent_config,
            "request": request,
            "status": CallStatus.PENDING,
            "start_time": time.time(),
            "pipeline_mode": request.pipeline.value,
            "lead_id": request.lead_id,
            "campaign_id": request.campaign_id,
        }

        # Start the call
        try:
            # Create the bridge between Twilio media stream and orchestrator
            bridge = CallBridge(
                orchestrator=orchestrator,
                agent_config=agent_config,
                call_id=call_id,
            )
            active_calls[call_id]["bridge"] = bridge
            active_calls[call_id]["status"] = CallStatus.ACTIVE
            active_calls[call_id]["pipeline_mode_str"] = request.pipeline.value

            # PRE-CONNECT providers AND pre-synthesize greeting while phone rings.
            # This way, when the prospect answers, everything is ready — near-zero latency.
            t_pre = time.time()

            # Step 1: Connect TTS + STT providers now (during ring time)
            try:
                await asyncio.gather(
                    orchestrator.tts.connect(),
                    orchestrator.stt.connect(),
                )
                # Mark orchestrator as active
                from ..pipelines.orchestrator import CallMetrics
                orchestrator._active = True
                orchestrator._metrics = CallMetrics(pipeline_mode=request.pipeline.value)
                orchestrator._conversation_history = []
                orchestrator._cancel_tts.clear()
                active_calls[call_id]["providers_pre_connected"] = True
                provider_ms = (time.time() - t_pre) * 1000
                logger.info("providers_pre_connected_at_dial",
                    call_id=call_id, elapsed_ms=round(provider_ms, 1))
            except Exception as e:
                logger.warning("provider_pre_connect_failed",
                    call_id=call_id, error=str(e), error_type=type(e).__name__)
                active_calls[call_id]["providers_pre_connected"] = False

            # Step 2: Pre-synthesize greeting AND pitch during dial time
            # Both are generated as single seamless audio for perfect delivery
            try:
                await bridge.pre_synthesize_greeting()
                # Synthesize pitch, fillers, backchannel, and turn 1 cache in parallel
                await asyncio.gather(
                    bridge.pre_synthesize_pitch(),
                    bridge.pre_synthesize_fillers(),
                    bridge.pre_synthesize_backchannel_audio(),
                    bridge.pre_synthesize_turn1_cache(),
                    bridge.pre_synthesize_turn2_cache(),
                    bridge.pre_synthesize_semantic_cache(),
                    bridge.pre_synthesize_recovery_audio(),
                )
                greeting_bytes = len(bridge._greeting_audio or b'')
                pitch_bytes = len(bridge._pitch_audio or b'')
                logger.info("audio_pre_synthesized_at_dial",
                    call_id=call_id,
                    greeting_bytes=greeting_bytes,
                    pitch_bytes=pitch_bytes,
                    filler_count=len(bridge._filler_audio),
                    pitch_duration_ms=round(pitch_bytes / 32, 0) if pitch_bytes else 0,
                    elapsed_ms=round((time.time() - t_pre) * 1000, 1),
                )
            except Exception as e:
                logger.warning("pre_synthesis_at_dial_failed",
                    call_id=call_id, error=str(e), error_type=type(e).__name__)

            # Determine public base URL for Twilio WebSocket callback
            base_url = settings.base_url
            if not base_url:
                # Auto-detect from Fly.io or fallback
                import os
                fly_app = os.environ.get("FLY_APP_NAME")
                if fly_app:
                    base_url = f"https://{fly_app}.fly.dev"
                else:
                    base_url = f"http://{settings.host}:{settings.port}"

            # Convert https:// to wss:// for WebSocket URL
            ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

            # Dial the prospect via configured telephony provider
            if request.phone_number:
                if settings.telephony_provider == "signalwire" and settings.signalwire_project_id:
                    sw = SignalWireTelephony(
                        project_id=settings.signalwire_project_id,
                        api_token=settings.signalwire_api_token,
                        space_name=settings.signalwire_space_name,
                        phone_number=settings.signalwire_phone_number,
                    )
                    call_sid = await sw.make_outbound_call(
                        to_number=request.phone_number,
                        call_id=call_id,
                        ws_url=ws_url,
                    )
                    active_calls[call_id]["call_sid"] = call_sid
                    active_calls[call_id]["telephony"] = sw

                    if bridge:
                        bridge.twilio_call_sid = call_sid
                        bridge.twilio_telephony = sw  # Duck-typed: inherits TwilioTelephony
                        bridge.webhook_base_url = base_url
                        # Provide Twilio-SDK-compatible client for warm transfer
                        from ..providers.signalwire_telephony import SignalWireClient
                        bridge.twilio_client = SignalWireClient(
                            project_id=settings.signalwire_project_id,
                            api_token=settings.signalwire_api_token,
                            space_name=settings.signalwire_space_name,
                            phone_number=settings.signalwire_phone_number,
                        )

                    logger.info("signalwire_call_initiated",
                        call_id=call_id,
                        call_sid=call_sid,
                        to=request.phone_number,
                        ws_url=ws_url,
                    )

                elif settings.telephony_provider == "vonage" and settings.vonage_api_key:
                    vonage = VonageTelephony(
                        api_key=settings.vonage_api_key,
                        api_secret=settings.vonage_api_secret,
                        application_id=settings.vonage_application_id,
                        private_key=settings.vonage_private_key,
                        phone_number=settings.vonage_phone_number,
                    )
                    call_uuid = await vonage.make_outbound_call(
                        to_number=request.phone_number,
                        call_id=call_id,
                        ws_url=ws_url,
                    )
                    active_calls[call_id]["call_sid"] = call_uuid
                    active_calls[call_id]["vonage_telephony"] = vonage

                    if bridge:
                        bridge.twilio_call_sid = call_uuid
                        bridge.twilio_telephony = vonage
                        bridge.webhook_base_url = base_url

                    logger.info("vonage_call_initiated",
                        call_id=call_id,
                        call_uuid=call_uuid,
                        to=request.phone_number,
                        ws_url=ws_url,
                    )

                elif settings.twilio_account_sid:
                    twilio = TwilioTelephony(
                        account_sid=settings.twilio_account_sid,
                        auth_token=settings.twilio_auth_token,
                        phone_number=settings.twilio_phone_number,
                    )
                    call_sid = await twilio.make_outbound_call(
                        to_number=request.phone_number,
                        call_id=call_id,
                        ws_url=ws_url,
                    )
                    active_calls[call_id]["call_sid"] = call_sid
                    active_calls[call_id]["twilio_client"] = twilio.client

                    if bridge:
                        bridge.twilio_call_sid = call_sid
                        bridge.twilio_client = twilio.client
                        bridge.twilio_telephony = twilio
                        bridge.webhook_base_url = base_url

                    logger.info("twilio_call_initiated",
                        call_id=call_id,
                        call_sid=call_sid,
                        to=request.phone_number,
                        ws_url=ws_url,
                    )

            metrics_collector.record_call_start(call_id, request.pipeline.value)

            logger.info("call_initiated",
                call_id=call_id,
                pipeline=request.pipeline.value,
                phone=request.phone_number,
            )

            return CallResponse(
                call_id=call_id,
                status=CallStatus.ACTIVE,
                pipeline=request.pipeline,
                phone_number=request.phone_number,
                agent_id=request.agent.agent_id,
                estimated_cost_per_minute=estimated_cost,
                message=f"Call started with {request.pipeline.value} pipeline",
            )

        except Exception as e:
            active_calls[call_id]["status"] = CallStatus.FAILED
            logger.error("call_start_failed", call_id=call_id, error=str(e))
            raise HTTPException(status_code=500, detail=f"Failed to start call: {str(e)}")

    # ── DELETE /v1/calls/{call_id} - End a call ───────────────────────────
    @app.delete("/v1/calls/{call_id}", response_model=CallMetricsResponse, tags=["Calls"])
    async def end_call(call_id: str, api_key: str = Depends(verify_api_key)):
        """End an active call and return final metrics."""
        if call_id not in active_calls:
            raise HTTPException(status_code=404, detail="Call not found")

        call_data = active_calls[call_id]
        orchestrator = call_data["orchestrator"]

        try:
            final_metrics = await orchestrator.end_call()
            call_data["status"] = CallStatus.COMPLETED

            metrics_collector.record_call_end(call_id, final_metrics)

            # Notify WebSocket listeners
            if call_id in call_websockets:
                for ws in call_websockets[call_id]:
                    try:
                        await ws.send_json({"event": "call_ended", "metrics": final_metrics})
                    except Exception:
                        pass

            return CallMetricsResponse(**final_metrics)

        except Exception as e:
            logger.error("call_end_failed", call_id=call_id, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    # ── GET /v1/calls/{call_id} - Get call status ─────────────────────────
    @app.get("/v1/calls/{call_id}", tags=["Calls"])
    async def get_call(call_id: str, api_key: str = Depends(verify_api_key)):
        """Get current status and metrics for a call."""
        if call_id not in active_calls:
            raise HTTPException(status_code=404, detail="Call not found")

        call_data = active_calls[call_id]
        orchestrator = call_data.get("orchestrator")

        result = {
            "call_id": call_id,
            "status": call_data["status"].value,
            "pipeline_mode": call_data["pipeline_mode"],
            "duration_seconds": round(time.time() - call_data["start_time"], 2),
        }

        if orchestrator and orchestrator.metrics:
            result.update(orchestrator.metrics.to_dict())

        return result

    # ── GET /v1/health - Health check ─────────────────────────────────────
    @app.get("/v1/health", response_model=HealthResponse, tags=["System"])
    async def health_check():
        """Platform health status and provider availability."""
        # Check each provider type
        providers = {}

        # Check if we can create pipelines (validates API keys exist)
        budget_ready = bool(settings.deepgram_api_key and settings.groq_api_key)
        quality_ready = bool(settings.deepgram_api_key and settings.google_api_key and settings.cartesia_api_key)

        providers["budget_pipeline"] = "ready" if budget_ready else "not_configured"
        providers["quality_pipeline"] = "ready" if quality_ready else "not_configured"

        # Check telephony provider
        if settings.telephony_provider == "signalwire":
            telephony_ready = bool(
                settings.signalwire_project_id
                and settings.signalwire_api_token
                and settings.signalwire_space_name
                and settings.signalwire_phone_number
            )
        elif settings.telephony_provider == "vonage":
            telephony_ready = bool(
                settings.vonage_api_key
                and settings.vonage_application_id
                and settings.vonage_phone_number
            )
        elif settings.telephony_provider == "twilio":
            telephony_ready = bool(
                settings.twilio_account_sid
                and settings.twilio_auth_token
                and settings.twilio_phone_number
            )
        else:
            telephony_ready = bool(settings.telnyx_api_key)
        providers["telephony"] = "ready" if telephony_ready else "not_configured"

        overall = "healthy" if (budget_ready or quality_ready) else "degraded"

        return HealthResponse(
            status=overall,
            version="1.0.0",
            providers=providers,
            active_calls=sum(1 for c in active_calls.values() if c["status"] == CallStatus.ACTIVE),
        )

    # ── GET /v1/dashboard - Metrics dashboard ─────────────────────────────
    @app.get("/v1/dashboard", response_model=DashboardResponse, tags=["System"])
    async def dashboard(api_key: str = Depends(verify_api_key)):
        """Platform-wide metrics and cost tracking."""
        return DashboardResponse(**metrics_collector.get_dashboard())

    # ── GET /v1/monitor - System monitor status ────────────────────────────
    @app.get("/v1/monitor", tags=["System"])
    async def monitor_status(api_key: str = Depends(verify_api_key)):
        """Auto-healing monitor status, provider health, and cost tracking."""
        if health_monitor:
            return health_monitor.get_system_status()
        return {"status": "monitor_not_configured", "providers": {}}

    # ── GET /v1/pipelines - Available pipelines ───────────────────────────
    @app.get("/v1/pipelines", tags=["System"])
    async def list_pipelines():
        """List available pipeline configurations with pricing."""
        return {
            "pipelines": [
                {
                    "mode": "budget",
                    "estimated_cost_per_minute": 0.021,
                    "description": "Deepgram Nova-3 STT + Groq Llama LLM + Deepgram Aura-2 TTS",
                    "stt": "Deepgram Nova-3 ($0.0077/min, sub-300ms latency)",
                    "llm": "Groq Llama 4 Scout ($0.0002/min, ~490ms TTFT, 877 tok/sec)",
                    "tts": "Deepgram Aura-2 ($0.010/min, 90ms TTFB)",
                    "telephony": "Telnyx ($0.007/min outbound)",
                    "target_latency_ms": 800,
                },
                {
                    "mode": "quality",
                    "estimated_cost_per_minute": 0.032,
                    "description": "Deepgram Nova-3 STT + Gemini 2.5 Flash LLM + Cartesia Sonic TTS",
                    "stt": "Deepgram Nova-3 ($0.0077/min, sub-300ms latency)",
                    "llm": "Gemini 2.5 Flash ($0.0009/min, ~192ms TTFT)",
                    "tts": "Cartesia Sonic-2 ($0.010/min, 40ms TTFB, emotion control)",
                    "telephony": "Telnyx ($0.007/min outbound)",
                    "target_latency_ms": 600,
                },
            ]
        }

    # ── WebSocket /v1/ws/{call_id} - Real-time events ────────────────────
    @app.websocket("/v1/ws/{call_id}")
    async def websocket_endpoint(websocket: WebSocket, call_id: str):
        """
        Real-time WebSocket stream for call events.
        Events: transcript, response, audio, metrics, call_ended
        """
        await websocket.accept()

        if call_id not in call_websockets:
            call_websockets[call_id] = []
        call_websockets[call_id].append(websocket)

        try:
            while True:
                # Receive commands from client
                data = await websocket.receive_json()

                if data.get("type") == "audio":
                    # Client sending audio data
                    pass  # Handled by telephony integration

                elif data.get("type") == "interrupt":
                    # Client requesting barge-in
                    if call_id in active_calls:
                        orchestrator = active_calls[call_id]["orchestrator"]
                        await orchestrator.handle_interruption()
                        await websocket.send_json({"event": "interrupted"})

                elif data.get("type") == "ping":
                    await websocket.send_json({"event": "pong"})

        except WebSocketDisconnect:
            if call_id in call_websockets:
                call_websockets[call_id].remove(websocket)

    # ── POST /v1/calls/inbound - Twilio inbound webhook ──────────────────
    @app.post("/v1/calls/inbound", tags=["Telephony"])
    async def handle_inbound_call(request: Request):
        """
        Twilio webhook for incoming calls.
        Returns TwiML that connects the call to our Media Streams WebSocket.

        Self-calls (test mode): If the call is FROM our own number, return
        scripted prospect TwiML instead of creating a second AI agent.

        Note: No authentication needed here since Twilio calls this endpoint directly.
        """
        # Check if this is a self-call (test mode)
        form_data = await request.form()
        from_number = form_data.get("From", "")
        to_number = form_data.get("To", "")
        if from_number and to_number and from_number == to_number:
            logger.info("inbound_self_call_detected",
                from_=from_number, to=to_number,
                msg="Returning test prospect TwiML")
            return await test_answer_twiml()

        call_id = str(uuid.uuid4())

        # Build WebSocket URL from base_url
        import os
        base_url = settings.base_url
        if not base_url:
            fly_app = os.environ.get("FLY_APP_NAME")
            if fly_app:
                base_url = f"https://{fly_app}.fly.dev"
            else:
                base_url = f"http://{settings.host}:{settings.port}"
        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

        try:
            if settings.telephony_provider == "signalwire":
                # SignalWire inbound: return TwiML XML (same as Twilio)
                sw = SignalWireTelephony(
                    project_id=settings.signalwire_project_id,
                    api_token=settings.signalwire_api_token,
                    space_name=settings.signalwire_space_name,
                    phone_number=settings.signalwire_phone_number,
                )
                twiml = sw.generate_inbound_twiml(call_id, ws_url)
                logger.info("inbound_call_received", call_id=call_id, provider="signalwire")
                return Response(content=twiml, media_type="application/xml")

            elif settings.telephony_provider == "vonage":
                # Vonage inbound: return NCCO JSON
                vonage = VonageTelephony(
                    api_key=settings.vonage_api_key,
                    api_secret=settings.vonage_api_secret,
                    application_id=settings.vonage_application_id,
                    private_key=settings.vonage_private_key,
                    phone_number=settings.vonage_phone_number,
                )
                ncco = vonage.generate_answer_ncco(call_id, ws_url)
                logger.info("inbound_call_received", call_id=call_id, provider="vonage")
                return JSONResponse(content=ncco)

            elif settings.telephony_provider == "twilio":
                # Twilio inbound: return TwiML XML
                twilio = TwilioTelephony(
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                    phone_number=settings.twilio_phone_number,
                )
                twiml = twilio.generate_inbound_twiml(call_id, ws_url)
                logger.info("inbound_call_received", call_id=call_id, provider="twilio")
                return Response(content=twiml, media_type="application/xml")

            else:
                logger.error("inbound_call_not_configured", provider=settings.telephony_provider)
                return Response(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>',
                    media_type="application/xml",
                    status_code=403,
                )

        except Exception as e:
            logger.error("inbound_call_error", error=str(e))
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>',
                media_type="application/xml",
                status_code=500,
            )

    # ── WebSocket /v1/media-stream/{call_id} - Audio Media Streams ──────
    @app.websocket("/v1/media-stream/{call_id}")
    async def media_stream(websocket: WebSocket, call_id: str):
        """
        Media Streams WebSocket endpoint — supports both Twilio and Vonage.

        Twilio: mulaw 8kHz JSON-wrapped base64 audio
        Vonage: raw PCM 16kHz binary frames (no conversion needed)

        When a call is started via POST /v1/calls, the bridge is pre-created.
        The telephony provider dials the prospect and connects here via WebSocket.
        """
        await websocket.accept()

        logger.info("media_stream_connected", call_id=call_id)

        # Get or create call data for this media stream
        if call_id not in active_calls:
            # Inbound call — create pipeline, bridge, and agent config on-the-fly
            # Uses the SAME unified config as outbound (same voice, transfer, rules)
            from ..inbound_handler import INBOUND_CONFIG
            inbound_agent_config = AgentConfig(
                agent_id=INBOUND_CONFIG["agent_id"],
                system_prompt=INBOUND_CONFIG["system_prompt"],
                voice_id=INBOUND_CONFIG["voice_id"],
                greeting=INBOUND_CONFIG["greeting"],
                pitch_text=INBOUND_CONFIG.get("pitch_text", ""),
                temperature=INBOUND_CONFIG.get("temperature", 0.7),
                max_tokens=INBOUND_CONFIG.get("max_tokens", 40),
                transfer_config=INBOUND_CONFIG.get("transfer_config"),
            )
            inbound_orchestrator = QualityPipeline.create()
            inbound_bridge = CallBridge(
                orchestrator=inbound_orchestrator,
                agent_config=inbound_agent_config,
                call_id=call_id,
            )
            active_calls[call_id] = {
                "status": CallStatus.RINGING,
                "start_time": time.time(),
                "media_websocket": websocket,
                "orchestrator": inbound_orchestrator,
                "bridge": inbound_bridge,
                "agent_config": inbound_agent_config,
                "pipeline_mode_str": "quality",
                "direction": "inbound",
            }
            logger.info("inbound_call_pipeline_created", call_id=call_id, pipeline="quality")

        try:
            call_data = active_calls[call_id]
            call_data["status"] = CallStatus.ACTIVE

            # Get the bridge and orchestrator (pre-created by start_call endpoint or inbound setup above)
            bridge = call_data.get("bridge")
            orchestrator = call_data.get("orchestrator")

            # Connect providers if not already pre-connected at dial time.
            if orchestrator:
                pipeline_mode = call_data.get("pipeline_mode_str", "budget")
                agent_config = call_data.get("agent_config")
                providers_pre_connected = call_data.get("providers_pre_connected", False)

                # ── Inject conversation memory into system prompt ──
                lead_id = call_data.get("lead_id")
                if lead_id and agent_config:
                    try:
                        lead_data = await _load_lead_data(lead_id)
                        if lead_data:
                            agent_config.system_prompt = conversation_memory.build_memory_prompt(
                                lead_data=lead_data,
                                base_system_prompt=agent_config.system_prompt,
                            )
                            call_data["lead_data"] = lead_data
                            logger.info("memory_injected", call_id=call_id, lead_id=lead_id,
                                        attempt=lead_data.get("attempt_count", 0))
                    except Exception as e:
                        logger.warning("memory_load_failed", call_id=call_id, error=str(e))

                try:
                    t_connect = time.time()

                    if providers_pre_connected:
                        # Providers were connected at dial time — skip connection!
                        logger.info("providers_already_connected",
                            call_id=call_id,
                            greeting_cached=bridge._greeting_audio is not None if bridge else False,
                            greeting_bytes=len(bridge._greeting_audio) if bridge and bridge._greeting_audio else 0,
                        )
                    else:
                        # Fallback: Connect providers now (inbound calls or failed pre-connect)
                        logger.info("connecting_providers_on_answer", call_id=call_id)
                        greeting_already_ready = bridge and bridge._greeting_audio is not None

                        if greeting_already_ready:
                            await asyncio.gather(
                                orchestrator.tts.connect(),
                                orchestrator.stt.connect(),
                            )
                        else:
                            await orchestrator.tts.connect()
                            tts_ms = (time.time() - t_connect) * 1000
                            logger.info("tts_connected_first", call_id=call_id, elapsed_ms=round(tts_ms, 1))

                            async def _connect_stt():
                                await orchestrator.stt.connect()

                            async def _pre_synth_greeting():
                                if bridge:
                                    await bridge.pre_synthesize_greeting()

                            async def _pre_synth_pitch():
                                if bridge:
                                    await bridge.pre_synthesize_pitch()

                            async def _pre_synth_fillers():
                                if bridge:
                                    await bridge.pre_synthesize_fillers()

                            async def _pre_synth_turn1_cache():
                                if bridge:
                                    await bridge.pre_synthesize_turn1_cache()

                            async def _pre_synth_turn2_cache():
                                if bridge:
                                    await bridge.pre_synthesize_turn2_cache()

                            async def _pre_synth_semantic_cache():
                                if bridge:
                                    await bridge.pre_synthesize_semantic_cache()

                            async def _pre_synth_backchannel():
                                if bridge:
                                    await bridge.pre_synthesize_backchannel_audio()

                            async def _pre_synth_recovery():
                                if bridge:
                                    await bridge.pre_synthesize_recovery_audio()

                            await asyncio.gather(
                                _connect_stt(),
                                _pre_synth_greeting(),
                                _pre_synth_pitch(),
                                _pre_synth_fillers(),
                                _pre_synth_backchannel(),
                                _pre_synth_turn1_cache(),
                                _pre_synth_turn2_cache(),
                                _pre_synth_semantic_cache(),
                                _pre_synth_recovery(),
                            )

                        # Set orchestrator state
                        from ..pipelines.orchestrator import CallMetrics
                        orchestrator._active = True
                        orchestrator._metrics = CallMetrics(pipeline_mode=pipeline_mode)
                        orchestrator._conversation_history = []
                        orchestrator._cancel_tts.clear()

                    total_ms = (time.time() - t_connect) * 1000
                    logger.info("media_stream_provider_setup_done",
                        call_id=call_id,
                        total_ms=round(total_ms, 1),
                        pre_connected=providers_pre_connected,
                        greeting_ready=bridge._greeting_audio is not None if bridge else False,
                    )
                except Exception as e:
                    logger.error("provider_connect_failed", call_id=call_id, error=str(e),
                                 error_type=type(e).__name__)
                    bridge = None
                    call_data["bridge"] = None

            # Instantiate the appropriate telephony handler
            if settings.telephony_provider == "signalwire":
                telephony = SignalWireTelephony(
                    project_id=settings.signalwire_project_id,
                    api_token=settings.signalwire_api_token,
                    space_name=settings.signalwire_space_name,
                    phone_number=settings.signalwire_phone_number,
                )
            elif settings.telephony_provider == "vonage":
                telephony = VonageTelephony(
                    api_key=settings.vonage_api_key,
                    api_secret=settings.vonage_api_secret,
                    application_id=settings.vonage_application_id,
                    private_key=settings.vonage_private_key,
                    phone_number=settings.vonage_phone_number,
                )
            else:
                telephony = TwilioTelephony(
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                    phone_number=settings.twilio_phone_number,
                )

            if bridge:
                # Wire telephony for clear messages on barge-in (duck-typed)
                bridge.twilio_telephony = telephony
                # Start the bridge (greeting audio is pre-synthesized — plays instantly)
                await bridge.start()

                # Define callbacks that route through the bridge
                async def on_audio(pcm_data: bytes):
                    await bridge.process_audio(pcm_data)

                async def get_audio():
                    return await bridge.get_audio()
            else:
                # Fallback: no bridge (e.g., inbound call without pre-setup)
                async def on_audio(pcm_data: bytes):
                    pass

                async def get_audio():
                    return None

            # Handle the media stream (blocks until call ends)
            await telephony.handle_media_stream(websocket, call_id, on_audio, get_audio)

        except Exception as e:
            logger.error("media_stream_error", call_id=call_id, error=str(e))
        finally:
            # Cleanup
            call_data = active_calls.get(call_id, {})
            bridge = call_data.get("bridge")
            if bridge:
                try:
                    await bridge.stop()
                except Exception:
                    pass

            orchestrator = call_data.get("orchestrator")
            call_duration = time.time() - call_data.get("start_time", time.time())

            # ── Save conversation memory after call ends ──
            if orchestrator and orchestrator._conversation_history:
                lead_id = call_data.get("lead_id")
                lead_data = call_data.get("lead_data", {})
                transcript = list(orchestrator._conversation_history)

                if lead_id and len(transcript) >= 2:
                    try:
                        # Use the orchestrator's LLM for summarization
                        async def llm_generate(messages, system_prompt):
                            full_response = ""
                            async for chunk in orchestrator.llm.generate_stream(
                                messages=messages,
                                system_prompt=system_prompt,
                                temperature=0.3,
                                max_tokens=512,
                            ):
                                text = chunk.get("text", "")
                                if text:
                                    full_response += text
                                if chunk.get("is_complete"):
                                    break
                            return full_response

                        memory_result = await conversation_memory.summarize_and_save(
                            lead_data=lead_data,
                            transcript=transcript,
                            call_duration_seconds=call_duration,
                            llm_generate_fn=llm_generate,
                        )

                        # Persist to database
                        await _save_memory_to_db(
                            lead_id=lead_id,
                            call_id=call_id,
                            lead_updates=memory_result.get("lead_updates", {}),
                            call_log_updates=memory_result.get("call_log_updates", {}),
                            transcript=transcript,
                            duration=call_duration,
                            call_data=call_data,
                        )
                        logger.info("memory_persisted", call_id=call_id, lead_id=lead_id)
                    except Exception as e:
                        logger.error("memory_save_failed", call_id=call_id, error=str(e))

            # ── Tag call disposition ──
            if orchestrator:
                try:
                    transcript = list(orchestrator._conversation_history) if orchestrator._conversation_history else []
                    transfer_data = call_data.get("transfer_result", {})
                    bridge_obj = call_data.get("bridge")
                    transfer_was_initiated = getattr(bridge_obj, '_transfer_initiated', False) if bridge_obj else False
                    has_human_contact = len(transcript) >= 2

                    signals = DispositionSignals(
                        call_duration_seconds=call_duration,
                        has_human_audio=has_human_contact,
                        reached_human=has_human_contact,
                        prospect_transferred=transfer_was_initiated or transfer_data.get("transferred", False),
                        requested_callback=any(
                            "call me back" in t.get("content", "").lower() or
                            "callback" in t.get("content", "").lower()
                            for t in transcript if t.get("role") == "user"
                        ),
                        dnc_request=any(
                            "do not call" in t.get("content", "").lower() or
                            "remove me" in t.get("content", "").lower() or
                            "stop calling" in t.get("content", "").lower()
                            for t in transcript if t.get("role") == "user"
                        ),
                    )

                    result = disposition_engine.tag_realtime(signals)
                    disposition = result.disposition
                    call_data["disposition"] = disposition.value
                    logger.info("call_disposition_tagged",
                        call_id=call_id,
                        disposition=disposition.value,
                        confidence=result.confidence,
                        duration=round(call_duration, 1),
                        retry=disposition_engine.should_retry(disposition),
                    )
                except Exception as e:
                    logger.warning("disposition_tagging_failed", call_id=call_id, error=str(e))

            if orchestrator:
                try:
                    await orchestrator.end_call()
                except Exception:
                    pass
            logger.info("media_stream_disconnected", call_id=call_id)

    # ── Universal WebSocket Audio Endpoint (Brightcall / Partner Integration) ──
    # Provider-agnostic bidirectional audio over WebSocket.
    # No Twilio dependency. Partners connect directly.
    #
    # Protocol (simple JSON + binary):
    #   Client → Server:
    #     1. JSON: {"event": "start", "call_id": "...", "phone_number": "+1...",
    #              "first_name": "John", "last_name": "Smith", ...any fields...
    #              "codec": "mulaw", "sample_rate": 8000, "direction": "outbound"}
    #        All fields beyond "event" are optional. Any field sent will be available
    #        as a {{field_name}} merge variable in the AI script, greeting, and FAQ.
    #     2. Binary frames: raw mulaw 8kHz audio (160 bytes = 20ms per frame)
    #     3. JSON: {"event": "stop"} — call ended
    #     4. JSON: {"event": "dtmf", "digit": "1"} — DTMF events (optional)
    #
    #   Server → Client:
    #     1. JSON: {"event": "ready", "call_id": "...", "session_id": "..."}
    #     2. Binary frames: raw mulaw 8kHz audio (AI response)
    #     3. JSON: {"event": "transfer", "target": "+19048404634"} — transfer request
    #     4. JSON: {"event": "end", "reason": "completed"} — AI wants to end call
    #
    # Connection URL: wss://wellheard-ai-631409718089.us-east4.run.app/v1/partner/audio
    # Auth: Bearer token in query param or Sec-WebSocket-Protocol header

    @app.websocket("/v1/partner/audio")
    async def partner_audio_stream(websocket: WebSocket):
        """
        Universal WebSocket audio endpoint for partner telephony integration.
        Brightcall (or any partner) connects here with their own telephony.
        Bidirectional raw mulaw 8kHz audio — no Twilio dependency.
        """
        import audioop
        import struct
        import json
        import base64

        # Accept with optional subprotocol
        await websocket.accept()

        call_id = f"partner-{uuid.uuid4().hex[:12]}"
        session_start = time.time()
        bridge = None
        orchestrator = None

        # Audio conversion state (persistent across frames for clean audio)
        ratecv_state_down = None  # For 16k→8k downsampling
        lpf_hist = [0, 0]  # Low-pass filter history

        def mulaw_8k_to_pcm_16k(mulaw_data: bytes) -> bytes:
            """Convert incoming mulaw 8kHz to PCM 16kHz for AI pipeline."""
            pcm_8k = audioop.ulaw2lin(mulaw_data, 2)
            pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
            return pcm_16k

        def pcm_16k_to_mulaw_8k(pcm_data: bytes) -> bytes:
            """Convert AI pipeline PCM 16kHz to mulaw 8kHz for partner."""
            nonlocal ratecv_state_down, lpf_hist

            n_samples = len(pcm_data) // 2
            if n_samples > 1:
                samples = struct.unpack(f'<{n_samples}h', pcm_data[:n_samples * 2])
                # FIR low-pass filter to prevent aliasing
                filtered = []
                h1, h2 = lpf_hist
                for s in samples:
                    out = (h1 + (h2 << 1) + s) >> 2
                    out = max(-32768, min(32767, out))
                    filtered.append(out)
                    h1 = h2
                    h2 = s
                lpf_hist = [h1, h2]
                pcm_data = struct.pack(f'<{n_samples}h', *filtered)

            pcm_8k, ratecv_state_down = audioop.ratecv(
                pcm_data, 2, 1, 16000, 8000, ratecv_state_down)
            return audioop.lin2ulaw(pcm_8k, 2)

        logger.info("partner_ws_connected", call_id=call_id)

        try:
            # ── Phase 1: Wait for "start" event with call metadata ──
            start_data = None
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
                start_data = json.loads(raw)
            except (asyncio.TimeoutError, json.JSONDecodeError) as e:
                logger.warning("partner_ws_no_start_event", call_id=call_id, error=str(e))
                await websocket.send_json({"event": "error", "message": "Expected JSON start event within 10s"})
                await websocket.close(1008)
                return

            if start_data.get("event") != "start":
                await websocket.send_json({"event": "error", "message": "First message must be {\"event\": \"start\", ...}"})
                await websocket.close(1008)
                return

            # Extract standard lead fields
            partner_call_id = start_data.get("call_id", call_id)
            codec = start_data.get("codec", "mulaw")
            sample_rate = start_data.get("sample_rate", 8000)
            direction = start_data.get("direction", "inbound")

            # Standard lead fields (all optional)
            lead_fields = {
                "phone_number": start_data.get("phone_number", start_data.get("caller", "")),
                "first_name": start_data.get("first_name", ""),
                "last_name": start_data.get("last_name", ""),
                "full_name": start_data.get("full_name", ""),
                "email": start_data.get("email", ""),
                "birth_date": start_data.get("birth_date", ""),
                "state": start_data.get("state", ""),
                "city": start_data.get("city", ""),
                "zip_code": start_data.get("zip_code", ""),
            }

            # Capture ALL extra fields sent by partner (any key-value pair)
            # These become available as {{field_name}} merge variables
            reserved_keys = {"event", "codec", "sample_rate", "direction", "call_id"}
            for key, value in start_data.items():
                if key not in reserved_keys and key not in lead_fields:
                    lead_fields[key] = str(value)

            # Build full_name from parts if not provided
            if not lead_fields["full_name"] and (lead_fields["first_name"] or lead_fields["last_name"]):
                lead_fields["full_name"] = f"{lead_fields['first_name']} {lead_fields['last_name']}".strip()

            # Also support legacy "prospect_name" field
            if start_data.get("prospect_name") and not lead_fields["first_name"]:
                lead_fields["first_name"] = start_data["prospect_name"]
                if not lead_fields["full_name"]:
                    lead_fields["full_name"] = start_data["prospect_name"]

            # ── Fetch remote lead data from partner API if configured ──
            # If partner provided a data_url, fetch lead data from their API
            data_url = start_data.get("data_url")  # e.g. "https://api.brightcall.com/calls/{call_id}/data"
            if data_url:
                try:
                    import httpx
                    data_api_key = start_data.get("data_api_key")
                    headers = {"Authorization": f"Bearer {data_api_key}"} if data_api_key else {}
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        fetch_url = data_url.replace("{call_id}", partner_call_id)
                        resp = await client.get(fetch_url, headers=headers)
                        if resp.status_code == 200:
                            remote_data = resp.json()
                            # Merge remote data into lead_fields
                            reserved_keys = {"event", "codec", "sample_rate", "direction", "call_id"}
                            for key, value in remote_data.items():
                                if value and key not in reserved_keys:
                                    lead_fields[key] = str(value)
                            logger.info("partner_data_fetched", call_id=call_id, fields_count=len(remote_data))
                        else:
                            logger.warning("partner_data_fetch_status", call_id=call_id, status=resp.status_code)
                except Exception as e:
                    logger.warning("partner_data_fetch_failed", call_id=call_id, error=str(e))

            # Use partner's call_id if provided
            if partner_call_id != call_id:
                call_id = f"partner-{partner_call_id[:20]}"

            # Extract caller/callee for active_calls registration
            caller_number = lead_fields.get("phone_number", "")
            callee_number = start_data.get("callee", "")

            logger.info("partner_call_start",
                call_id=call_id, direction=direction,
                phone=lead_fields["phone_number"],
                name=lead_fields["full_name"],
                fields_count=len(lead_fields))

            # ── Phase 2: Create AI pipeline + bridge ──
            from ..inbound_handler import INBOUND_CONFIG, OUTBOUND_CONFIG
            import re as _re

            config = OUTBOUND_CONFIG if direction == "outbound" else INBOUND_CONFIG

            agent_config = AgentConfig(
                agent_id=config["agent_id"],
                system_prompt=config["system_prompt"],
                voice_id=config["voice_id"],
                greeting=config["greeting"],
                pitch_text=config.get("pitch_text", ""),
                temperature=config.get("temperature", 0.7),
                max_tokens=config.get("max_tokens", 40),
                transfer_config=config.get("transfer_config"),
            )

            # ── Merge field injection ──
            # Replace {{field_name}} placeholders in system prompt, greeting,
            # and pitch text with actual lead data from the partner.
            # Works with ANY field — standard or custom.
            def apply_merge_fields(text: str, fields: dict) -> str:
                """Replace {{field_name}} and {field_name} with lead values."""
                if not text:
                    return text
                for key, value in fields.items():
                    if value:  # Only replace if value is non-empty
                        # Match both {{key}} and {key} patterns (case-insensitive)
                        text = _re.sub(
                            r'\{\{' + _re.escape(key) + r'\}\}',
                            str(value), text, flags=_re.IGNORECASE
                        )
                        text = _re.sub(
                            r'\{' + _re.escape(key) + r'\}',
                            str(value), text, flags=_re.IGNORECASE
                        )
                        # Also match common variations: {{First Name}}, {{first-name}}
                        readable_key = key.replace("_", " ")
                        text = _re.sub(
                            r'\{\{' + _re.escape(readable_key) + r'\}\}',
                            str(value), text, flags=_re.IGNORECASE
                        )
                        text = _re.sub(
                            r'\{' + _re.escape(readable_key) + r'\}',
                            str(value), text, flags=_re.IGNORECASE
                        )
                return text

            agent_config.system_prompt = apply_merge_fields(agent_config.system_prompt, lead_fields)
            agent_config.greeting = apply_merge_fields(agent_config.greeting, lead_fields)
            agent_config.pitch_text = apply_merge_fields(agent_config.pitch_text, lead_fields)

            # Inject lead context block into system prompt so AI knows about the prospect
            lead_context_lines = []
            for key, value in lead_fields.items():
                if value:
                    label = key.replace("_", " ").title()
                    lead_context_lines.append(f"  {label}: {value}")
            if lead_context_lines:
                lead_block = "\n[LEAD DATA — use naturally in conversation, do NOT read out loud like a list]\n"
                lead_block += "\n".join(lead_context_lines) + "\n"
                agent_config.system_prompt = agent_config.system_prompt + lead_block

            orchestrator = QualityPipeline.create()
            bridge = CallBridge(
                orchestrator=orchestrator,
                agent_config=agent_config,
                call_id=call_id,
            )

            # Register in active calls
            active_calls[call_id] = {
                "status": CallStatus.ACTIVE,
                "start_time": session_start,
                "media_websocket": websocket,
                "orchestrator": orchestrator,
                "bridge": bridge,
                "agent_config": agent_config,
                "pipeline_mode_str": "quality",
                "direction": direction,
                "partner": "brightcall",
                "caller": caller_number,
                "callee": callee_number,
            }

            # ── Phase 3: Connect providers and pre-synthesize ──
            t_connect = time.time()

            # Connect TTS first (needed for greeting synthesis)
            await orchestrator.tts.connect()

            async def _connect_stt():
                await orchestrator.stt.connect()

            async def _pre_synth_greeting():
                await bridge.pre_synthesize_greeting()

            async def _pre_synth_pitch():
                await bridge.pre_synthesize_pitch()

            async def _pre_synth_turn1():
                if hasattr(bridge, 'pre_synthesize_turn1_cache'):
                    await bridge.pre_synthesize_turn1_cache()

            async def _pre_synth_turn2():
                if hasattr(bridge, 'pre_synthesize_turn2_cache'):
                    await bridge.pre_synthesize_turn2_cache()

            async def _pre_synth_semantic():
                if hasattr(bridge, 'pre_synthesize_semantic_cache'):
                    await bridge.pre_synthesize_semantic_cache()

            # Parallel pre-synthesis with timeout — ready event MUST fire
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _connect_stt(),
                        _pre_synth_greeting(),
                        _pre_synth_pitch(),
                        _pre_synth_turn1(),
                        _pre_synth_turn2(),
                        _pre_synth_semantic(),
                        return_exceptions=True,
                    ),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                logger.warning("partner_presynth_timeout", call_id=call_id)
            except Exception as e:
                logger.warning("partner_presynth_error", call_id=call_id, error=str(e))

            from ..pipelines.orchestrator import CallMetrics
            orchestrator._active = True
            orchestrator._metrics = CallMetrics(pipeline_mode="quality")
            orchestrator._conversation_history = []
            orchestrator._cancel_tts.clear()

            setup_ms = (time.time() - t_connect) * 1000
            logger.info("partner_pipeline_ready",
                call_id=call_id, setup_ms=round(setup_ms, 1))

            # Start the bridge
            await bridge.start()

            # Send ready confirmation to partner
            await websocket.send_json({
                "event": "ready",
                "call_id": call_id,
                "session_id": call_id,
                "setup_ms": round(setup_ms, 1),
            })

            # ── Phase 4: Bidirectional audio streaming ──
            audio_ended = asyncio.Event()

            # Voicemail detection state
            voicemail_detection_state = {
                "samples_received": 0,
                "large_continuous_segments": 0,
                "voicemail_detected": False,
                "detection_complete": False,
            }

            async def send_audio_to_partner():
                """Get audio from AI pipeline and send to partner as raw binary."""
                FRAME_INTERVAL = 0.02  # 20ms per frame
                next_send = time.time()

                try:
                    while not audio_ended.is_set():
                        pcm_data = await bridge.get_audio()
                        if pcm_data is None:
                            break
                        if not pcm_data or len(pcm_data) < 2:
                            await asyncio.sleep(0.02)
                            next_send = time.time() + FRAME_INTERVAL
                            continue

                        # Validate PCM frame
                        if len(pcm_data) % 2 != 0:
                            pcm_data = pcm_data[:len(pcm_data) - 1]
                            if len(pcm_data) < 2:
                                continue

                        # Convert to mulaw 8kHz
                        try:
                            mulaw_data = pcm_16k_to_mulaw_8k(pcm_data)
                        except Exception:
                            continue

                        # Pace at 20ms intervals
                        now = time.time()
                        if now < next_send:
                            await asyncio.sleep(next_send - now)
                        next_send = max(time.time(), next_send) + FRAME_INTERVAL

                        # Send raw binary frame (no JSON wrapper)
                        await websocket.send_bytes(mulaw_data)

                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.error("partner_send_error", call_id=call_id, error=str(e))
                finally:
                    audio_ended.set()

            async def receive_audio_from_partner():
                """Receive audio from partner and feed to AI pipeline."""
                import struct
                try:
                    while not audio_ended.is_set():
                        message = await websocket.receive()

                        if message.get("type") == "websocket.disconnect":
                            break

                        # Binary frame = raw audio
                        if "bytes" in message and message["bytes"]:
                            raw_audio = message["bytes"]
                            # Convert mulaw 8kHz to PCM 16kHz for pipeline
                            try:
                                pcm_16k = mulaw_8k_to_pcm_16k(raw_audio)

                                # Simple voicemail detection heuristic:
                                # If we receive > 5 seconds of continuous audio without pauses,
                                # it's likely an answering machine/voicemail
                                if not voicemail_detection_state["detection_complete"]:
                                    voicemail_detection_state["samples_received"] += len(pcm_16k) // 2
                                    # 16kHz = 16000 samples/sec
                                    if voicemail_detection_state["samples_received"] > 80000:  # ~5 seconds
                                        voicemail_detection_state["voicemail_detected"] = True
                                        voicemail_detection_state["detection_complete"] = True
                                        if call_id in active_calls:
                                            active_calls[call_id]["voicemail_detected"] = True
                                        await websocket.send_json({
                                            "event": "voicemail_detected",
                                            "call_id": partner_call_id,
                                        })
                                        logger.info("partner_voicemail_detected",
                                            call_id=call_id, samples=voicemail_detection_state["samples_received"])

                                await bridge.process_audio(pcm_16k)
                            except Exception as e:
                                logger.warning("partner_audio_convert_error",
                                    call_id=call_id, error=str(e))

                        # Text frame = JSON control message
                        elif "text" in message and message["text"]:
                            try:
                                ctrl = json.loads(message["text"])
                                event = ctrl.get("event")

                                if event == "stop":
                                    logger.info("partner_call_stopped",
                                        call_id=call_id, reason=ctrl.get("reason", "unknown"))
                                    break

                                elif event == "dtmf":
                                    digit = ctrl.get("digit", "")
                                    logger.info("partner_dtmf", call_id=call_id, digit=digit)
                                    # Could handle DTMF for transfer acceptance etc.

                                elif event == "metadata":
                                    # Partner can send updated metadata mid-call
                                    logger.info("partner_metadata_update",
                                        call_id=call_id, data=ctrl)

                            except json.JSONDecodeError:
                                pass

                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.error("partner_receive_error", call_id=call_id, error=str(e))
                finally:
                    audio_ended.set()

            # Run send + receive in parallel
            send_task = asyncio.create_task(send_audio_to_partner())
            receive_task = asyncio.create_task(receive_audio_from_partner())

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

        except WebSocketDisconnect:
            logger.info("partner_ws_disconnected", call_id=call_id)
        except Exception as e:
            logger.error("partner_ws_error", call_id=call_id, error=str(e))
        finally:
            # ── Cleanup (same as Twilio path) ──
            call_data = active_calls.get(call_id, {})
            call_duration = time.time() - session_start

            if bridge:
                try:
                    await bridge.stop()
                except Exception:
                    pass

            # Save conversation memory
            if orchestrator and orchestrator._conversation_history:
                lead_id = call_data.get("lead_id")
                lead_data = call_data.get("lead_data", {})
                transcript = list(orchestrator._conversation_history)
                if lead_id and len(transcript) >= 2:
                    try:
                        async def llm_generate(messages, system_prompt):
                            full_response = ""
                            async for chunk in orchestrator.llm.generate_stream(
                                messages=messages, system_prompt=system_prompt,
                                temperature=0.3, max_tokens=512,
                            ):
                                text = chunk.get("text", "")
                                if text:
                                    full_response += text
                                if chunk.get("is_complete"):
                                    break
                            return full_response
                        await conversation_memory.summarize_and_save(
                            lead_data=lead_data, transcript=transcript,
                            call_duration_seconds=call_duration,
                            llm_generate_fn=llm_generate,
                        )
                    except Exception as e:
                        logger.error("partner_memory_save_failed", call_id=call_id, error=str(e))

            # Tag disposition
            if orchestrator:
                try:
                    transcript = list(orchestrator._conversation_history) if orchestrator._conversation_history else []
                    bridge_obj = call_data.get("bridge")
                    transfer_was_initiated = getattr(bridge_obj, '_transfer_initiated', False) if bridge_obj else False

                    signals = DispositionSignals(
                        call_duration_seconds=call_duration,
                        has_human_audio=len(transcript) >= 2,
                        reached_human=len(transcript) >= 2,
                        prospect_transferred=transfer_was_initiated,
                        requested_callback=any(
                            "call me back" in t.get("content", "").lower()
                            for t in transcript if t.get("role") == "user"
                        ),
                        dnc_request=any(
                            "stop calling" in t.get("content", "").lower()
                            for t in transcript if t.get("role") == "user"
                        ),
                    )
                    result = disposition_engine.tag_realtime(signals)
                    call_data["disposition"] = result.disposition.value
                    logger.info("partner_call_disposition",
                        call_id=call_id, disposition=result.disposition.value,
                        duration=round(call_duration, 1))
                except Exception:
                    pass

                try:
                    await orchestrator.end_call()
                except Exception:
                    pass

            # ── POST results to partner webhook if configured ──
            webhook_url = start_data.get("webhook_url") if start_data else None
            if webhook_url:
                try:
                    import httpx
                    transcript = []
                    if orchestrator and orchestrator._conversation_history:
                        transcript = [
                            {"role": m["role"], "content": m["content"]}
                            for m in orchestrator._conversation_history
                        ]

                    webhook_data = {
                        "call_id": partner_call_id,
                        "wellheard_call_id": call_id,
                        "duration_seconds": round(call_duration, 1),
                        "disposition": call_data.get("disposition", "completed"),
                        "transcript": transcript,
                        "turns": len(transcript) // 2 if transcript else 0,
                        "transfer_initiated": getattr(bridge, '_transfer_initiated', False) if bridge else False,
                        "voicemail_detected": call_data.get("voicemail_detected", False),
                    }
                    data_api_key = start_data.get("data_api_key") if start_data else None
                    headers = {"Authorization": f"Bearer {data_api_key}"} if data_api_key else {}
                    headers["Content-Type"] = "application/json"

                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(webhook_url, json=webhook_data, headers=headers)
                        logger.info("partner_webhook_sent",
                            call_id=call_id, status=resp.status_code, url=webhook_url)
                except Exception as e:
                    logger.warning("partner_webhook_failed",
                        call_id=call_id, error=str(e), url=webhook_url)

            active_calls.pop(call_id, None)
            logger.info("partner_call_ended",
                call_id=call_id, duration=round(call_duration, 1))

    # ── Transfer Webhook Endpoints ─────────────────────────────────────────
    # Webhooks are handled by transfer_endpoints.py router (included above).
    # The router's registry is populated when call_bridge registers its manager.

    @app.get("/v1/transfer/{call_id}/status", tags=["Transfer"])
    async def get_transfer_status(call_id: str, api_key: str = Depends(verify_api_key)):
        """Get current transfer status and metrics for a call."""
        if call_id not in active_calls:
            raise HTTPException(status_code=404, detail="Call not found")

        call_data = active_calls[call_id]
        transfer_mgr = call_data.get("transfer_manager")

        if not transfer_mgr:
            return {"status": "no_transfer", "call_id": call_id}

        return {
            "call_id": call_id,
            **transfer_mgr.get_transfer_metrics(),
        }

    # ── Agent Configuration UI ──────────────────────────────────────────

    @app.get("/v1/agent/config", tags=["Configuration"])
    async def get_agent_config(api_key: str = Depends(verify_api_key)):
        """Get current agent configuration (system prompt, pitch, shared rules, FAQ)."""
        from ..inbound_handler import (
            OUTBOUND_SYSTEM_PROMPT, OUTBOUND_PITCH_TEXT, SHARED_RULES,
            VOICE_ID, VOICE_NAME, MODEL, SPEED, EMOTION, TEMPERATURE, MAX_TOKENS,
            TRANSFER_AGENT_NAME,
        )
        return {
            "system_prompt": OUTBOUND_SYSTEM_PROMPT,
            "pitch_text": OUTBOUND_PITCH_TEXT,
            "shared_rules": SHARED_RULES,
            "voice_id": VOICE_ID,
            "voice_name": VOICE_NAME,
            "model": MODEL,
            "speed": SPEED,
            "emotion": EMOTION,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "transfer_agent_name": TRANSFER_AGENT_NAME,
        }

    @app.post("/v1/agent/config", tags=["Configuration"])
    async def update_agent_config(
        request: Request,
        api_key: str = Depends(verify_api_key),
    ):
        """Update agent configuration. Only provided fields are updated.
        Changes take effect on the NEXT call (not in-progress calls)."""
        import src.inbound_handler as handler

        body = await request.json()
        updated = []

        if "system_prompt" in body:
            handler.OUTBOUND_SYSTEM_PROMPT = body["system_prompt"]
            handler.INBOUND_SYSTEM_PROMPT = body["system_prompt"]
            updated.append("system_prompt")
        if "pitch_text" in body:
            handler.OUTBOUND_PITCH_TEXT = body["pitch_text"]
            # Update outbound config dict too
            handler.OUTBOUND_CONFIG["pitch_text"] = body["pitch_text"]
            updated.append("pitch_text")
        if "shared_rules" in body:
            handler.SHARED_RULES = body["shared_rules"]
            updated.append("shared_rules")
        if "temperature" in body:
            handler.TEMPERATURE = float(body["temperature"])
            updated.append("temperature")
        if "max_tokens" in body:
            handler.MAX_TOKENS = int(body["max_tokens"])
            updated.append("max_tokens")

        return {"updated": updated, "message": f"Updated {len(updated)} fields. Changes apply to next call."}

    @app.get("/v1/agent/dashboard", tags=["Configuration"], response_class=HTMLResponse)
    async def agent_dashboard():
        """Serve the agent configuration dashboard."""
        from starlette.responses import HTMLResponse
        dashboard_path = Path(__file__).parent.parent.parent / "static" / "dashboard.html"
        if dashboard_path.exists():
            return HTMLResponse(dashboard_path.read_text())
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)

    @app.get("/", tags=["Website"], response_class=HTMLResponse)
    async def homepage():
        """Serve the WellHeard AI homepage."""
        from starlette.responses import HTMLResponse
        index_path = Path(__file__).parent.parent.parent / "static" / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text())
        return HTMLResponse("<h1>WellHeard AI</h1>", status_code=200)

    @app.get("/get-started", tags=["Website"], response_class=HTMLResponse)
    async def get_started():
        """Serve the sales funnel / get started page."""
        from starlette.responses import HTMLResponse
        funnel_path = Path(__file__).parent.parent.parent / "static" / "get-started.html"
        if funnel_path.exists():
            return HTMLResponse(funnel_path.read_text())
        return HTMLResponse("<h1>Get Started</h1>", status_code=200)

    @app.get("/docs", tags=["Documentation"], response_class=HTMLResponse)
    @app.get("/v1/docs", tags=["Documentation"], response_class=HTMLResponse)
    async def api_docs():
        """Serve the API documentation page."""
        from starlette.responses import HTMLResponse
        docs_path = Path(__file__).parent.parent.parent / "static" / "api-docs.html"
        if docs_path.exists():
            return HTMLResponse(docs_path.read_text())
        return HTMLResponse("<h1>Documentation not found</h1>", status_code=404)

    @app.get("/v1/static/{filename}", tags=["Configuration"])
    async def serve_static(filename: str):
        """Serve static files (SVG logos, etc.)."""
        import mimetypes
        static_dir = Path(__file__).parent.parent.parent / "static"
        file_path = static_dir / filename
        # Security: prevent path traversal
        if ".." in filename or "/" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return Response(content=file_path.read_bytes(), media_type=content_type)

    @app.get("/v1/transfer/config", tags=["Transfer"])
    async def get_transfer_config(api_key: str = Depends(verify_api_key)):
        """Get current transfer configuration (agent DIDs, timeouts, etc.)."""
        return {
            "primary_agent_did": settings.transfer_agent_did,
            "backup_agent_did": settings.transfer_agent_did_backup or None,
            "ring_timeout_seconds": settings.transfer_ring_timeout,
            "max_hold_time_seconds": settings.transfer_max_hold_time,
            "max_retries": settings.transfer_max_retries,
            "verify_duration_seconds": settings.transfer_verify_duration,
            "record_calls": settings.transfer_record_calls,
            "callback_enabled": settings.transfer_callback_enabled,
            "whisper_enabled": settings.transfer_whisper_enabled,
        }

    # ── GET /v1/test-answer — TwiML for test callee side ────────────────
    @app.post("/v1/test-answer", tags=["Testing"])
    @app.get("/v1/test-answer", tags=["Testing"])
    async def test_answer_twiml():
        """
        Returns TwiML for the callee side of a test call.
        Simulates a real prospect responding to the Becky final expense pitch.

        Timeline:
          0-3s:  Pause (AI greeting plays: "Hi, can you hear me ok?")
          3s:    Prospect says "Hello? Yeah I can hear you."
          3-18s: Pause (AI pitch plays ~15s: "This is Becky...")
          18s:   Prospect responds to "Does that ring a bell?"
          18-25s: Pause (AI asks qualification question)
          25s:   Prospect answers bank account question
          25-32s: Pause (AI triggers transfer)
          32s:   Prospect accepts transfer
          32-60s: Wait for transfer/hold
        """
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            '  <Pause length="3"/>'
            '  <Say voice="Polly.Matthew">Hello? Yeah I can hear you.</Say>'
            '  <Pause length="16"/>'
            '  <Say voice="Polly.Matthew">Oh yeah, I think I remember filling that out.</Say>'
            '  <Pause length="6"/>'
            '  <Say voice="Polly.Matthew">Sure, I would like to see what you have.</Say>'
            '  <Pause length="6"/>'
            '  <Say voice="Polly.Matthew">Yeah I have a checking account.</Say>'
            '  <Pause length="6"/>'
            '  <Say voice="Polly.Matthew">Sounds good, go ahead and connect me.</Say>'
            '  <Pause length="30"/>'
            '  <Say voice="Polly.Matthew">OK, I will wait. Thank you.</Say>'
            '  <Pause length="30"/>'
            '</Response>'
        )
        return Response(content=twiml, media_type="application/xml")

    # ── POST /v1/test-call — Automated self-test ──────────────────────────
    @app.post("/v1/test-call", tags=["Testing"])
    async def trigger_test_call(api_key: str = Depends(verify_api_key)):
        """
        Automated loopback test call. Uses Twilio to make a call where:
        - Our side: AI agent connected via media stream
        - Callee side: scripted TwiML that speaks test phrases

        The AI agent hears the scripted speech, processes it through
        STT → LLM → TTS, and responds. Check logs or GET /v1/test-call/{call_id}
        for results.
        """
        if not settings.twilio_account_sid:
            raise HTTPException(status_code=400, detail="Twilio not configured")

        call_id = f"test-{uuid.uuid4().hex[:8]}"

        # Use the ACTUAL Becky agent config — test with real production settings
        from ..inbound_handler import (
            OUTBOUND_PITCH_TEXT, OUTBOUND_SYSTEM_PROMPT,
            VOICE_ID, TEMPERATURE, MAX_TOKENS, TRANSFER_CONFIG,
        )

        agent_config = AgentConfig(
            agent_id="final-expense-becky",
            system_prompt=OUTBOUND_SYSTEM_PROMPT,
            voice_id=VOICE_ID,
            greeting="Hi, can you hear me ok?",
            pitch_text=OUTBOUND_PITCH_TEXT,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            transfer_config=TRANSFER_CONFIG,
        )

        # Create pipeline — identical to normal outbound call
        orchestrator = QualityPipeline.create()

        bridge = CallBridge(
            orchestrator=orchestrator,
            agent_config=agent_config,
            call_id=call_id,
        )

        # Pre-connect providers and pre-synthesize audio (same as outbound)
        try:
            await orchestrator.tts.connect()
            await bridge.pre_synthesize_greeting()
            await asyncio.gather(
                bridge.pre_synthesize_pitch(),
                bridge.pre_synthesize_fillers(),
                bridge.pre_synthesize_backchannel_audio(),
                bridge.pre_synthesize_turn1_cache(),
                bridge.pre_synthesize_turn2_cache(),
                bridge.pre_synthesize_semantic_cache(),
                bridge.pre_synthesize_recovery_audio(),
            )
            logger.info("test_call_pre_synthesized",
                call_id=call_id,
                greeting_bytes=len(bridge._greeting_audio or b''),
                pitch_bytes=len(bridge._pitch_audio or b''),
                filler_count=len(bridge._filler_audio),
                turn1_cache_count=len(bridge._turn1_cache))
        except Exception as e:
            logger.warning("test_call_pre_synth_failed",
                call_id=call_id, error=str(e))

        active_calls[call_id] = {
            "orchestrator": orchestrator,
            "bridge": bridge,
            "agent_config": agent_config,
            "status": CallStatus.ACTIVE,
            "start_time": time.time(),
            "pipeline_mode_str": "quality",
            "direction": "test",
            "test_mode": True,
            "providers_pre_connected": True,
        }

        # Determine public base URL
        base_url = settings.base_url
        if not base_url:
            import os
            fly_app = os.environ.get("FLY_APP_NAME")
            if fly_app:
                base_url = f"https://{fly_app}.fly.dev"
            else:
                base_url = f"http://{settings.host}:{settings.port}"

        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

        # Our side: AI agent connects to media stream
        our_twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Connect><Stream url="{ws_url}/v1/media-stream/{call_id}"/></Connect></Response>'
        )

        try:
            from twilio.rest import Client as TwilioClient
            twilio_client = TwilioClient(
                settings.twilio_account_sid, settings.twilio_auth_token
            )

            # Make the call:
            # - Caller side (our AI): connects to our media stream WebSocket
            # - Callee side (test bot): auto-answers with scripted speech via URL callback
            call = twilio_client.calls.create(
                to=settings.twilio_phone_number,  # Call our own number
                from_=settings.twilio_phone_number,
                twiml=our_twiml,
                # When our number answers, Twilio fetches this URL for callee TwiML
                # Note: This only works if the Twilio number's voice webhook is set
                # to our /v1/calls/inbound endpoint. Alternatively, the inbound handler
                # creates a second AI agent, and two AI agents talk to each other.
            )

            active_calls[call_id]["call_sid"] = call.sid

            logger.info("test_call_initiated",
                call_id=call_id,
                call_sid=call.sid,
                ws_url=ws_url,
            )

            return {
                "call_id": call_id,
                "call_sid": call.sid,
                "status": "initiated",
                "message": (
                    "Loopback test call started. Our AI agent will talk to "
                    "the inbound handler. Check GET /v1/test-call/" + call_id + " for results, "
                    "or check server logs for conversation flow."
                ),
            }

        except Exception as e:
            logger.error("test_call_failed", call_id=call_id, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/v1/test-call/{call_id}", tags=["Testing"])
    async def get_test_results(call_id: str, api_key: str = Depends(verify_api_key)):
        """Get test call results — conversation transcript and metrics."""
        if call_id not in active_calls:
            raise HTTPException(status_code=404, detail="Test call not found")

        call_data = active_calls[call_id]
        orchestrator = call_data.get("orchestrator")

        result = {
            "call_id": call_id,
            "status": call_data.get("status", "unknown"),
            "duration_seconds": round(time.time() - call_data.get("start_time", time.time()), 1),
            "direction": call_data.get("direction", "unknown"),
        }

        if orchestrator and orchestrator._conversation_history:
            result["conversation"] = orchestrator._conversation_history
            result["turns"] = len([m for m in orchestrator._conversation_history if m.get("role") == "user"])

        if orchestrator and orchestrator._metrics:
            result["metrics"] = orchestrator._metrics.to_dict()

        return result

    # ── Call Logs (file-based, survives Fly.io log buffer) ──────────────
    @app.get("/v1/logs", tags=["System"])
    async def get_logs(
        call_id: str = None,
        lines: int = 500,
        api_key: str = Depends(verify_api_key),
    ):
        """Get recent call logs from file. Optionally filter by call_id."""
        from ..call_logger import get_recent_logs, get_call_ids
        if call_id:
            logs = get_recent_logs(call_id=call_id, lines=lines)
            return {"call_id": call_id, "entries": len(logs), "logs": logs}
        else:
            calls = get_call_ids()
            return {"calls": calls, "total_calls": len(calls)}

    @app.get("/v1/logs/{call_id}", tags=["System"])
    async def get_call_logs(
        call_id: str,
        lines: int = 1000,
        api_key: str = Depends(verify_api_key),
    ):
        """Get all logs for a specific call."""
        from ..call_logger import get_recent_logs
        logs = get_recent_logs(call_id=call_id, lines=lines)
        return {"call_id": call_id, "entries": len(logs), "logs": logs}

    # ── Call Quality Grading ─────────────────────────────────────────────
    @app.get("/v1/grade/{call_id}", tags=["Quality"])
    async def grade_call_endpoint(
        call_id: str,
        api_key: str = Depends(verify_api_key),
    ):
        """
        Grade a call's quality across all dimensions.
        Returns detailed scores, findings, improvements, and competitor comparison.
        """
        from ..call_logger import get_recent_logs
        from ..call_grader import grade_call, format_report
        import dataclasses

        logs = get_recent_logs(call_id=call_id, lines=10000)
        if not logs:
            return {"error": "Call not found or no logs available", "call_id": call_id}

        report = grade_call(logs)

        # Convert to dict for JSON response
        report_dict = {
            "call_id": report.call_id,
            "graded_at": report.graded_at,
            "overall_score": report.overall_score,
            "overall_grade": report.overall_grade,
            "categories": [dataclasses.asdict(c) for c in report.categories],
            "competitor_comparison": report.competitor_comparison,
            "summary": report.summary,
            "top_issues": report.top_issues,
            "call_metadata": report.call_metadata,
            "formatted_report": format_report(report),
        }
        return report_dict

    @app.get("/v1/grade-report/{call_id}", tags=["Quality"])
    async def grade_report_endpoint(
        call_id: str,
        api_key: str = Depends(verify_api_key),
    ):
        """
        Get a detailed HTML report for a call's quality grade.

        Returns an HTML report with:
        - Overall score and competition ranking
        - Category breakdowns with visual progress bars
        - Detailed findings and improvement recommendations
        - Competitor benchmark comparison
        - Top priority issues

        Perfect for stakeholder presentations or dashboard embedding.
        """
        from ..call_logger import get_recent_logs
        from ..call_grader import grade_call, format_html_report

        logs = get_recent_logs(call_id=call_id, lines=10000)
        if not logs:
            return {
                "error": "Call not found or no logs available",
                "call_id": call_id,
            }

        report = grade_call(logs)
        html_report = format_html_report(report)

        from starlette.responses import HTMLResponse
        return HTMLResponse(content=html_report)

    # ── Test Prospect & Agent System ─────────────────────────────────────
    # Shared state: next forced scenario for test prospect (set by test-call-v2)
    _forced_scenario: dict = {"value": None}  # Mutable container
    @app.post("/v1/test-prospect-answer", tags=["Testing"])
    @app.get("/v1/test-prospect-answer", tags=["Testing"])
    async def test_prospect_answer(request: Request):
        """
        Twilio webhook for test prospect number (+13185522502).

        When Becky (the AI agent) calls this number, Twilio hits this endpoint
        and expects TwiML that connects to our media stream WebSocket.

        The media stream handler runs a TestProspectBridge with a randomly
        selected scenario, making the prospect behave realistically.
        """
        # Extract call SID and other context from Twilio
        try:
            body = await request.form()
            call_sid = body.get("CallSid", f"prospect-{uuid.uuid4().hex[:8]}")
            call_id = f"prospect-{call_sid[:12]}"
        except Exception:
            call_id = f"prospect-{uuid.uuid4().hex[:8]}"

        # Determine public base URL
        base_url = settings.base_url
        if not base_url:
            import os
            fly_app = os.environ.get("FLY_APP_NAME")
            if fly_app:
                base_url = f"https://{fly_app}.fly.dev"
            else:
                base_url = f"http://{settings.host}:{settings.port}"

        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

        # Generate TwiML that connects to our media stream
        from twilio.twiml.voice_response import VoiceResponse, Connect
        response = VoiceResponse()
        connect = Connect()
        # Pass scenario hint from Twilio SIP headers if present
        scenario_hint = body.get("scenario", "") if body else ""
        stream_url = f"{ws_url}/v1/test-media-stream/prospect/{call_id}"
        if scenario_hint:
            stream_url += f"?scenario={scenario_hint}"
        connect.stream(url=stream_url)
        response.append(connect)

        logger.info("test_prospect_answer_webhook",
            call_id=call_id,
            ws_url=stream_url)

        return Response(content=str(response), media_type="application/xml")

    @app.post("/v1/test-agent-answer", tags=["Testing"])
    @app.get("/v1/test-agent-answer", tags=["Testing"])
    async def test_agent_answer(request: Request):
        """
        Twilio webhook for test agent number (+13185586497).
        Simulates licensed agent Sarah answering a transfer.
        Auto-accepts the whisper and plays agent greeting.
        """
        try:
            body = await request.form()
            call_sid = body.get("CallSid", f"agent-{uuid.uuid4().hex[:8]}")
            call_id = f"agent-{call_sid[:12]}"
        except Exception:
            call_id = f"agent-{uuid.uuid4().hex[:8]}"

        logger.info("test_agent_answer_webhook", call_id=call_id)

        from ..test_actors import generate_agent_answer_twiml
        twiml = generate_agent_answer_twiml(call_id)
        return Response(content=twiml, media_type="application/xml")

    @app.websocket("/v1/test-media-stream/prospect/{call_id}")
    async def test_media_stream_prospect(websocket: WebSocket, call_id: str):
        """
        Media stream WebSocket for test prospect AI.

        Receives mulaw audio from Becky (the AI agent), runs it through
        continuous STT → LLM → TTS loop (same architecture as CallBridge Phase 3).

        The prospect responds based on its randomly-selected scenario.
        """
        from ..test_actors import select_scenario, TestProspectBridge, scenario_summary
        from ..providers.twilio_telephony import TwilioTelephony

        await websocket.accept()
        logger.info("test_prospect_media_stream_connected", call_id=call_id)

        # Select scenario (forced or random)
        forced = _forced_scenario.get("value")
        _forced_scenario["value"] = None  # Consume it
        scenario_enum, scenario_config = select_scenario(force=forced)
        summary = scenario_summary(scenario_enum, scenario_config)
        logger.info("test_prospect_scenario_selected",
            call_id=call_id,
            scenario=scenario_enum.value,
            summary=summary)

        # Create the prospect bridge
        bridge = TestProspectBridge(
            scenario=scenario_enum,
            scenario_config=scenario_config,
            call_id=call_id,
        )

        # Track in active calls
        active_calls[call_id] = {
            "status": CallStatus.ACTIVE,
            "start_time": time.time(),
            "bridge": bridge,
            "direction": "test-prospect",
            "scenario": scenario_enum.value,
            "scenario_summary": summary,
        }

        try:
            await bridge.connect_providers()

            twilio_telephony = TwilioTelephony(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                phone_number=settings.twilio_phone_number,
            )

            # The on_audio callback feeds PCM to the bridge's input queue
            async def on_audio(pcm_audio: bytes):
                bridge.on_audio_received(pcm_audio)

            # Start the prospect's continuous conversation loop in background
            conv_task = asyncio.create_task(bridge.run_conversation_loop())

            # Run the Twilio media stream handler (blocks until disconnect)
            await twilio_telephony.handle_media_stream(
                websocket=websocket,
                call_id=call_id,
                on_audio=on_audio,
                get_audio=bridge.get_audio,
            )

            # Media stream ended — stop the conversation loop
            bridge._active = False
            try:
                await asyncio.wait_for(conv_task, timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                conv_task.cancel()

        except Exception as e:
            logger.error("test_prospect_media_stream_error",
                call_id=call_id,
                scenario=scenario_enum.value,
                error=str(e))
        finally:
            try:
                await bridge.close()
            except Exception:
                pass

            if call_id in active_calls:
                active_calls[call_id]["status"] = CallStatus.COMPLETED
                active_calls[call_id]["end_time"] = time.time()

            logger.info("test_prospect_media_stream_closed",
                call_id=call_id,
                scenario=scenario_enum.value,
                turns=bridge._turn_count,
                duration_s=round(time.time() - active_calls.get(call_id, {}).get("start_time", time.time()), 1))

    @app.post("/v1/test-call-v2", tags=["Testing"])
    async def trigger_test_call_v2(
        scenario: Optional[str] = None,
        to_number: Optional[str] = None,
        transfer_did: Optional[str] = None,
        prospect_name: Optional[str] = None,
        api_key: str = Depends(verify_api_key),
    ):
        """
        Enhanced test call: Becky (AI agent) calls test prospect (+13185522502).
        Pass ?to_number=+1XXXXXXXXXX to call a real person instead.
        Pass ?transfer_did=+1XXXXXXXXXX to override the transfer agent number.

        Uses the NEW test prospect and test agent numbers:
        - Caller (Becky/AI): +13187222561 (main number) → calls test prospect
        - Test Prospect: +13185522502 (test number, answers with prospect AI via webhook)
        - Test Agent (Sarah): +13185586497 (test number, answers with agent AI via webhook)

        Flow:
        1. Our AI agent (Becky) is connected to media stream
        2. Becky dials the test prospect number (+13185522502)
        3. Test prospect webhook answers and returns our media stream URL
        4. Prospect AI runs scenario-based conversation
        5. If Becky initiates transfer, prospect number hangs up
           and Becky dials test agent number (+13185586497)
        6. Agent AI answers with simple greeting

        Returns call_id, call_sid, scenario selected, and test metadata.
        """
        if not settings.twilio_account_sid:
            raise HTTPException(status_code=400, detail="Twilio not configured")
        if not settings.twilio_phone_number:
            raise HTTPException(status_code=400, detail="Twilio phone number not configured")

        call_id = f"test-v2-{uuid.uuid4().hex[:8]}"

        # Use production Becky agent config
        from ..inbound_handler import (
            OUTBOUND_PITCH_TEXT, OUTBOUND_SYSTEM_PROMPT,
            VOICE_ID, TEMPERATURE, MAX_TOKENS, TRANSFER_CONFIG,
        )

        # Use real transfer number for real people, test agent for AI tests
        is_real_person = bool(to_number)
        if transfer_did:
            # Explicit transfer DID override
            transfer_cfg = {
                **(TRANSFER_CONFIG or {}),
                "agent_dids": [transfer_did],
            }
        elif is_real_person:
            transfer_cfg = TRANSFER_CONFIG or {}
        else:
            transfer_cfg = {
                **(TRANSFER_CONFIG or {}),
                "agent_dids": ["+13185586497"],  # Test agent number
            }

        agent_config = AgentConfig(
            agent_id="final-expense-becky-test-v2",
            system_prompt=OUTBOUND_SYSTEM_PROMPT,
            voice_id=VOICE_ID,
            greeting="Hi, can you hear me ok?",
            pitch_text=OUTBOUND_PITCH_TEXT,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            transfer_config=transfer_cfg,
        )

        # Create pipeline
        orchestrator = QualityPipeline.create()

        bridge = CallBridge(
            orchestrator=orchestrator,
            agent_config=agent_config,
            call_id=call_id,
        )

        # Pre-connect providers
        try:
            await orchestrator.tts.connect()
            await bridge.pre_synthesize_greeting()
            # Synthesize pitch, fillers, backchannel, and turn 1+2 cache in parallel
            await asyncio.gather(
                bridge.pre_synthesize_pitch(),
                bridge.pre_synthesize_fillers(),
                bridge.pre_synthesize_backchannel_audio(),
                bridge.pre_synthesize_turn1_cache(),
                bridge.pre_synthesize_turn2_cache(),
                bridge.pre_synthesize_semantic_cache(),
                bridge.pre_synthesize_recovery_audio(),
            )
            logger.info("test_call_v2_pre_synthesized",
                call_id=call_id,
                greeting_bytes=len(bridge._greeting_audio or b''),
                pitch_bytes=len(bridge._pitch_audio or b''),
                filler_count=len(bridge._filler_audio),
                turn1_cache_count=len(bridge._turn1_cache))
        except Exception as e:
            logger.warning("test_call_v2_pre_synth_failed",
                call_id=call_id, error=str(e))

        active_calls[call_id] = {
            "orchestrator": orchestrator,
            "bridge": bridge,
            "agent_config": agent_config,
            "status": CallStatus.ACTIVE,
            "start_time": time.time(),
            "pipeline_mode_str": "quality",
            "direction": "test-v2",
            "test_mode": True,
            "providers_pre_connected": True,
        }

        # Determine public base URL
        base_url = settings.base_url
        if not base_url:
            import os
            fly_app = os.environ.get("FLY_APP_NAME")
            if fly_app:
                base_url = f"https://{fly_app}.fly.dev"
            else:
                base_url = f"http://{settings.host}:{settings.port}"

        ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://")

        # Generate TwiML for Becky (our AI agent)
        our_twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Connect><Stream url="{ws_url}/v1/media-stream/{call_id}"/></Connect></Response>'
        )

        try:
            from twilio.rest import Client as TwilioClient
            twilio_client = TwilioClient(
                settings.twilio_account_sid, settings.twilio_auth_token
            )

            # Set forced scenario if provided
            if scenario:
                _forced_scenario["value"] = scenario

            # Determine call destination
            is_real_person = bool(to_number)
            dial_number = to_number or "+13185522502"  # Real person or test prospect

            call = twilio_client.calls.create(
                to=dial_number,
                from_=settings.twilio_phone_number,
                twiml=our_twiml,
            )

            # Set Twilio client on bridge so transfer manager can make real calls
            bridge.twilio_client = twilio_client
            bridge.twilio_call_sid = call.sid
            bridge.webhook_base_url = base_url
            bridge.prospect_name = prospect_name or ""  # Used in agent whisper
            active_calls[call_id]["twilio_client"] = twilio_client

            active_calls[call_id]["call_sid"] = call.sid
            active_calls[call_id]["is_real_person"] = is_real_person
            if not is_real_person:
                active_calls[call_id]["test_prospect_number"] = dial_number
                active_calls[call_id]["test_agent_number"] = "+13185586497"

            logger.info("test_call_v2_initiated",
                call_id=call_id,
                call_sid=call.sid,
                to_number=dial_number,
                is_real_person=is_real_person,
                ws_url=ws_url)

            return {
                "call_id": call_id,
                "call_sid": call.sid,
                "status": "initiated",
                "to_number": dial_number,
                "is_real_person": is_real_person,
                "message": (
                    f"Call started to {dial_number}. "
                    "Check GET /v1/test-call/" + call_id + " for results."
                ),
            }

        except Exception as e:
            logger.error("test_call_v2_failed", call_id=call_id, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    # ── A/B Testing Endpoints ────────────────────────────────────────────────
    @app.get("/v1/ab-test/status", tags=["A/B Testing"])
    async def get_ab_test_status(
        experiment_name: Optional[str] = None,
        api_key: str = Depends(verify_api_key)
    ):
        """
        Get status and results of A/B test experiments.

        Args:
            experiment_name: Optional. If provided, returns status for that experiment.
                           If omitted, returns status for all experiments.

        Returns:
            Single experiment status or list of all experiments with their results.
        """
        manager = await get_ab_test_manager()

        if experiment_name:
            status = await manager.get_experiment_status(experiment_name)
            if not status:
                raise HTTPException(status_code=404, detail=f"Experiment '{experiment_name}' not found")
            return status
        else:
            # Return all experiments
            all_experiments = await manager.list_experiments()
            return {
                "total_experiments": len(all_experiments),
                "experiments": all_experiments,
            }

    @app.post("/v1/ab-test/create", tags=["A/B Testing"])
    async def create_ab_test(
        request: dict,
        api_key: str = Depends(verify_api_key)
    ):
        """
        Create a new A/B test experiment.

        Request body:
        {
            "name": "my_experiment",
            "description": "Test description",
            "metric": "transfer_rate",  // or "grade_score", "latency_p95", etc.
            "variant_a": {"temperature": 0.7, "max_tokens": 40},
            "variant_b": {"temperature": 0.8, "max_tokens": 40},
            "min_samples_per_variant": 20
        }
        """
        try:
            from ..ab_testing import ExperimentConfig, VariantConfig

            variant_a = VariantConfig(**request.get("variant_a", {}))
            variant_b = VariantConfig(**request.get("variant_b", {}))

            config = ExperimentConfig(
                name=request["name"],
                description=request["description"],
                metric=request["metric"],
                variant_a=variant_a,
                variant_b=variant_b,
                min_samples_per_variant=request.get("min_samples_per_variant", 20),
            )

            manager = await get_ab_test_manager()
            created = await manager.create_experiment(config)

            if not created:
                raise HTTPException(
                    status_code=409,
                    detail=f"Experiment '{config.name}' already exists"
                )

            logger.info("ab_test_created", experiment_name=config.name)
            return {
                "status": "created",
                "experiment_name": config.name,
                "description": config.description,
                "metric": config.metric,
            }

        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
        except Exception as e:
            logger.error("ab_test_create_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/ab-test/assign", tags=["A/B Testing"])
    async def assign_ab_test_variant(
        request: dict,
        api_key: str = Depends(verify_api_key)
    ):
        """
        Assign a call to an A/B test variant.

        Called during call initialization to determine which variant
        the call should use.

        Request body:
        {
            "call_id": "abc123",
            "experiment_name": "speed_test"
        }

        Returns:
        {
            "call_id": "abc123",
            "experiment_name": "speed_test",
            "variant": "variant_a",
            "config_overrides": {"speed": 0.97}
        }
        """
        try:
            call_id = request["call_id"]
            experiment_name = request["experiment_name"]

            manager = await get_ab_test_manager()

            # Assign variant
            variant = await manager.assign_variant(call_id, experiment_name)
            if not variant:
                raise HTTPException(
                    status_code=404,
                    detail=f"Experiment '{experiment_name}' not found"
                )

            # Get the experiment config to return overrides
            exp_status = await manager.get_experiment_status(experiment_name)
            if variant == Variant.A:
                config_overrides = exp_status["variant_a"]["config"]
            else:
                config_overrides = exp_status["variant_b"]["config"]

            # Remove None values
            config_overrides = {k: v for k, v in config_overrides.items() if v is not None}

            logger.info("ab_test_variant_assigned",
                call_id=call_id,
                experiment_name=experiment_name,
                variant=variant.value)

            return {
                "call_id": call_id,
                "experiment_name": experiment_name,
                "variant": variant.value,
                "config_overrides": config_overrides,
            }

        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
        except Exception as e:
            logger.error("ab_test_assign_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/ab-test/record-result", tags=["A/B Testing"])
    async def record_ab_test_result(
        request: dict,
        api_key: str = Depends(verify_api_key)
    ):
        """
        Record the result of a call in an A/B test experiment.

        Called after a call is graded to feed results back to the AB test manager.

        Request body:
        {
            "call_id": "abc123",
            "experiment_name": "speed_test",
            "grade_score": 78.5,
            "transfer_attempted": true,
            "transfer_completed": true,
            "latency_p95_ms": 450.0,
            "latency_avg_ms": 320.0,
            "total_turns": 6,
            "duration_seconds": 125.3,
            "cost_usd": 0.42
        }
        """
        try:
            call_id = request["call_id"]
            experiment_name = request["experiment_name"]

            manager = await get_ab_test_manager()

            recorded = await manager.record_result(
                call_id=call_id,
                experiment_name=experiment_name,
                grade_score=request.get("grade_score", 0.0),
                transfer_attempted=request.get("transfer_attempted", False),
                transfer_completed=request.get("transfer_completed", False),
                latency_p95_ms=request.get("latency_p95_ms", 0.0),
                latency_avg_ms=request.get("latency_avg_ms", 0.0),
                total_turns=request.get("total_turns", 0),
                duration_seconds=request.get("duration_seconds", 0.0),
                cost_usd=request.get("cost_usd", 0.0),
            )

            if not recorded:
                logger.warning("ab_test_result_not_recorded",
                    call_id=call_id,
                    experiment_name=experiment_name,
                    reason="Call not found in experiment or already recorded")
                return {
                    "recorded": False,
                    "reason": "Call not found in experiment or already recorded"
                }

            logger.info("ab_test_result_recorded",
                call_id=call_id,
                experiment_name=experiment_name,
                grade_score=request.get("grade_score", 0.0))

            return {
                "recorded": True,
                "call_id": call_id,
                "experiment_name": experiment_name,
            }

        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"Missing required field: {e}")
        except Exception as e:
            logger.error("ab_test_record_failed", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/v1/ab-test/stop", tags=["A/B Testing"])
    async def stop_ab_test(
        experiment_name: str,
        api_key: str = Depends(verify_api_key)
    ):
        """Stop an A/B test experiment."""
        manager = await get_ab_test_manager()
        stopped = await manager.stop_experiment(experiment_name)

        if not stopped:
            raise HTTPException(status_code=404, detail=f"Experiment '{experiment_name}' not found")

        logger.info("ab_test_stopped", experiment_name=experiment_name)
        return {
            "stopped": True,
            "experiment_name": experiment_name,
        }

    @app.delete("/v1/ab-test/{experiment_name}", tags=["A/B Testing"])
    async def delete_ab_test(
        experiment_name: str,
        api_key: str = Depends(verify_api_key)
    ):
        """Delete an A/B test experiment and all its results."""
        manager = await get_ab_test_manager()
        deleted = await manager.delete_experiment(experiment_name)

        if not deleted:
            raise HTTPException(status_code=404, detail=f"Experiment '{experiment_name}' not found")

        logger.info("ab_test_deleted", experiment_name=experiment_name)
        return {
            "deleted": True,
            "experiment_name": experiment_name,
        }

    # ── Website Analysis + Demo Generation Endpoints ─────────────────────────

    @app.post("/v1/analyze-site", tags=["Growth"])
    async def analyze_site(request: Request):
        """
        Analyze a website URL and recommend AI Employees.
        Returns: business name, industry, description, and 2-4 AI Employee recommendations
        each with a personalized demo script.
        """
        import httpx
        import json as _json
        import re

        body = await request.json()
        url = body.get("url", "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")

        # Normalize URL
        if not url.startswith("http"):
            url = "https://" + url

        # 1. Fetch website content
        site_text = ""
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; WellHeardBot/1.0)"
                })
                html = resp.text[:30000]  # Cap at 30k chars
                # Strip tags, keep text
                site_text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
                site_text = re.sub(r'<style[^>]*>.*?</style>', '', site_text, flags=re.DOTALL|re.IGNORECASE)
                site_text = re.sub(r'<[^>]+>', ' ', site_text)
                site_text = re.sub(r'\s+', ' ', site_text).strip()[:8000]
        except Exception as e:
            logger.warning("site_fetch_failed", url=url, error=str(e))
            site_text = f"Could not fetch {url}. Domain appears to be: {url.split('//')[1].split('/')[0]}"

        # 2. Call Groq LLM to analyze
        if not settings.groq_api_key:
            raise HTTPException(status_code=503, detail="LLM not configured")

        from groq import AsyncGroq
        groq_client = AsyncGroq(api_key=settings.groq_api_key)

        analysis_prompt = f"""Analyze this website and recommend AI voice employees for this business.

WEBSITE URL: {url}
WEBSITE CONTENT (excerpt):
{site_text[:5000]}

Return ONLY valid JSON (no markdown, no code fences) with this exact structure:
{{
  "business_name": "The company name found on the website",
  "industry": "One of: insurance, real_estate, solar, healthcare, home_services, legal, financial, automotive, agency, call_center, other",
  "description": "One sentence describing what this company does",
  "employees": [
    {{
      "name": "A human-sounding name for this AI employee (e.g. 'Sarah' or 'Michael')",
      "role": "SDR / Appointment Setter / Lead Qualifier / Follow-Up Specialist / Customer Service Rep",
      "title": "Short job title like 'Medicare SDR' or 'Solar Appointment Setter'",
      "description": "What this AI employee does for the business in 1-2 sentences",
      "demo_script": "An exact 2-3 sentence opening of a phone call this AI would make. Use the real business name. Sound natural, warm, and human. Example: 'Hi, this is Sarah calling from [Business]. I'm reaching out because we noticed you requested some information about [topic]. Do you have just a quick moment?'",
      "voice": "female_warm",
      "impact": "A short metric like 'Handles 500+ calls/day' or 'Books 3x more appointments'"
    }}
  ]
}}

Generate 2-4 AI employees that would be most valuable for THIS specific business. Make the demo_scripts feel real and personalized to their actual business. Use the business name in every script."""

        try:
            completion = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are a business analyst. Return only valid JSON, no markdown."},
                    {"role": "user", "content": analysis_prompt},
                ],
                temperature=0.6,
                max_tokens=1500,
            )
            raw_text = completion.choices[0].message.content.strip()
            # Clean any markdown fences
            raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)
            analysis = _json.loads(raw_text)
        except Exception as e:
            logger.error("site_analysis_llm_failed", error=str(e))
            # Fallback analysis
            domain = url.split("//")[1].split("/")[0].replace("www.", "")
            biz_name = domain.split(".")[0].title()
            analysis = {
                "business_name": biz_name,
                "industry": "other",
                "description": f"Business at {domain}",
                "employees": [
                    {
                        "name": "Sarah",
                        "role": "SDR",
                        "title": "Outbound Sales Rep",
                        "description": f"Makes outbound calls to qualify leads for {biz_name}.",
                        "demo_script": f"Hi, this is Sarah calling from {biz_name}. I'm reaching out because we have some new offerings I thought might interest you. Do you have just a quick moment?",
                        "voice": "female_warm",
                        "impact": "Handles 500+ calls/day",
                    },
                    {
                        "name": "Michael",
                        "role": "Follow-Up Specialist",
                        "title": "Follow-Up Agent",
                        "description": f"Follows up with leads who expressed interest but haven't converted.",
                        "demo_script": f"Hey there, this is Michael from {biz_name}. I'm calling to follow up on your recent inquiry — I want to make sure you got all the information you needed. Is now a good time?",
                        "voice": "male_warm",
                        "impact": "Recovers 30% of lost leads",
                    },
                ],
            }

        logger.info("site_analyzed", url=url, business=analysis.get("business_name"), employees=len(analysis.get("employees", [])))
        return analysis

    @app.post("/v1/generate-demo", tags=["Growth"])
    async def generate_demo(request: Request):
        """
        Generate a TTS audio demo for an AI Employee.
        Takes a script and voice preference, returns base64-encoded WAV audio.
        """
        import base64
        import struct

        body = await request.json()
        script = body.get("script", "").strip()
        voice_pref = body.get("voice", "female_warm")
        employee_name = body.get("name", "AI Employee")

        if not script:
            raise HTTPException(status_code=400, detail="script is required")

        if not settings.cartesia_api_key:
            raise HTTPException(status_code=503, detail="TTS not configured")

        # Map voice preferences to Cartesia voice IDs
        from ..providers.cartesia_tts import VOICE_PRESETS
        voice_map = {
            "female_warm": "vicky",
            "female_calm": "victoria",
            "female_upbeat": "molly",
            "male_warm": "liam",
            "male_steady": "ben",
        }
        preset_name = voice_map.get(voice_pref, "vicky")
        preset = VOICE_PRESETS.get(preset_name, VOICE_PRESETS["vicky"])
        voice_id = preset["id"]

        # Use Cartesia HTTP API for one-shot synthesis (simpler than WebSocket for single request)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.cartesia.ai/tts/bytes",
                    headers={
                        "Cartesia-Version": "2024-06-10",
                        "X-API-Key": settings.cartesia_api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "model_id": "sonic-3",
                        "transcript": script,
                        "voice": {"mode": "id", "id": voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                        "language": "en",
                    },
                )
                if resp.status_code != 200:
                    logger.error("cartesia_http_error", status=resp.status_code, body=resp.text[:200])
                    raise HTTPException(status_code=502, detail="TTS generation failed")

                pcm_data = resp.content
        except httpx.HTTPError as e:
            logger.error("cartesia_request_failed", error=str(e))
            raise HTTPException(status_code=502, detail="TTS service unavailable")

        # Convert raw PCM to WAV
        sample_rate = 24000
        num_channels = 1
        bits_per_sample = 16
        data_size = len(pcm_data)
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8

        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF',
            36 + data_size,
            b'WAVE',
            b'fmt ',
            16,               # fmt chunk size
            1,                # PCM format
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data',
            data_size,
        )
        wav_bytes = wav_header + pcm_data
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

        duration_sec = round(data_size / byte_rate, 1)
        logger.info("demo_generated", employee=employee_name, voice=preset_name, duration=duration_sec)

        return {
            "audio_base64": audio_b64,
            "format": "wav",
            "sample_rate": sample_rate,
            "duration_seconds": duration_sec,
            "employee_name": employee_name,
        }

    @app.post("/v1/create-checkout", tags=["Growth"])
    async def create_checkout(request: Request):
        """
        Create a Stripe Checkout session for plan activation.
        Applies 50% off first month coupon automatically.
        """
        body = await request.json()
        plan = body.get("plan", "starter")
        email = body.get("email", "")
        business_name = body.get("business_name", "")
        success_url = body.get("success_url", "https://wellheard.ai/get-started?checkout=success")
        cancel_url = body.get("cancel_url", "https://wellheard.ai/get-started?checkout=cancel")

        # Check for Stripe key
        stripe_key = getattr(settings, "stripe_secret_key", "") or ""
        if not stripe_key:
            # Stripe not configured yet — return mailto fallback
            logger.warning("stripe_not_configured")
            return {
                "checkout_url": None,
                "fallback": "mailto",
                "mailto_url": f"mailto:hello@wellheard.ai?subject=Activate%20{plan}%20plan&body=Email:%20{email}%0ABusiness:%20{business_name}%0APlan:%20{plan}",
                "message": "Payment processing is being set up. We'll send you an invoice shortly.",
            }

        try:
            import stripe
            stripe.api_key = stripe_key

            # Price IDs should be configured in settings
            price_map = {
                "starter": getattr(settings, "stripe_price_starter", ""),
                "agency": getattr(settings, "stripe_price_agency", ""),
            }
            price_id = price_map.get(plan)
            if not price_id:
                raise HTTPException(status_code=400, detail=f"Unknown plan: {plan}")

            # Create checkout session with 50% off first month
            session_params = {
                "mode": "subscription",
                "payment_method_types": ["card"],
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "allow_promotion_codes": True,
            }

            # Apply 50% off coupon if configured
            coupon_id = getattr(settings, "stripe_coupon_50off", "")
            if coupon_id:
                session_params["discounts"] = [{"coupon": coupon_id}]

            if email:
                session_params["customer_email"] = email

            session_params["metadata"] = {
                "plan": plan,
                "business_name": business_name,
                "source": "wellheard_funnel",
            }

            session = stripe.checkout.Session.create(**session_params)
            logger.info("checkout_created", plan=plan, email=email)

            return {
                "checkout_url": session.url,
                "session_id": session.id,
            }

        except Exception as e:
            logger.error("stripe_checkout_failed", error=str(e))
            return {
                "checkout_url": None,
                "fallback": "mailto",
                "mailto_url": f"mailto:hello@wellheard.ai?subject=Activate%20{plan}%20plan&body=Email:%20{email}%0ABusiness:%20{business_name}%0APlan:%20{plan}",
                "message": "Payment processing encountered an error. We'll follow up via email.",
            }

    return app


# ── Entry point ───────────────────────────────────────────────────────────
app = create_app()
