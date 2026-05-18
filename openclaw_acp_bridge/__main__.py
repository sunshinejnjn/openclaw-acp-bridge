import argparse
import asyncio
import json
import os
import sys
from .server import run_server

def main():
    parser = argparse.ArgumentParser(description="OpenClaw ACP TCP Bridge")
    parser.add_argument("--host", help="Host to bind to")
    parser.add_argument("--port", type=int, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    parser.add_argument("--token", help="Authentication token required for clients")
    parser.add_argument("--openclaw-path", help="Path to the openclaw binary")
    parser.add_argument("--no-http", action="store_true", help="Disable the high-speed HTTP side-channel")
    parser.add_argument("--session", help="Default session key for OpenClaw (e.g. agent:main:main)")
    parser.add_argument("--reset-session", action="store_true", help="Reset the session key before first use")
    parser.add_argument("--agent", help="OpenClaw agent to serve (e.g., main, gemini, opencode)")
    args = parser.parse_args()

    # Load from config.json if it exists (check current directory and module directory)
    config_data = {}
    config_paths = [
        "config.json",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    ]
    for path in config_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                break
            except Exception as e:
                pass

    # Merge hierarchy: CLI argument (if provided) > config.json (if present) > default value
    host = args.host or config_data.get("host") or "0.0.0.0"
    port = args.port or config_data.get("port") or 18781
    is_debug = args.debug or config_data.get("debug") or False
    token = args.token or config_data.get("token")
    openclaw_path = args.openclaw_path or config_data.get("openclaw_path") or "openclaw"
    no_http = args.no_http or config_data.get("no_http") or False
    use_http = not no_http
    
    agent = args.agent or config_data.get("agent") or "main"
    
    # Session defaults to agent:{agent}:acp if not specified (cli or config)
    session = args.session or config_data.get("session")
    if not session:
        session = f"agent:{agent}:acp"
        # Default reset_session to True for dynamic spawned session to ensure it gets spawned cleanly
        reset_session = args.reset_session or config_data.get("reset_session") or True
    else:
        reset_session = args.reset_session or config_data.get("reset_session") or False

    asyncio.run(run_server(
        host=host,
        port=port,
        is_debug=is_debug,
        token=token,
        openclaw_path=openclaw_path,
        use_http=use_http,
        session=session,
        reset_session=reset_session,
        agent=agent
    ))

if __name__ == "__main__":
    main()
