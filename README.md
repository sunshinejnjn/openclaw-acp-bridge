# OpenClaw ACP Bridge

A high-performance TCP bridge and async client for OpenClaw ACP. Make your OpenClaw Agent an ACP server on TCP! Call OpenClaw in your python projects with ease!

The **OpenClaw ACP Bridge** solves the challenge of maintaining persistent agent sessions over standard TCP while providing a high-speed "side-channel" for large-scale file transfers. It is designed for high-performance agentic workflows where large binary assets (images, videos, datasets) need to be moved efficiently between remote agents and local clients.

## 🚀 Key Features

- **Persistent Agent Session**: Unlike standard ACP tools that may restart agents per request, the bridge maintains a single persistent agent process across multiple client turns.
- **Real-time Response Streaming**: Stream agent responses token-by-token (chunk-by-chunk) for an interactive, low-latency UI experience.
- **High-Speed HTTP Side-Channel**: Automatically switches to HTTP streaming for large files (GB-sized), bypassing JSON-RPC/Base64 overhead and memory bloat.
- **Explicit File Interception**: Use `/filerequest <path>` to instantly fetch any file from the remote agent's filesystem.
- **Improved Auto-File Retrieval**: Intelligently detects `[FILEPATH: /path/to/file]` and `file:///path/to/file` patterns in agent responses and automatically initiates a high-speed transfer.
- **Zero-Wait Synchronization**: New v0.5.0 client ensures all background image downloads are fully synchronized before yielding final responses, eliminating race conditions in high-concurrency environments.
- **Async/Non-Blocking**: Built from the ground up for `asyncio`, utilizing `httpx` for reliable binary streaming.
- **Environment Consistency**: Server-side agent launching utilizes interactive shells (`bash -i`) to ensure `.bashrc`, NVM, and local paths are correctly resolved.

## 📦 Installation
On the server (host where your openclaw gateway runs on) side, as the acp_server_bridge.
And the client side if you want to run with our client interface. Or you can use acp directly to connect to the acp_server_bridge using TCP directly (pip install agent-client-protocol).
```bash
pip install openclaw-acp-bridge
```

## 🛠️ Usage

### 1. Launch the Bridge Server
On your remote server (where OpenClaw is installed), start the bridge using the provided helper script:

```bash
chmod +x run_acp_server.sh
./run_acp_server.sh
```

Or run the module directly:

```bash
python -m openclaw_acp_bridge --host 0.0.0.0 --port 18781 --debug
```

*Note: The bridge will also open a side-channel HTTP server on `port + 1` (default 18782).*

### 2. Connect the Async Client
On your local machine, use the `OpenClaw` client to interact with the remote agent:

```python
import asyncio
from openclaw_acp_bridge import OpenClaw

async def main():
    # Connect to the remote bridge
    async with OpenClaw(host="10.71.253.132", download_dir="my_assets") as client:
        # 1. Standard Chat
        response = await client.chat("Hello, who are you?")
        print(f"Agent: {response.text}")

        # 2. Real-time Streaming
        async for chunk in client.chat_stream("Explain quantum physics simply."):
            if isinstance(chunk, str):
                print(chunk, end="", flush=True)
            else:
                # Final response object containing full text and file paths
                print(f"\nFinal response has {len(chunk.files)} files.")

        # 3. Explicit File Request

        # 3. Auto-Retrieval Pattern
        # Ask the agent to generate something and return the path
        response = await client.chat("Generate a report and return path in [FILEPATH: /path] format.")
        # The client automatically detects the pattern and fetches the file!
        for file in response.files:
            print(f"Auto-downloaded: {file}")

if __name__ == "__main__":
    asyncio.run(main())
```

## 🔌 Using with Standard ACP Clients

The bridge is fully compatible with any standard ACP-compliant client or SDK. To use it, simply point your client to the bridge's TCP address (default port `18781`).

### Why use the Bridge with standard clients?
1. **Persistence**: Even with a standard client, the bridge keeps your remote agent process alive across sessions.
2. **Environment**: The bridge handles the complex `bash -i` shell environment setup for you.
3. **Special Commands**: You can still use `/filerequest <path>` in your prompts. The bridge will intercept these and return a standard ACP `resource` block.

*Note: When using a standard client, the high-speed HTTP side-channel will return a `resource` block with a `uri` starting with `http://`. Ensure your client can handle HTTP-based resources or use our provided `openclaw_acp_bridge` client for automatic handling.*

## ⚙️ Advanced Configuration

### Bridge Server Options
You can customize the server behavior using CLI arguments:

