#!/bin/bash

# =================================================================
# OpenClaw ACP Bridge Server Launcher
# =================================================================

# 1. Authentication Token
# If not specified here, the server will look for a 'token.txt' file.
#TOKEN="your-secret-token"

# 2. OpenClaw Binary Path
# Change this if 'openclaw' is not in your system PATH.
OPENCLAW_PATH="openclaw"

# 3. Server Configuration
HOST="0.0.0.0"
PORT=18781

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
$PYTHON_CMD -m openclaw_acp_bridge \
    --host "$HOST" \
    --port "$PORT" \
    --token "$TOKEN" \
    --openclaw-path "$OPENCLAW_PATH" \
    --debug

# To disable the HTTP side-channel, add the --no-http flag above.
