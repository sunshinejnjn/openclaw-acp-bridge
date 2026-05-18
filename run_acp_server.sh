#!/bin/bash

# =================================================================
# OpenClaw ACP Bridge Server Launcher
# =================================================================

# Stop any conflicting openclaw-acp, run_acp_server.py or openclaw_acp_bridge processes
echo "🧹 Stopping any existing openclaw-acp, run_acp_server.py, or openclaw_acp_bridge processes..."
pkill -f openclaw-acp 2>/dev/null || true
pkill -f run_acp_server.py 2>/dev/null || true
pkill -f openclaw_acp_bridge 2>/dev/null || true
sleep 1

# 1. Authentication Token
# If not specified here, the server will look for a 'token.txt' file.
#TOKEN="your-secret-token"

# 2. OpenClaw Binary Path
# Change this if 'openclaw' is not in your system PATH.
OPENCLAW_PATH="openclaw"

# 3. Server Configuration
HOST="0.0.0.0"
PORT=18781
AGENT="agentchatter"

# 4. Detect Python command
if command -v python &> /dev/null; then
    PYTHON_CMD="python"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    echo "❌ Error: Python not found. Please install Python 3."
    exit 1
fi

echo "🚀 Starting OpenClaw ACP Bridge using $PYTHON_CMD..."
echo "📍 Host: $HOST"
echo "🔢 Port: $PORT"
echo "📂 Side-channel HTTP: $(($PORT + 1))"

# Launch the server module
exec $PYTHON_CMD -m openclaw_acp_bridge \
    --host "$HOST" \
    --port "$PORT" \
    --token "$TOKEN" \
    --openclaw-path "$OPENCLAW_PATH" \
    --agent "$AGENT" \
    --debug

# To disable the HTTP side-channel, add the --no-http flag above.