```bash
python -m openclaw_acp_bridge --port 18781 --token "my-secret-key" --openclaw-path "/usr/local/bin/openclaw"
```

| Parameter | Description |
| :--- | :--- |
| `--host` | Host to bind the TCP server to (default: `0.0.0.0`) |
| `--port` | Port to listen on (default: `18781`) |
| `--token` | Optional authentication token. If set, clients must provide this token to connect. |
| `--openclaw-path` | Path to the `openclaw` binary on the server (default: `openclaw`) |
| `--no-http` | Disable the high-speed HTTP side-channel. When set, the bridge uses standard Base64 blobs for all file transfers. |
| `--debug` | Enable verbose logging for debugging. |

### Authentication
The bridge supports simple token-based authentication. You can either pass the token via the `--token` CLI argument or place a `token.txt` file in the server's working directory. The `--token` argument takes precedence.

### Download Directory
The client allows you to specify where downloaded assets should be stored:
```python
client = OpenClaw(host="...", download_dir="./downloads")
```

## 🧪 Testing
The package includes a comprehensive test suite `test_bridge.py` that demonstrates chat, small file blobs, and large-scale (100MB+) HTTP streaming.

```bash
python test_bridge.py --tests 1,2,3,4
```

## 🔄 Version 0.6.0 Updates

- **Isolated Agent Targeting (`--agent <agent_name>`)**: Start the bridge with a specific agent name using the `--agent` parameter in the launcher. The bridge handles agent routing and isolated workspace initialization automatically.
- **Spawned ACP Session Keys**: Spawns isolated session keys in the format `agent:<agent_name>:<session_id>` (e.g. `agent:agentchatter:acp`) instead of overriding the default `main (agent:main:main)` session. This enables multiple bridges and agents to run concurrently without session overlap.
- **Self-Healing Conflict Resolution**: Automatically stops and resolves stale `openclaw-acp`, `run_acp_server.py`, or `openclaw_acp_bridge` daemon instances on server boot/restart to guarantee a fresh ACP TCP listener.
- **Standardized systemd Service Template (`openclaw-acp-bridge.service`)**:
  - Implements complete environment variables configuration (`PATH`, `HOME`, `TMPDIR`) to cleanly load NVM/Node paths.
  - Implements optimized PID replacement (`exec` substitution) so systemd tracks the Python server directly as the `Main PID` instead of a bash wrapper script.
  - Uses robust `KillMode=control-group` process management to prevent zombie or orphaned subprocesses.

## 🚀 Version 0.7.0 Updates

- **Universal File & Media Attachments**: Both `chat()` and `chat_stream()` now support an `attachments` parameter accepting local file paths, base64 strings, or raw block dicts, allowing seamless file delivery (images, documents, archives) directly to OpenClaw.
- **Automated Client-Side Image Compression**: Integrated smart Pillow compression/transcoding into JPEG format (with transparent layer compositing and configurable max dimensions) to substantially reduce bandwidth consumption and transfer latency.
- **Server-Side MIME Landing (`/acp/incoming`)**: Media and MIME payload blocks sent through ACP are now automatically extracted and saved to a dedicated incoming directory on the server disk, prepend-injected into the prompt as textual file paths so agent tools can process them locally.
- **Binary MIME Sniffing & Path Sanitization**: Added magic bytes inspection for precise MIME detection (JPEG, PNG, GIF, WebP, PDF, ZIP) and invalid/dummy path filtering during file transfers.

## 🚀 Version 0.6.1 Updates

- **Multimodal `chat_stream` Support**: `chat_stream()` now accepts an optional `image_data` (base64-encoded bytes) and `image_mime` parameter, allowing image+text prompts to be sent to the agent in a single streaming call. Enables i2i (image-to-image) and vision-based agentic workflows without a separate API.
- **Proactive Dead-Process Detection on Connect**: The server now immediately checks whether the backing OpenClaw process is alive the moment a new client authenticates and connects, triggering an automatic `restart_openclaw_process()` before the first message is forwarded — preventing silent failures on stale sessions.
- **Debug Log Truncation**: Agent↔Client log lines in `--debug` mode are now capped at 500 characters with a `... (truncated)` suffix, preventing log flooding when large base64 payloads (e.g. images) are exchanged.
- **Increased TCP Read Buffer**: `asyncio.start_server` is now launched with `limit=16*1024*1024` (16 MB), supporting large prompt payloads such as base64-encoded images that exceed the previous default 64 KB stream buffer.

## 📜 License
BSD 3-Clause

