"""Serve a verified ROM descriptor, never reopen a mutable library pathname.

Starlette retains Range/If-Range parsing; the multipart envelope uses CRLF.
The descriptor alias is internal to this process; each response owns a separately
opened file so concurrent ranges cannot share an offset. Linux and macOS expose
these aliases through /proc/self/fd and /dev/fd respectively.
"""
from pathlib import Path
import os
import stat
from secrets import token_hex
import weakref

import anyio
from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import FileResponse, PlainTextResponse

from .torrent_distribution import _revision

MAX_RANGE_HEADER = 8192
MAX_RANGES = 32


def open_verified_rom(library: Path, path: Path, revision: str):
    """Walk relative to a pinned library directory, without following links."""
    library, path = Path(library), Path(path)
    if not library.is_absolute() or not path.is_absolute() or '..' in path.parts:
        raise HTTPException(403, '游戏文件不在游戏库内')
    try:
        relative = path.relative_to(library)
    except ValueError as error:
        raise HTTPException(403, '游戏文件不在游戏库内') from error
    if not relative.parts:
        raise HTTPException(403, '游戏入口不是独立文件')
    directory = descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(library, flags)
        for component in relative.parts[:-1]:
            previous = directory
            directory = os.open(component, flags, dir_fd=previous)
            os.close(previous)
        # O_NONBLOCK lets us reject a substituted FIFO rather than hang on open.
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _revision(info) != revision:
            raise HTTPException(409, '游戏文件已变化，请刷新后重新下载')
        return descriptor, info
    except (OSError, HTTPException) as error:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(409, '游戏文件已变化或暂时无法安全读取，请刷新后重试') from error
    finally:
        if directory is not None:
            os.close(directory)


class SafeRomResponse(FileResponse):
    def __init__(self, library: Path, path: Path, revision: str, *, filename: str | None = None):
        descriptor, info = open_verified_rom(library, path, revision)
        try:
            aliases = (Path('/proc/self/fd'), Path('/dev/fd'))
            root = next((candidate for candidate in aliases if candidate.is_dir()), None)
            if root is None:
                raise HTTPException(503, '当前系统不支持安全文件下载')
            alias = root / str(descriptor)
            # macOS stat(/dev/fd/N) describes its descriptor node, not the file.
            probe = os.open(alias, os.O_RDONLY)
            try:
                alias_info = os.fstat(probe)
            finally:
                os.close(probe)
            if (alias_info.st_dev, alias_info.st_ino) != (info.st_dev, info.st_ino):
                raise HTTPException(503, '当前系统不支持安全文件下载')
            super().__init__(alias, filename=filename, stat_result=info,
                             media_type='application/octet-stream',
                             headers={'Cache-Control': 'private, no-store'})
            # Covers both normal/cancelled responses and abandoned response objects.
            self._release_descriptor = weakref.finalize(self, os.close, descriptor)
        except OSError as error:
            os.close(descriptor)
            raise HTTPException(503, '当前系统暂时无法安全读取游戏文件') from error
        except BaseException:
            os.close(descriptor)
            raise

    @property
    def closed(self):
        return not self._release_descriptor.alive

    async def __call__(self, scope, receive, send):
        try:
            if self.closed:
                raise RuntimeError('A ROM response descriptor can only be used once')
            if scope['method'].upper() == 'HEAD':
                # RFC 9110 section 14.2: Range only applies to GET, even when
                # a HEAD request supplies malformed or excessive range fields.
                scope = {**scope, 'headers': [(key, value) for key, value in scope.get('headers', [])
                                             if key.lower() not in {b'range', b'if-range'}]}
            header = Headers(scope=scope).get('range', '')
            if len(header) > MAX_RANGE_HEADER or header.count(',') >= MAX_RANGES:
                return await PlainTextResponse('请求的文件分段过多，请减少 Range 范围', status_code=413,
                                               headers={'Cache-Control': 'private, no-store'})(scope, receive, send)
            # A future Starlette version may delegate path opening to the server;
            # retain local streaming so the descriptor stays alive until EOF.
            if 'http.response.pathsend' in scope.get('extensions', {}):
                scope = {**scope, 'extensions': {key: value for key, value in scope['extensions'].items()
                                               if key != 'http.response.pathsend'}}
            async def standard_errors(message):
                if message['type'] == 'http.response.start' and message['status'] == 416:
                    headers = MutableHeaders(raw=message['headers'])
                    headers['content-range'] = f'bytes */{self.stat_result.st_size}'
                    headers['cache-control'] = 'private, no-store'
                await send(message)
            return await super().__call__(scope, receive, standard_errors)
        finally:
            self._release_descriptor()

    def generate_multipart(self, ranges, boundary, max_size, content_type):
        def header(start, end):
            return (f'--{boundary}\r\nContent-Type: {content_type}\r\n'
                    f'Content-Range: bytes {start}-{end - 1}/{max_size}\r\n\r\n').encode('latin-1')
        closing = f'--{boundary}--\r\n'.encode('ascii')
        length = sum(len(header(start, end)) + end - start + 2 for start, end in ranges) + len(closing)
        return length, header

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        # Keep Starlette's parsed/merged byte ranges and bounded file reads.
        # Only the envelope is ours: header, separator and closing lines use
        # CRLF; raw ROM bytes are never inspected or normalized as text.
        boundary = token_hex(13)
        length, header = self.generate_multipart(ranges, boundary, file_size, self.headers['content-type'])
        self.headers['content-type'] = f'multipart/byteranges; boundary={boundary}'
        self.headers['content-length'] = str(length)
        if 'content-range' in self.headers:
            del self.headers['content-range']
        await send({'type': 'http.response.start', 'status': 206, 'headers': self.raw_headers})
        if send_header_only:
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return
        async with await anyio.open_file(self.path, mode='rb') as file:
            for start, end in ranges:
                await send({'type': 'http.response.body', 'body': header(start, end), 'more_body': True})
                await file.seek(start)
                while start < end:
                    chunk = await file.read(min(self.chunk_size, end - start))
                    if not chunk:
                        raise RuntimeError('ROM was truncated during delivery')
                    start += len(chunk)
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'\r\n', 'more_body': True})
            await send({'type': 'http.response.body', 'body': f'--{boundary}--\r\n'.encode('ascii'), 'more_body': False})
