"""
WellHeard AI - Main Entry Point
Start the API server with: python main.py
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from config.settings import settings
from src.call_logger import setup_file_logging


def main():
    # Set up file-based logging BEFORE anything else
    setup_file_logging()
    print(f"""
    ╔══════════════════════════════════════════════════════════════╗
    ║                    WellHeard AI v1.0                        ║
    ║                    Voice AI Platform                         ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  Budget Pipeline:  ~$0.021/min                              ║
    ║  Quality Pipeline: ~$0.032/min                              ║
    ║                                                              ║
    ║  API Docs:  http://{settings.host}:{settings.port}/docs     ║
    ║  Health:    http://{settings.host}:{settings.port}/v1/health║
    ╚══════════════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        "src.api.server:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info",
        workers=1,  # Single worker for WebSocket support; scale with pods
    )


if __name__ == "__main__":
    main()
