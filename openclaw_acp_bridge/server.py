import asyncio
import sys
import json
import os
import base64
import io
import zipfile
import mimetypes
import tempfile
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

async def run_server(host="0.0.0.0", port=18781, is_debug=False, token=None, openclaw_path="openclaw", use_http=True, session=None, reset_session=False, agent="main"):
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
        
        cmd = f"{openclaw_path} acp"
        if session:
            cmd += f" --session {session}"
        if reset_session:
            cmd += " --reset-session"

        process = await asyncio.create_subprocess_exec(
            "bash", "-i", "-c", cmd,
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
                    log_str = line_str.strip()
                    if len(log_str) > 500:
                        log_str = log_str[:500] + "... (truncated)"
                    print(f"Agent -> Clients: {log_str}", file=sys.stderr)
                
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
    cmd = f"{openclaw_path} acp"
    if session:
        cmd += f" --session {session}"
    if reset_session:
        cmd += " --reset-session"

    process = await asyncio.create_subprocess_exec(
        "bash", "-i", "-c", cmd,
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

        if process.returncode is not None:
            print("⚠️ Client connected but backing OpenClaw process is dead. Restarting process...", file=sys.stderr)
            await restart_openclaw_process()

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
                    log_str = line_str.strip()
                    if len(log_str) > 500:
                        log_str = log_str[:500] + "... (truncated)"
                    try:
                        tmp_data = json.loads(line)
                        p = tmp_data.get("params", {})
                        tmp_sid = p.get("sessionId", p.get("session_id", "no-session"))
                        print(f"[{addr[0]}] [Session: {tmp_sid}] Client -> Agent: {log_str}", file=sys.stderr)
                    except:
                        print(f"[{addr[0]}] Client -> Agent: {log_str}", file=sys.stderr)
                
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

                # Check if prompt contains MIME/media blocks (image, resource, audio) to land on disk
                try:
                    if method in ["prompt", "session/prompt"]:
                        params = data.get("params", {})
                        prompt_list = params.get("prompt", [])
                        session_id = params.get("sessionId", params.get("session_id", "unknown"))

                        mime_blocks = []
                        text_blocks = []
                        for b in prompt_list:
                            b_type = b.get("type", "")
                            if b_type in ["image", "resource", "audio"] or b.get("data") or (b.get("resource") or {}).get("blob") or b.get("blob"):
                                mime_blocks.append(b)
                            else:
                                text_blocks.append(b)

                        if mime_blocks:
                            # 确定系统临时目录下的 /acp/incoming 子目录
                            incoming_dir = os.path.join(tempfile.gettempdir(), "acp", "incoming")
                            os.makedirs(incoming_dir, exist_ok=True)

                            saved_file_paths = []
                            for idx, mb in enumerate(mime_blocks, 1):
                                raw_b64 = mb.get("data") or (mb.get("resource") or {}).get("blob") or mb.get("blob") or ""
                                if not raw_b64:
                                    continue

                                mtype = mb.get("mime_type") or mb.get("mimeType") or (mb.get("resource") or {}).get("mimeType") or "application/octet-stream"
                                uri = mb.get("uri") or (mb.get("resource") or {}).get("uri") or ""

                                orig_name = os.path.basename(uri.replace("file://", "")) if uri else ""
                                if not orig_name:
                                    # 根据 MIME 类型猜测文件扩展名
                                    ext = mimetypes.guess_extension(mtype) or ".bin"
                                    if ext == ".jpe":
                                        ext = ".jpg"
                                    orig_name = f"incoming_{idx}{ext}"

                                name_base, ext = os.path.splitext(orig_name)
                                target_path = os.path.join(incoming_dir, orig_name)
                                counter = 1
                                # 避免重名：若存在重名文件，则在文件名后追加递增后缀
                                while os.path.exists(target_path):
                                    target_path = os.path.join(incoming_dir, f"{name_base}_{counter}{ext}")
                                    counter += 1

                                try:
                                    file_bytes = base64.b64decode(raw_b64)
                                    with open(target_path, "wb") as f_out:
                                        f_out.write(file_bytes)
                                    saved_file_paths.append(target_path)
                                    print(f"[{addr[0]}] [Session: {session_id}] MIME 文件已落盘: {target_path} ({len(file_bytes)} bytes)", file=sys.stderr)
                                except Exception as e_save:
                                    print(f"[{addr[0]}] [Session: {session_id}] MIME 文件落盘失败: {e_save}", file=sys.stderr)

                            if saved_file_paths:
                                # 构造文件绝对路径声明，形如：本消息包括文件1：xxxx.jpg ；文件2：xxxx.zip 。
                                file_desc_parts = [f"文件{i}：{p}" for i, p in enumerate(saved_file_paths, 1)]
                                prefix_header = f"本消息包括{' ；'.join(file_desc_parts)} 。\n\n"

                                # 将描述前缀放在最前面，合并所有文本
                                combined_text = prefix_header
                                for tb in text_blocks:
                                    if tb.get("type") == "text":
                                        combined_text += tb.get("text", "")

                                # 去掉 MIME 部分，最终发给 Agent 的 ACP 消息只包括纯文本部分
                                new_prompt_list = [{"type": "text", "text": combined_text}]
                                params["prompt"] = new_prompt_list
                                data["params"] = params

                                line = json.dumps(data).encode("utf-8") + b"\n"
                                print(f"[{addr[0]}] [Session: {session_id}] 已转换 MIME 块为纯文本路径说明前缀并转发 OpenClaw", file=sys.stderr)
                except Exception as e_mime:
                    if is_debug: print(f"MIME landing processing error: {e_mime}", file=sys.stderr)

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

    server = await asyncio.start_server(handle_client, host, port, limit=16*1024*1024)
    async with server:
        await server.serve_forever()
