"""Descriptor-bound ROM delivery survives pathname replacement and closes fds."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email import policy
import gc
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_browser_experiments import environment
from retroone import api, safe_rom_response
from retroone.safe_rom_response import SafeRomResponse, MAX_RANGES
from retroone.torrent_distribution import revision


def route_for(game, path, channel):
    return '/api/games/' + game['id'] + '/' + channel + '-rom/' + game['id'] + path.suffix + '?revision=' + revision(path)


@pytest.mark.parametrize('channel', ['download', 'browser'])
@pytest.mark.parametrize('replacement', ['file_symlink', 'parent_symlink', 'different_file'])
def test_replacement_between_api_validation_and_descriptor_open_is_rejected(environment, tmp_path, monkeypatch, channel, replacement):
    build, _, _, _ = environment
    _, client, game, path = build(size=1024)
    route = route_for(game, path, channel)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / path.name
    target.write_bytes(b'private-outside-marker')
    native = api.SafeRomResponse
    def replace_before_open(*args, **kwargs):
        if replacement == 'parent_symlink':
            parent = path.parent
            parent.rename(parent.with_name('old-parent'))
            parent.symlink_to(outside, target_is_directory=True)
        else:
            path.unlink()
            if replacement == 'file_symlink':
                path.symlink_to(target)
            else:
                path.write_bytes(b'different replacement bytes')
        return native(*args, **kwargs)
    monkeypatch.setattr(api, 'SafeRomResponse', replace_before_open)
    result = client.get(route)
    assert result.status_code == 409
    assert b'private-outside-marker' not in result.content
    assert b'different replacement bytes' not in result.content


@pytest.mark.parametrize('channel', ['download', 'browser'])
@pytest.mark.parametrize('replacement', ['file_symlink', 'parent_symlink'])
def test_response_keeps_original_file_after_constructor_even_when_path_is_replaced(environment, tmp_path, monkeypatch, channel, replacement):
    build, _, _, _ = environment
    _, client, game, path = build(size=1024)
    expected = path.read_bytes()
    route = route_for(game, path, channel)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / path.name
    target.write_bytes(b'private-outside-marker')
    native, responses = api.SafeRomResponse, []
    def replace_after_open(*args, **kwargs):
        response = native(*args, **kwargs)
        responses.append(response)
        if replacement == 'parent_symlink':
            parent = path.parent
            parent.rename(parent.with_name('old-parent'))
            parent.symlink_to(outside, target_is_directory=True)
        else:
            path.unlink()
            path.symlink_to(target)
        return response
    monkeypatch.setattr(api, 'SafeRomResponse', replace_after_open)
    result = client.get(route)
    assert result.status_code == 200 and result.content == expected
    assert responses[0].closed


def test_openat_pins_library_root_before_parent_path_is_replaced(environment, tmp_path, monkeypatch):
    build, roms, _, _ = environment
    _, client, game, path = build(size=1024)
    expected = path.read_bytes()
    route = route_for(game, path, 'download')
    library = roms / 'library'
    outside = tmp_path / 'outside'
    target = outside / path.relative_to(library)
    target.parent.mkdir(parents=True)
    target.write_bytes(b'private-outside-marker')
    native_open = os.open
    replaced = False
    def swapped_open(value, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = native_open(value, flags, *args, **kwargs)
        if Path(value) == library and flags & os.O_DIRECTORY and not replaced:
            replaced = True
            library.rename(roms / 'old-library')
            library.symlink_to(outside, target_is_directory=True)
        return descriptor
    monkeypatch.setattr(safe_rom_response.os, 'open', swapped_open)
    response = client.get(route)
    assert replaced and response.status_code == 200 and response.content == expected


def test_parallel_ranges_head_multipart_if_range_and_errors_close_descriptors(environment, monkeypatch):
    build, _, _, _ = environment
    _, client, game, path = build(size=128 * 1024)
    content = path.read_bytes()
    route = route_for(game, path, 'download')
    native, responses = api.SafeRomResponse, []
    def capture(*args, **kwargs):
        result = native(*args, **kwargs)
        responses.append(result)
        return result
    monkeypatch.setattr(api, 'SafeRomResponse', capture)
    ranges = [(0, 7), (3, 31), (65532, 65556), (100000, 100031)] * 4
    def get_range(bounds):
        start, end = bounds
        response = client.get(route, headers={'Range': f'bytes={start}-{end}'})
        assert response.status_code == 206 and response.content == content[start:end + 1]
        assert response.headers['content-range'] == f'bytes {start}-{end}/{len(content)}'
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(get_range, ranges))
    head = client.head(route)
    assert head.status_code == 200 and head.content == b''
    assert int(head.headers['content-length']) == len(content)
    matched = client.get(route, headers={'Range': 'bytes=0-3', 'If-Range': head.headers['etag']})
    assert matched.status_code == 206 and matched.content == content[:4]
    ranged_head = client.head(route, headers={'Range': 'bytes=2-5'})
    assert ranged_head.status_code == 200 and not ranged_head.content
    assert ranged_head.headers['content-length'] == str(len(content)) and 'content-range' not in ranged_head.headers
    for ignored in ('invalid', 'bytes=999999-', 'bytes=' + ','.join(['0-1'] * (MAX_RANGES + 1)), 'x' * 10000):
        ignored_head = client.head(route, headers={'Range': ignored, 'If-Range': '"stale-etag"'})
        assert ignored_head.status_code == 200 and not ignored_head.content
        assert ignored_head.headers['content-length'] == str(len(content))
        assert 'content-range' not in ignored_head.headers
    single = client.get(route, headers={'Range': 'bytes=-7'})
    assert single.status_code == 206 and single.content == content[-7:]
    multipart = client.get(route, headers={'Range': 'bytes=0-3,8-15'})
    assert multipart.status_code == 206 and 'content-range' not in multipart.headers
    assert multipart.headers['content-type'].startswith('multipart/byteranges; boundary=')
    assert int(multipart.headers['content-length']) == len(multipart.content)
    assert b'\n' not in multipart.content.replace(b'\r\n', b'')
    message = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + multipart.headers['content-type'] + '\r\n\r\n').encode() + multipart.content)
    parts = list(message.iter_parts())
    assert [part['content-range'] for part in parts] == [f'bytes 0-3/{len(content)}', f'bytes 8-15/{len(content)}']
    assert [part.get_payload(decode=True) for part in parts] == [content[:4], content[8:16]]
    fallback = client.get(route, headers={'Range': 'bytes=0-3', 'If-Range': '"stale-etag"'})
    assert fallback.status_code == 200 and fallback.content == content
    unsatisfied = client.get(route, headers={'Range': 'bytes=999999-'})
    assert unsatisfied.status_code == 416
    assert unsatisfied.headers['content-range'] == f'bytes */{len(content)}'
    assert unsatisfied.headers['cache-control'] == 'private, no-store'
    assert client.get(route, headers={'Range': 'not-a-range'}).status_code == 400
    assert client.get(route, headers={'Range': 'bytes=' + ','.join(['0-1'] * (MAX_RANGES + 1))}).status_code == 413
    assert all(response.closed for response in responses)


def test_cancelled_and_abandoned_responses_release_descriptor(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    path = library / 'game.rom'
    path.write_bytes(b'content' * 10000)
    response = SafeRomResponse(library, path, revision(path))
    descriptor = int(Path(response.path).name)
    scope = {'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {'http.response.pathsend': {}}}
    async def receive(): return {'type': 'http.disconnect'}
    async def send(message):
        assert message['type'] != 'http.response.pathsend'
        if message['type'] == 'http.response.body':
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(response(scope, receive, send))
    assert response.closed
    with pytest.raises(OSError): os.fstat(descriptor)
    abandoned = SafeRomResponse(library, path, revision(path))
    descriptor = int(Path(abandoned.path).name)
    del abandoned
    gc.collect()
    with pytest.raises(OSError): os.fstat(descriptor)


def test_nonregular_file_and_direct_parent_escape_cannot_open(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    fifo = library / 'pipe.rom'
    os.mkfifo(fifo)
    with pytest.raises(HTTPException) as error:
        SafeRomResponse(library, fifo, '0' * 64)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        SafeRomResponse(library, library / '..' / 'outside.rom', '0' * 64)
    assert error.value.status_code == 403


@pytest.mark.parametrize('channel', ['download', 'browser'])
def test_multipart_crlf_envelope_preserves_raw_lf_and_crlf_in_rom(environment, channel):
    build, _, _, _ = environment
    app, client, game, path = build(size=64)
    content = b'A\nB\r\nC\nD-untouched-\r\nE\nF\r\nG\x00H'
    path.write_bytes(content)
    app.state.catalog.scan()
    route = route_for(game, path, channel)
    # Include bare LF in one part and CRLF plus NUL in another.
    ranges = [(0, 7), (18, len(content))]
    result = client.get(route, headers={'Range': f'bytes=0-6,18-{len(content) - 1}'})
    assert result.status_code == 206
    boundary = result.headers['content-type'].split('boundary=', 1)[1]
    expected = b''.join(
        (f'--{boundary}\r\nContent-Type: application/octet-stream\r\n'
         f'Content-Range: bytes {start}-{end - 1}/{len(content)}\r\n\r\n').encode()
        + content[start:end] + b'\r\n' for start, end in ranges
    ) + f'--{boundary}--\r\n'.encode()
    assert result.content == expected
    assert int(result.headers['content-length']) == len(expected)
    assert 'content-range' not in result.headers
    message = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + result.headers['content-type'] + '\r\n\r\n').encode() + result.content)
    assert [part.get_payload(decode=True) for part in message.iter_parts()] == [content[start:end] for start, end in ranges]
