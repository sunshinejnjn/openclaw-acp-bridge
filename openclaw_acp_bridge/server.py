import asyncio
import sys
import json
import os
import base64
import io
import zipfile
import mimetypes
import argparse
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Global state to manage the single persistent process
process = None
active_writer = None
# Track files for the side-channel HTTP server
served_files = {} # {uuid: full_path}

class FileServerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Path is /<file_id> or /<file_id>/<filename>
        parts = self.path.strip("/").split("/")
        file_id = parts[0]
        if file_id in served_files:
            full_path = served_files[file_id]
            filename = os.path.basename(full_path)
            with open(full_path, 'rb') as f:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(os.path.getsize(full_path)))
                self.end_headers()
                self.wfile.write(f.read())
            # Optional: remove from served_files after one download
            # del served_files[file_id]
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args): pass # Silence logs

def start_http_server(port):
    httpd = HTTPServer(('0.0.0.0', port), FileServerHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port

async def run_server(host="0.0.0.0", port=18781, is_debug=False, token=None, openclaw_path="openclaw", use_http=True):
    global process, active_writer

    # Start side-channel HTTP server on port + 1
    http_port = port + 1
    if use_http:
        start_http_server(http_port)
        print(f"Side-channel HTTP server listening on {host}:{http_port}")
    else:
        print("Side-channel HTTP server disabled. Using Base64 blobs for all transfers.")

    # Helper to restart the persistent OpenClaw process
    async def restart_openclaw_process():
        global process
        print("⚠️ Restarting persistent OpenClaw ACP subprocess...", file=sys.stderr)
        if process:
            try:
                process.terminate()
                await process.wait()
            except Exception as e:
                if is_debug:
                    print(f"Error terminating process: {e}", file=sys.stderr)
        
        process = await asyncio.create_subprocess_exec(
            "bash", "-i", "-c", f"{openclaw_path} acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr
        )
        print("Fresh OpenClaw ACP subprocess launched successfully.", file=sys.stderr)
        asyncio.create_task(agent_to_client(process))

    # Start a background task to forward Agent stdout to all active clients
    async def agent_to_client(current_process):
        try:
            async for line in current_process.stdout:
                line_str = line.decode('utf-8', errors='ignore')
                if is_debug:
                    print(f"Agent -> Clients: {line_str.strip()}", file=sys.stderr)
                
                # Auto-heal on stale session
                if "ACP_SESSION_INIT_FAILED" in line_str or "ACP metadata is missing" in line_str:
                    print("⚠️ Stale session detected in agent output! Triggering auto-restart...", file=sys.stderr)
                    # Trigger restart if this is still the active process
                    if current_process == process:
                        asyncio.create_task(restart_openclaw_process())
                
                if active_writer and current_process == process:
                    try:
                        active_writer.write(line)
                        await active_writer.drain()
                    except:
                        pass
        except Exception as e:
            if is_debug:
                print(f"Agent stream error: {e}", file=sys.stderr)

    # Launch initial subprocess
    process = await asyncio.create_subprocess_exec(
        "bash", "-i", "-c", f"{openclaw_path} acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr # Forward stderr to see agent logs
    )
    print(f"TCP Bridge listening on {host}:{port}")
    asyncio.create_task(agent_to_client(process))

    async def handle_client(reader, writer):
        global active_writer
        addr = writer.get_extra_info('peername')
        local_ip = writer.get_extra_info('sockname')[0]
        
        # Simple token-based authentication
        expected_token = token
        if not expected_token:
            token_path = os.path.join(os.getcwd(), "token.txt")
            if os.path.exists(token_path):
                with open(token_path, "r") as f:
                    expected_token = f.read().strip()
        
        if expected_token:
                try:
                    auth_line = await reader.readline()
                    if auth_line.decode('utf-8').strip() != expected_token:
                        print(f"Failed authentication from {addr}", file=sys.stderr)
                        writer.close()
                        return
                except Exception as e:
                    if is_debug: print(f"Auth Error: {e}", file=sys.stderr)
                    writer.close()
                    return

        if active_writer is not None:
            print(f"Preempting existing connection from {active_writer.get_extra_info('peername')}", file=sys.stderr)
            try:
                active_writer.close()
            except:
                pass

        print(f"Client {addr} authenticated and connected successfully", file=sys.stderr)
        active_writer = writer

        try:
            while True:
                if process.returncode is not None:
                    print("Background process died, closing connection", file=sys.stderr)
                    break
                
                line = await reader.readline()
                if not line:
                    break
                
                # Check for raw plain-text control commands (e.g. /acp spawn, /restart, /reset)
                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str in ["/acp spawn", "/restart_acp", "/reset_acp", "/restart", "/reset"]:
                    print(f"[{addr[0]}] Intercepted raw control command: {line_str}", file=sys.stderr)
                    asyncio.create_task(restart_openclaw_process())
                    try:
                        writer.write(b"OK: Resetting OpenClaw ACP subprocess...\n")
                        await writer.drain()
                    except:
                        pass
                    continue

                # Debug logging
                if is_debug:
                    try:
                        tmp_data = json.loads(line)
                        p = tmp_data.get("params", {})
                        tmp_sid = p.get("sessionId", p.get("session_id", "no-session"))
                        print(f"[{addr[0]}] [Session: {tmp_sid}] Client -> Agent: {line_str}", file=sys.stderr)
                    except:
                        print(f"[{addr[0]}] Client -> Agent: {line_str}", file=sys.stderr)
                
                # Interception Logic for Special Mode
                intercepted = False
                try:
                    data = json.loads(line)
                    method = data.get("method", "")
                    
                    if method in ["prompt", "session/prompt"]:
                        params = data.get("params", {})
                        prompt_list = params.get("prompt", [])
                        session_id = params.get("sessionId", params.get("session_id", "unknown"))
                        request_id = data.get("id")

                        for block in prompt_list:
                            if block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if "/filerequest" in text:
                                    intercepted = True
                                    print(f"[{addr[0]}] [Session: {session_id}] [Special Mode] Intercepted: {text}", file=sys.stderr)
                                    
                                    path_part = text.split("/filerequest")[-1].strip()
                                    full_path = os.path.abspath(os.path.expanduser(path_part))
                                    
                                    if not os.path.exists(full_path):
                                        msg = {
                                            "jsonrpc": "2.0", "method": "session/update", 
                                            "params": {
                                                "sessionId": session_id, 
                                                "update": {
                                                    "sessionUpdate": "agent_message_chunk", 
                                                    "content": {"type": "text", "text": f"\nError: {path_part} not found.\n"}
                                                }
                                            }
                                        }
                                        writer.write(json.dumps(msg).encode() + b"\n")
                                    else:
                                        # Prepare file data
                                        if os.path.isdir(full_path):
                                            buf = io.BytesIO()
                                            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                                                for r, d, fs in os.walk(full_path):
                                                    for f in fs:
                                                        fp = os.path.join(r, f)
                                                        zf.write(fp, os.path.relpath(fp, full_path))
                                            file_bytes = buf.getvalue()
                                            m_type = "application/zip"
                                            fname = os.path.basename(full_path.rstrip("/\\")) + ".zip"
                                            
                                            b64 = base64.b64encode(file_bytes).decode('utf-8')
                                            
                                            # Send file chunk
                                            chunk = {
                                                "jsonrpc": "2.0", "method": "session/update",
                                                "params": {
                                                    "sessionId": session_id,
                                                    "update": {
                                                        "sessionUpdate": "agent_message_chunk",
                                                        "content": {
                                                            "type": "resource",
                                                            "resource": {"blob": b64, "mimeType": m_type, "uri": f"file://{fname}"}
                                                        }
                                                    }
                                                }
                                            }
                                            writer.write(json.dumps(chunk).encode() + b"\n")
                                            await writer.drain()
                                            info_text = f"\n[System]: Sent {fname} ({len(file_bytes)} bytes)\n"
                                        else:
                                            m_type, _ = mimetypes.guess_type(full_path)
                                            m_type = m_type or "application/octet-stream"
                                            fname = os.path.basename(full_path)
                                            file_size = os.path.getsize(full_path)
                                            
                                            if file_size > 10 * 1024 * 1024 and use_http: # > 10MB and HTTP enabled
                                                import uuid
                                                file_id = str(uuid.uuid4())
                                                served_files[file_id] = full_path
                                                # Include filename in URL so client can extract it easily
                                                url = f"http://{local_ip}:{http_port}/{file_id}/{fname}"
                                                
                                                chunk = {
                                                    "jsonrpc": "2.0", "method": "session/update",
                                                    "params": {
                                                        "sessionId": session_id,
                                                        "update": {
                                                            "sessionUpdate": "agent_message_chunk",
                                                            "content": {
                                                                "type": "resource",
                                                                "resource": {"blob": "AAA=", "mimeType": m_type, "uri": url}
                                                            }
                                                        }
                                                    }
                                                }
                                                writer.write(json.dumps(chunk).encode() + b"\n")
                                                await writer.drain()
                                                
                                                info_text = f"\n[System]: Serving large file via HTTP stream: {url} ({file_size} bytes)\n"
                                            else:
                                                # Standard Base64 blob (used if small OR if HTTP is disabled)
                                                with open(full_path, 'rb') as f:
                                                    file_bytes = f.read()
                                                b64 = base64.b64encode(file_bytes).decode('utf-8')
                                                
                                                chunk = {
                                                    "jsonrpc": "2.0", "method": "session/update",
                                                    "params": {
                                                        "sessionId": session_id,
                                                        "update": {
                                                            "sessionUpdate": "agent_message_chunk",
                                                            "content": {
                                                                "type": "resource",
                                                                "resource": {"blob": b64, "mimeType": m_type, "uri": f"file://{fname}"}
                                                            }
                                                        }
                                                    }
                                                }
                                                writer.write(json.dumps(chunk).encode() + b"\n")
                                                await writer.drain()
                                                info_text = f"\n[System]: Sent {fname} ({len(file_bytes)} bytes)\n"
                                        
                                        # Send confirmation text
                                        info = {
                                            "jsonrpc": "2.0", "method": "session/update",
                                            "params": {
                                                "sessionId": session_id,
                                                "update": {
                                                    "sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": info_text}
                                                }
                                            }
                                        }
                                        writer.write(json.dumps(info).encode() + b"\n")
                                        await writer.drain()
                                        print(f"[{addr[0]}] [Session: {session_id}] [Special Mode] Initiated transfer of {fname}.", file=sys.stderr)

                                    # Increased delay for perfect sync with large files
                                    await asyncio.sleep(0.5)
                                    
                                    # End the turn for this request
                                    res = {"jsonrpc": "2.0", "id": request_id, "result": {"stop_reason": "end_turn"}}
                                    writer.write(json.dumps(res).encode() + b"\n")
                                    await writer.drain()
                                    break
                except Exception as e:
                    if is_debug: print(f"Processing error: {e}", file=sys.stderr)

                if intercepted:
                    continue

                # Forward standard request to the agent
                if is_debug and method in ["prompt", "session/prompt"]:
                    print(f"[{addr[0]}] [Session: {session_id}] Forwarding to Agent...", file=sys.stderr)
                
                process.stdin.write(line)
                await process.stdin.drain()

        except Exception as e:
            if is_debug: print(f"Connection error: {e}", file=sys.stderr)
        finally:
            print(f"Client {addr} disconnected", file=sys.stderr)
            if active_writer == writer:
                active_writer = None
            writer.close()

    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()
