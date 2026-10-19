"""Render static per-profile routes; no Docker API or runtime container creation."""
import os
from pathlib import Path
import re

profile = os.environ.get('RETROONE_PROFILE_ID', 'player1')
if not re.fullmatch(r'player[1-9][0-9]{0,5}', profile):
    raise SystemExit('Unsupported configured RetroOne profile')
base = f'/p/{profile}'
if os.environ.get('RETROONE_BASE_PATH', base).rstrip('/') != base:
    raise SystemExit('Profile ID and base path must match')
if os.environ.get('SUBFOLDER', base + '/stream/') != base + '/stream/':
    raise SystemExit('Selkies SUBFOLDER must match the profile stream path')

headers = '''    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_http_version 1.1;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;
    proxy_buffering off;
    client_max_body_size 64M;'''
routes = f'''  location = {base} {{ return 302 {base}/; }}
  location {base}/ {{
    proxy_pass http://127.0.0.1:8080/;
{headers}
  }}
  location = {base}/stream {{ return 302 {base}/stream/; }}
'''
if os.environ.get('RETROONE_GATEWAY', 'true').lower() == 'true':
    if profile != 'player1':
        raise SystemExit('Only player1 may be the shared gateway')
    # Only server-generated numeric profile IDs become Docker DNS names. The
    # complete original URI reaches that worker, whose own route strips it.
    routes += f'''  location ~ "^/p/(?<retroone_profile>player(?:[2-9]|[1-9][0-9]{{1,5}}))(?:/|$)" {{
    resolver 127.0.0.11 valid=10s ipv6=off;
    set $retroone_worker http://retroone-$retroone_profile:3000;
    proxy_pass $retroone_worker;
{headers}
  }}
'''
source = Path('/opt/retroone/nginx.template.conf').read_text()
source = source.replace('  RETROONE_ROUTES', routes)
source = source.replace('RETROONE_LEGACY_STREAM', base + '/stream/')
Path('/defaults/default.conf').write_text(source)
