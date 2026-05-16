import asyncio
import os
import re
import base64
import shutil
import httpx
from uuid import uuid4
from typing import Optional, Callable, Any
from acp import PROTOCOL_VERSION, text_block
from acp.client import ClientSideConnection
from acp.interfaces import Client

class ChatResponse:
    """A structured response from OpenClaw, containing text and any received files."""
    def __init__(self, text: str, files: list[str]):
        self.text = text
        self.files = files

    def __str__(self):
        return self.text

    def __repr__(self):
        return self.text

class _InternalACPClient(Client):
    """Internal handler for ACP session updates."""
    def __init__(self, on_update: Optional[Callable[[str, Any], Any]] = None, download_dir: str = "downloads"):
        self.on_update = on_update
        self.download_dir = download_dir
        self.current_response_chunks = []
        self.received_files = []
        self.active_tasks = [] # Track background downloads
        
        # Ensure download directory exists
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

    async def _download_stream(self, uri: str, save_path: str, filename: str):
        try:
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                async with http_client.stream("GET", uri) as r:
                    with open(save_path, "wb") as f:
                        chunk_count = 0
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
                            chunk_count += len(chunk)
                            if chunk_count > 0 and chunk_count % (10 * 1024 * 1024) == 0:
                                print(f"--- [Download Progress: {chunk_count // (1024*1024)}MB] ---")
            
            self.received_files.append(save_path)
            self.current_response_chunks.append(f"\n[File Received: {save_path}]\n")
        except Exception as e:
            self.current_response_chunks.append(f"\n[Stream Error: {e}]\n")


    async def request_permission(self, options, session_id, tool_call, **kwargs):
        return {"outcome": {"outcome": "approved"}}

    async def session_update(self, session_id, update, **kwargs):
        # Forward to custom callback if provided
        if self.on_update:
            if asyncio.iscoroutinefunction(self.on_update):
                await self.on_update(session_id, update)
            else:
                self.on_update(session_id, update)
        
        # Helper to get attributes from either an object or a dict
        def get_attr(obj, name, default=None):
            if isinstance(obj, dict):
                val = obj.get(name)
                if val is not None: return val
                # Try alternate case
                alt_name = name.replace("_", "") if "_" in name else name
                for k, v in obj.items():
                    if k.lower() == name.lower() or k.lower() == alt_name.lower():
                        return v
                return default
            return getattr(obj, name, default)

        # Robust type detection
        update_type = get_attr(update, 'session_update', get_attr(update, 'sessionUpdate'))
        
        if update_type == 'agent_message_chunk':
            content = get_attr(update, 'content')
            if content:
                content_type = get_attr(content, 'type')
                
                if content_type == 'text':
                    text = get_attr(content, 'text')
                    if text: self.current_response_chunks.append(text)
                elif content_type == 'resource':
                    # Handle file/blob resources
                    res_info = get_attr(content, 'resource')
                    if res_info:
                        uri = get_attr(res_info, 'uri', get_attr(res_info, 'URI', 'unknown_file'))
                        blob = get_attr(res_info, 'blob')
                        
                        # Extract filename from URI
                        filename = os.path.basename(uri.replace("file://", ""))
                        save_path = os.path.join(self.download_dir, filename)

                        # Priority 1: High-speed HTTP Side-Channel (for large files)
                        if uri and str(uri).startswith("http"):
                            task = asyncio.create_task(self._download_stream(uri, save_path, filename))
                            self.active_tasks.append(task)
                        # Priority 2: Standard Base64 Blob (for small files)
                        elif blob and blob != "AAA=":
                            with open(save_path, "wb") as f:
                                f.write(base64.b64decode(blob))
                            self.received_files.append(save_path)
                            self.current_response_chunks.append(f"\n[File Saved: {save_path}]\n")
                        else:
                            self.current_response_chunks.append(f"\n[File Received: {uri}]\n")
                elif content_type == 'image':
                    # Handle direct image data
                    data = get_attr(content, 'data')
                    uri = get_attr(content, 'uri')
                    
                    if data:
                        mime_type = get_attr(content, 'mimeType', 'image/png')
                        ext = mime_type.split('/')[-1]
                        uri = uri or f"image_{uuid4().hex[:8]}.{ext}"
                        filename = os.path.basename(uri)
                        save_path = os.path.join(self.download_dir, filename)
                        
                        with open(save_path, "wb") as f:
                            f.write(base64.b64decode(data))
                        
                        self.received_files.append(save_path)
                        self.current_response_chunks.append(f"\n[Image Saved: {save_path}]\n")
                    elif uri and uri.startswith("http"):
                        # Handle images served via HTTP as a background task
                        task = asyncio.create_task(self._download_stream(uri, os.path.join(self.download_dir, os.path.basename(uri)), os.path.basename(uri)))
                        self.active_tasks.append(task)

