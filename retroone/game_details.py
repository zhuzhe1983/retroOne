"""Validated, sourced editorial game facts. No launch config or copyrighted binaries."""
from datetime import date
import ipaddress
import math
from urllib.parse import urlsplit

DETAIL_FIELDS = {'description', 'release_date', 'publisher', 'developer', 'genres', 'players', 'languages',
                 'screenshots', 'official_url', 'popularity', 'sources'}
MEDIA_HOSTS = ('nintendo.com', 'nintendo.net', 'nintendo.co.jp', 'playstation.com',
               'steamstatic.com', 'steamcdn-a.akamaihd.net', 'images.igdb.com', 'screenscraper.fr', 'libretro.com')


def _string(value, limit=512):
    if type(value) is not str or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError('invalid detail text')
    value.encode('utf-8')
    return value.strip()


def public_url(value, *, image=False):
    value = _string(value, 4096)
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host or '.' not in host or parsed.username or parsed.password or
            parsed.port not in (None, 443) or host.endswith(('.local', '.localhost', '.internal')) or '\\' in value):
        raise ValueError('a public HTTPS URL is required')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('IP address URLs are not accepted')
    if image and not any(host == allowed or host.endswith('.' + allowed) for allowed in MEDIA_HOSTS):
        raise ValueError('screenshot host is not approved')
    return value


def validate_details(value):
    if type(value) is not dict or set(value) - DETAIL_FIELDS:
        raise ValueError('invalid game details')
    result = {}
    for key, item in value.items():
        if item is None:
            continue  # Unknown values stay absent, not estimated or fabricated.
        if key == 'description':
            if type(item) is not str or len(item) > 8192 or any(ord(char) < 32 and char not in '\n\r\t' for char in item):
                raise ValueError('invalid description')
            item.encode('utf-8')
            result[key] = item.strip()
        elif key == 'release_date':
            if type(item) is not str or len(item) != 10 or date.fromisoformat(item).isoformat() != item:
                raise ValueError('release date must be YYYY-MM-DD')
            result[key] = item
        elif key in {'publisher', 'developer', 'players'}:
            result[key] = _string(item)
        elif key in {'genres', 'languages', 'screenshots'}:
            if type(item) is not list or len(item) > (32 if key == 'screenshots' else 64):
                raise ValueError('invalid detail list')
            result[key] = list(dict.fromkeys(public_url(entry, image=True) if key == 'screenshots' else _string(entry, 128) for entry in item))
        elif key == 'official_url':
            result[key] = public_url(item)
        elif key == 'sources':
            if type(item) is not list or len(item) > 32:
                raise ValueError('invalid sources')
            result[key] = []
            for entry in item:
                if type(entry) is not dict or set(entry) != {'name', 'url'}:
                    raise ValueError('each source requires name and URL')
                result[key].append({'name': _string(entry['name']), 'url': public_url(entry['url'])})
        elif key == 'popularity':
            if (type(item) is not dict or not {'score', 'source', 'url', 'kind'} <= set(item) or set(item) - {'score', 'source', 'url', 'kind', 'value', 'unit', 'as_of'} or
                    type(item['score']) not in (int, float) or not math.isfinite(item['score']) or not 0 <= item['score'] <= 100):
                raise ValueError('popularity requires a sourced normalized 0–100 score')
            result[key] = {'score': item['score'], 'source': _string(item['source']),
                           'url': public_url(item['url']), 'kind': _string(item['kind'], 128)}
            if 'value' in item:
                if type(item['value']) not in (int, float) or not math.isfinite(item['value']) or item['value'] < 0:
                    raise ValueError('invalid source metric value')
                result[key]['value'] = item['value']
            if 'unit' in item:
                result[key]['unit'] = _string(item['unit'], 128)
            if 'as_of' in item:
                if item['as_of'] is None:
                    raise ValueError('invalid source metric date')
                result[key]['as_of'] = validate_details({'release_date': item['as_of']})['release_date']
    return result
