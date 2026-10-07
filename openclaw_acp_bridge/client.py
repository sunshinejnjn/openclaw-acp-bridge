import asyncio
import os
import re
import base64
import shutil
import httpx
import mimetypes
from uuid import uuid4
from typing import Optional, Callable, Any, Union, List
from acp import PROTOCOL_VERSION, text_block, image_block
from acp.client import ClientSideConnection
from acp.interfaces import Client

def _compress_image_bytes(data_bytes: bytes, max_dim: int = 1280, quality: int = 82) -> tuple[bytes, str]:
    """
    使用 Pillow 将图像有损压缩转码为高质量 JPEG 以节约网络带宽与提速传输。
    若非图片或 PIL 异常，则返回原始数据。
    """
    try:
        import io
        from PIL import Image

        bio_in = io.BytesIO(data_bytes)
        img = Image.open(bio_in)
        
        # 针对带 Alpha 通道（RGBA/LA/P）图片合成到白底避免转为 JPEG 时黑边
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            bands = img.getbands()
            mask = img.split()[-1] if 'A' in bands else None
            bg.paste(img, mask=mask)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # 等比例限制最大宽/高尺寸（默认 1280px，足够大模型辨识特征并极大幅度压缩体积）
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        bio_out = io.BytesIO()
        img.save(bio_out, format='JPEG', quality=quality, optimize=True)
        compressed_bytes = bio_out.getvalue()
        
        # 若压缩后尺寸确实变小或为非JPEG转码成功，则采纳
        if len(compressed_bytes) < len(data_bytes) or not data_bytes.startswith(b'\xff\xd8\xff'):
            return compressed_bytes, "image/jpeg"
    except Exception:
        pass
    return data_bytes, "application/octet-stream"

def _prepare_attachment_blocks(
    attachments: Optional[List[Union[str, dict]]] = None,
    image_data: Optional[str] = None,
    image_mime: Optional[str] = None,
    compress_images: bool = True
) -> list:
    """
    将图片、文档等各类附件文件统一编码为与 ACP 协议兼容的 MIME Block 列表。
    与 server.py 端的 MIME 拦截器紧密协同，自动落盘至 /tmp/acp/incoming/。
    默认会自动对图片附件进行高质量 JPEG 有损压缩（默认 1280px, quality=82），极大节省传输带宽与延迟。
    """
    blocks = []
    items = []

    # 1. 兼容原有直接传递 image_data 的调用形式
    if image_data:
        items.append({
            "data": image_data,
            "mime_type": image_mime or "image/jpeg",
            "uri": "file://input_image.jpg"
        })

    # 2. 支持 attachments 列表（可以传本地文件路径如 'cat.png' 或自定义 dict）
    if attachments:
        for att in attachments:
            if isinstance(att, str):
                if os.path.exists(att):
                    # 本地文件路径：自动读取并转换为带文件名的 Base64 数据块
                    fname = os.path.basename(att)
                    mtype, _ = mimetypes.guess_type(att)
                    mtype = mtype or "application/octet-stream"
                    with open(att, "rb") as f:
                        file_bytes = f.read()

                    # 若开启了图片压缩，针对图片类型执行转码压缩
                    if compress_images and (mtype.startswith("image/") or fname.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp'))):
                        c_bytes, c_mtype = _compress_image_bytes(file_bytes)
                        if c_mtype == "image/jpeg":
                            file_bytes = c_bytes
                            mtype = "image/jpeg"
                            # 更新后缀名
                            base_n, _ = os.path.splitext(fname)
                            fname = f"{base_n}.jpg"

                    b64 = base64.b64encode(file_bytes).decode("utf-8")
                    items.append({
                        "data": b64,
                        "mime_type": mtype,
                        "uri": f"file://{fname}"
                    })
                else:
                    # 纯 Base64 字符串
                    items.append({
                        "data": att,
                        "mime_type": "image/jpeg",
                        "uri": "file://attachment.jpg"
                    })
            elif isinstance(att, dict):
                items.append(att)

    # 3. 逐项组装并修正 MIME Magic Bytes 与 Base64 图片压缩
    for it in items:
        raw_b64 = it.get("data") or it.get("blob") or ""
        if not raw_b64:
            continue
        mtype = it.get("mime_type") or it.get("mimeType") or "application/octet-stream"
        uri = it.get("uri") or None

        # 二进制头部嗅探，精确修正图片 MIME 类型
        try:
            head = base64.b64decode(raw_b64[:64])
            is_image = False
            if head.startswith(b'\xff\xd8\xff'):
                mtype = "image/jpeg"
                is_image = True
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.jpg'
            elif head.startswith(b'\x89PNG\r\n\x1a\n'):
                mtype = "image/png"
                is_image = True
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.png'
            elif head.startswith(b'GIF8'):
                mtype = "image/gif"
                is_image = True
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.gif'
            elif head.startswith(b'RIFF') and len(head) >= 12 and head[8:12] == b'WEBP':
                mtype = "image/webp"
                is_image = True
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.webp'
            elif head.startswith(b'%PDF'):
                mtype = "application/pdf"
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.pdf'
            elif head.startswith(b'PK\x03\x04'):
                mtype = "application/zip"
                if uri and uri.endswith('.bin'): uri = uri[:-4] + '.zip'

            # 如果是纯 Base64 传入的图片且允许压缩，若体积较大或为 PNG 则转码为高压缩比 JPEG
            if compress_images and is_image and len(raw_b64) > 100 * 1024:
                try:
                    full_bytes = base64.b64decode(raw_b64)
                    c_bytes, c_mtype = _compress_image_bytes(full_bytes)
                    if c_mtype == "image/jpeg" and len(c_bytes) < len(full_bytes):
                        raw_b64 = base64.b64encode(c_bytes).decode("utf-8")
                        mtype = "image/jpeg"
                        if uri:
                            base_u, _ = os.path.splitext(uri)
                            uri = f"{base_u}.jpg"
                except Exception:
                    pass
        except Exception:
            pass

        # 构造符合 ACP 标准的多模态数据块
        blocks.append(image_block(data=raw_b64, mime_type=mtype, uri=uri))

    return blocks

def _is_valid_transfer_path(path: str) -> bool:
    """Filter out mock, example, or placeholder file paths to avoid illegal /filerequest calls."""
    if not path or not isinstance(path, str):
        return False
    p = path.strip().lower()
    dummy_keywords = [
        '/full/path/to', '/path/to', 'example.png', 'file.png',
        'placeholder', 'your_image', 'image.png', 'test.png', 'temp.png'
    ]
    if any(k in p for k in dummy_keywords):
        return False
    if p.endswith('/') or len(p) < 4:
        return False
    return True

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

    # -------------------------------------------------------------------------
    # Implement default stubs for Client Protocol to satisfy static analysis
    # -------------------------------------------------------------------------
    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs):
        return None

    async def read_text_file(self, session_id: str, path: str, line: Optional[int] = None, limit: Optional[int] = None, **kwargs):
        raise NotImplementedError("read_text_file is not supported by bridge client")

    async def create_terminal(self, session_id: str, command: str, args=None, env=None, cwd=None, output_byte_limit=None, **kwargs):
        raise NotImplementedError("create_terminal is not supported by bridge client")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs):
        raise NotImplementedError("terminal_output is not supported by bridge client")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs):
        return None

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs):
        raise NotImplementedError("wait_for_terminal_exit is not supported by bridge client")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs):
        return None

    async def create_elicitation(self, message: str, mode: Any, **kwargs):
        raise NotImplementedError("create_elicitation is not supported by bridge client")

    async def complete_elicitation(self, elicitation_id: str, **kwargs):
        return None

    async def ext_method(self, method: str, params: dict):
        return {}

    async def ext_notification(self, method: str, params: dict):
        return None

    def on_connect(self, conn: Any):
        pass