class OpenClaw:
    """
    A high-level client for interacting with OpenClaw via the ACP TCP Bridge.
    
    Usage:
        async with OpenClaw(host="10.71.253.132", token="...") as client:
            response = await client.chat("Hello!")
            print(response)
    """
    def __init__(self, host: str, port: int = 18781, token: Optional[str] = None, download_dir: str = "downloads"):
        self.host = host
        self.port = port
        self.token = token
        self.download_dir = download_dir
        self._conn = None
        self._writer = None
        self._reader = None
        self.session = None
        self._internal_client = None

    async def connect(self, on_update: Optional[Callable[[str, Any], Any]] = None):
        """Connects to the remote OpenClaw server and initializes a session."""
        # Increase limit to 10MB to handle large file transfers
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port, limit=10*1024*1024)
        
        # Handle token authentication
        auth_token = self.token
        if not auth_token:
            try:
                with open("token.txt", "r") as f:
                    auth_token = f.read().strip()
            except FileNotFoundError:
                pass

        if auth_token:
            self._writer.write(auth_token.encode('utf-8') + b'\n')
            await self._writer.drain()
        
        # Setup internal ACP infrastructure
        self._internal_client = _InternalACPClient(on_update=on_update, download_dir=self.download_dir)
        self._conn = ClientSideConnection(self._internal_client, self._writer, self._reader)
        
        # Initialize ACP protocol
        await self._conn.initialize(protocol_version=PROTOCOL_VERSION)
        self.session = await self._conn.new_session(cwd="/", mcp_servers=[])
        
        return self

    async def chat(self, message: str) -> ChatResponse:
        """
        Sends a message to OpenClaw and returns a ChatResponse object.
        The response object can be printed as a string, but also contains a .files list.
        """
        if not self._conn or not self.session:
            raise RuntimeError("Client is not connected. Call connect() or use 'async with'.")
            
        self._internal_client.current_response_chunks = []
        self._internal_client.received_files = []
        
        # Sends the prompt. The library blocks here until the 'turn' is finished.
        await self._conn.prompt(
            session_id=self.session.session_id,
            prompt=[text_block(message)],
            message_id=str(uuid4())
        )
        
        # Wait for any background downloads to finish
        if self._internal_client.active_tasks:
            await asyncio.gather(*self._internal_client.active_tasks)
            self._internal_client.active_tasks = []

        full_text = "".join(self._internal_client.current_response_chunks)
        full_files = list(self._internal_client.received_files)
        
        # New: Auto-request files marked with [FILEPATH: ...] or file:/// URLs
        # Catch [FILEPATH: ...], [📎 ...](file:///...), or just file:///...
        explicit_paths = re.findall(r'\[FILEPATH:\s*<?([^\]>]+)>?\]', full_text)
        file_urls = re.findall(r'file:///([^\s\)\n\r]+)', full_text)
        
        # Combine and deduplicate
        all_paths_to_request = list(set(explicit_paths + file_urls))
        
        for path in all_paths_to_request:
            # Ensure we have the full path if it's a relative-looking one from the AI
            # But usually they are absolute paths from the remote system
            # We call the underlying logic to fetch the file without resetting the whole session state
            self._internal_client.current_response_chunks = []
            self._internal_client.received_files = []
            
            await self._conn.prompt(
                session_id=self.session.session_id,
                prompt=[text_block(f"/filerequest {path}")],
                message_id=str(uuid4())
            )
            
            # Wait for the streaming download
            if self._internal_client.active_tasks:
                await asyncio.gather(*self._internal_client.active_tasks)
                self._internal_client.active_tasks = []
                
            full_files.extend(self._internal_client.received_files)
            # Note: We don't necessarily need to append the "/filerequest" confirmation text to full_text
            
        return ChatResponse(text=full_text, files=full_files)

    async def chat_stream(self, message: str):
        """
        An async generator that yields chunks of text as they arrive from OpenClaw.
        Yields strings. The final yield will be a ChatResponse object containing the full history and files.
        """
        if not self._conn or not self.session:
            raise RuntimeError("Client is not connected. Call connect() or use 'async with'.")
            
        self._internal_client.current_response_chunks = []
        self._internal_client.received_files = []
        
        queue = asyncio.Queue()
        
        # We wrap the existing on_update to also feed our queue
        original_on_update = self._internal_client.on_update
        
        async def streaming_callback(session_id, update):
            if original_on_update:
                if asyncio.iscoroutinefunction(original_on_update):
                    await original_on_update(session_id, update)
                else:
                    original_on_update(session_id, update)
            
            # Extract text chunk
            update_type = getattr(update, 'session_update', getattr(update, 'sessionUpdate', None))
            if update_type == 'agent_message_chunk':
                content = getattr(update, 'content', None)
                if content and getattr(content, 'type', None) == 'text':
                    text = getattr(content, 'text', None)
                    if text: await queue.put(text)
        
        self._internal_client.on_update = streaming_callback
        
        # Start the prompt in a task
        prompt_task = asyncio.create_task(self._conn.prompt(
            session_id=self.session.session_id,
            prompt=[text_block(message)],
            message_id=str(uuid4())
        ))
        
        # Yield from queue until prompt_task is done
        while not prompt_task.done() or not queue.empty():
            try:
                # Use wait_for to check prompt_task status periodically
                chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
                yield chunk
            except asyncio.TimeoutError:
                continue
        
        await prompt_task # Ensure exceptions are raised
        
        # Wait for any background downloads
        if self._internal_client.active_tasks:
            await asyncio.gather(*self._internal_client.active_tasks)
            self._internal_client.active_tasks = []
            
        # Restore callback
        self._internal_client.on_update = original_on_update
        
        # Handle auto-requests (these are non-streaming for now but we yield the result)
        full_text = "".join(self._internal_client.current_response_chunks)
        full_files = list(self._internal_client.received_files)
        
        explicit_paths = re.findall(r'\[FILEPATH:\s*<?([^\]>]+)>?\]', full_text)
        file_urls = re.findall(r'file:///([^\s\)\n\r]+)', full_text)
        all_paths_to_request = list(set(explicit_paths + file_urls))
        
        for path in all_paths_to_request:
            self._internal_client.received_files = []
            await self._conn.prompt(
                session_id=self.session.session_id,
                prompt=[text_block(f"/filerequest {path}")],
                message_id=str(uuid4())
            )
            if self._internal_client.active_tasks:
                await asyncio.gather(*self._internal_client.active_tasks)
                self._internal_client.active_tasks = []
            full_files.extend(self._internal_client.received_files)
            
        yield ChatResponse(text=full_text, files=full_files)

    async def close(self):
        """Gracefully closes the connection."""
        if self._conn:
            await self._conn.close()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except:
                pass

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
