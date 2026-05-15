# OpenClaw ACP Bridge

A high-performance, persistent TCP bridge and client for the OpenClaw Agent Control Protocol (ACP).

The **OpenClaw ACP Bridge** solves the challenge of maintaining persistent agent sessions over standard TCP while providing a high-speed "side-channel" for large-scale file transfers. It is designed for high-performance agentic workflows where large binary assets (images, videos, datasets) need to be moved efficiently between remote agents and local clients.

## 🚀 Key Features

- **Persistent Agent Session**: Unlike standard ACP tools that may restart agents per request, the bridge maintains a single persistent agent process across multiple client turns.
- **High-Speed HTTP Side-Channel**: Automatically switches to HTTP streaming for large files (GB-sized), bypassing JSON-RPC/Base64 overhead and memory bloat.
- **Explicit File Interception**: Use `/filerequest <path>` to instantly fetch any file from the remote agent's filesystem.
- **Auto-File Retrieval**: Intelligently detects the `[FILEPATH: /path/to/file]` pattern in agent responses and automatically initiates a high-speed transfer.
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

        # 2. Explicit File Request
        # This uses the high-speed side-channel automatically
        response = await client.chat("/filerequest /path/to/large_dataset.zip")
        if response.files:
            print(f"Received file: {response.files[0]}")

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

## 📜 License
BSD 3-Clause
