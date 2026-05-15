import argparse
import asyncio
from .server import run_server

def main():
    parser = argparse.ArgumentParser(description="OpenClaw ACP TCP Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=18781, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    parser.add_argument("--token", help="Authentication token required for clients")
    parser.add_argument("--openclaw-path", default="openclaw", help="Path to the openclaw binary")
    parser.add_argument("--no-http", action="store_true", help="Disable the high-speed HTTP side-channel and use Base64 blobs instead")
    args = parser.parse_args()

    asyncio.run(run_server(args.host, args.port, args.debug, args.token, args.openclaw_path, use_http=(not args.no_http)))

if __name__ == "__main__":
    main()