class OpenClaw:
    """
    A high-level client for interacting with OpenClaw via the ACP TCP Bridge.
    
    Usage:
        async with OpenClaw(host="192.168.7.7", token="...") as client:
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

    async def chat(
        self,
        message: str,
        attachments: Optional[List[Union[str, dict]]] = None,
        image_data: Optional[str] = None,
        image_mime: str = "image/png"
    ) -> ChatResponse:
        """
        Sends a message to OpenClaw and returns a ChatResponse object.
        Supports sending attachments (file paths, base64 strings, or attachment dicts) alongside message.
        The response object can be printed as a string, but also contains a .files list.
        """
        if not self._conn or not self.session:
            raise RuntimeError("Client is not connected. Call connect() or use 'async with'.")
            
        self._internal_client.current_response_chunks = []
        self._internal_client.received_files = []
        
        blocks = _prepare_attachment_blocks(attachments=attachments, image_data=image_data, image_mime=image_mime)
        blocks.append(text_block(message))

        # Sends the prompt. The library blocks here until the 'turn' is finished.
        await self._conn.prompt(
            session_id=self.session.session_id,
            prompt=blocks,
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
        
        # Combine, filter valid paths, and deduplicate
        all_paths_to_request = [p for p in set(explicit_paths + file_urls) if _is_valid_transfer_path(p)]
        
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

    async def chat_stream(
        self,
        message: str,
        image_data: Optional[str] = None,
        image_mime: str = "image/png",
        attachments: Optional[List[Union[str, dict]]] = None
    ):
        """
        An async generator that yields chunks of text as they arrive from OpenClaw.
        Supports sending attachments (file paths, base64 strings, or dicts) and legacy image_data.
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
            
            # Robust attribute helper
            def get_attr(obj, name, default=None):
                if isinstance(obj, dict):
                    val = obj.get(name)
                    if val is not None: return val
                    alt_name = name.replace("_", "") if "_" in name else name
                    for k, v in obj.items():
                        if k.lower() == name.lower() or k.lower() == alt_name.lower():
                            return v
                    return default
                return getattr(obj, name, default)

            # Extract text chunk
            update_type = get_attr(update, 'session_update', get_attr(update, 'sessionUpdate'))
            if update_type == 'agent_message_chunk':
                content = get_attr(update, 'content')
                if content and get_attr(content, 'type') == 'text':
                    text = get_attr(content, 'text')
                    if text: await queue.put(text)
        
        self._internal_client.on_update = streaming_callback
        
        blocks = _prepare_attachment_blocks(attachments=attachments, image_data=image_data, image_mime=image_mime)
        blocks.append(text_block(message))
        
        # Start the prompt in a task
        prompt_task = asyncio.create_task(self._conn.prompt(
            session_id=self.session.session_id,
            prompt=blocks,
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
        all_paths_to_request = [p for p in set(explicit_paths + file_urls) if _is_valid_transfer_path(p)]
        
        for path in all_paths_to_request:
            self._internal_client.received_files = []
            await self._conn.prompt(
                session_id=self.session.session_id,
                prompt=[text_block(f"/filerequest {path}")],
                message_id=str(uuid4())
            )
            if self._internal_client.active_tasks:
                await asyncio.gather(*self._internal_client.active_tasks, return_exceptions=True)
                self._internal_client.active_tasks = []
            full_files.extend(self._internal_client.received_files)

        # Final check for any lingering background tasks
        if self._internal_client.active_tasks:
            await asyncio.gather(*self._internal_client.active_tasks, return_exceptions=True)
            self._internal_client.active_tasks = []
            
        # Re-sync files after any final downloads
        full_files = list(set(full_files + self._internal_client.received_files))
            
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
