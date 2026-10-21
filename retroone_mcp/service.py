"""Scope-checked game operations shared by MCP and read-only spectators.

The family application remains a trusted-LAN application. MCP credentials are
independent of its passwordless profile picker and never expose worker URLs.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any
from urllib.parse import urlsplit

from .profiles import AVATARS, PERMISSIONS, PLAYER_ID, OwnedProfiles, policy, validate_credential


class ServiceFailure(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    identifier: str
    digest: str


class Ledger:
    """Persist intent before mutations; uncertain actions never replay after restart."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (actor TEXT, kind TEXT, id TEXT, fingerprint TEXT, result TEXT, created REAL, PRIMARY KEY(actor,kind,id))')
        self.db.commit()

    def begin(self, actor, kind, identifier, arguments):
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = self.db.execute('SELECT fingerprint,result FROM requests WHERE actor=? AND kind=? AND id=?', (actor, kind, identifier)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ServiceFailure('request_reused', 'This request ID was already used with different arguments.')
            if row[1] is None:
                raise ServiceFailure('outcome_unknown', 'The earlier request is pending or was interrupted. Inspect the current session; it will not be replayed.')
            return json.loads(row[1])
        self.db.execute('INSERT INTO requests VALUES (?,?,?,?,NULL,?)', (actor, kind, identifier, fingerprint, time.time()))
        self.db.commit()
        return None

    def finish(self, actor, kind, identifier, result):
        self.db.execute('UPDATE requests SET result=? WHERE actor=? AND kind=? AND id=?', (json.dumps(result), actor, kind, identifier))
        self.db.commit()

    def result(self, actor, kind, identifier):
        row = self.db.execute('SELECT result FROM requests WHERE actor=? AND kind=? AND id=?', (actor, kind, identifier)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def close(self):
        self.db.close()


ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
BUTTONS = {'a', 'b', 'x', 'y', 'lb', 'rb', 'lt', 'rt', 'back', 'start', 'l3', 'r3', 'up', 'down', 'left', 'right'}
AXES = {'lx', 'ly', 'rx', 'ry'}
READ_TOOLS = {'capabilities', 'list_games', 'sessions', 'watch', 'observe', 'profile_status'}


def _id(value, field='identifier'):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ServiceFailure('invalid_argument', f'Invalid {field}.')
    return value


def _session(snapshot):
    raw = snapshot.get('session')
    if not raw:
        return None
    return {k: v for k, v in dict(raw, id=raw.get('id') or raw.get('session_id')).items()
            if k in {'id', 'session_id', 'game_id', 'title', 'platform', 'mode', 'started_at', 'display', 'elapsed_seconds', 'profile_id', 'target_fps'}}


def _game(raw):
    return {k: raw[k] for k in ('id', 'title', 'platform', 'platform_name', 'mode', 'size', 'browser_eligible', 'reason', 'verified_playable') if k in raw}


class Service:
    LEASE_TTL = 90
    WATCH_TTL = 3600
    OBSERVATION_AGE = 30

    def __init__(self, config_path: Path, state_path: Path, runtime=None):
        self.config_path = Path(config_path)
        self.settings = self._read_config()
        self.profiles = dict(self.settings['profiles'])
        if runtime is None:
            from .runtime import RuntimeClient
            runtime = RuntimeClient(self.profiles)
        self.runtime = runtime
        self.ledger = Ledger(Path(state_path))
        self.owned_profiles = OwnedProfiles(self.ledger.db)
        self.creation_lock = asyncio.Lock()
        self.handles: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.observations: dict[str, dict] = {}
        self.sequence: dict[tuple[str, str], int] = {}
        self.locks = {p: asyncio.Lock() for p in self.profiles}
        self.capture_slots = asyncio.Semaphore(3)
        self.call_slots = asyncio.Semaphore(16)
        self.rates: dict[str, list[float]] = {}
        self._refresh_profiles()

    def _read_config(self):
        try:
            if self.config_path.stat().st_size > 256 * 1024:
                raise ValueError('size')
            config = json.loads(self.config_path.read_text())
            policy(config)
            profiles = config['profiles']
            if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 32:
                raise ValueError('profiles')
            for key, value in profiles.items():
                _id(key, 'profile')
                for field in ('api_url', 'selkies_url'):
                    parsed = urlsplit(value[field])
                    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                        raise ValueError('endpoint')
            seen = set()
            digests = set()
            for credential in config.get('credentials', []):
                validate_credential(credential)
                _id(credential['id'])
                digest = credential['token_sha256']
                if credential['id'] in seen or digest in digests or not re.fullmatch('[a-f0-9]{64}', digest):
                    raise ValueError('credential')
                seen.add(credential['id'])
                digests.add(digest)
                slots = credential.get('slots', [1])
                if not isinstance(slots, list) or not all(type(s) is int and 1 <= s <= 4 for s in slots):
                    raise ValueError('slots')
                for field in ('observe_profiles', 'control_profiles', 'launch_profiles', 'stop_profiles'):
                    entries = credential.get(field, [])
                    if not isinstance(entries, list) or not all(p in profiles for p in entries):
                        raise ValueError('scope')
            return config
        except (OSError, KeyError, TypeError, ValueError, ServiceFailure) as error:
            raise ServiceFailure('configuration_unavailable', 'MCP configuration is missing or invalid.') from error

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            return None
        try:
            config = self._read_config()
        except ServiceFailure:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for entry in config.get('credentials', []):
            if hmac.compare_digest(entry['token_sha256'], digest) and entry.get('enabled', True) and entry.get('expires_at', float('inf')) > time.time():
                return Principal(entry['id'], digest)
        return None

    def _credential(self, principal):
        for entry in self._read_config().get('credentials', []):
            if (entry['id'] == principal.identifier and hmac.compare_digest(entry['token_sha256'], principal.digest)
                    and entry.get('enabled', True) and entry.get('expires_at', float('inf')) > time.time()):
                return entry
        raise ServiceFailure('unauthorized', 'This credential is no longer authorized.')

    def _refresh_profiles(self):
        creation = policy(self._read_config())
        if not creation:
            return
        for row in self.owned_profiles.all():
            profile = row['profile_id']
            if not profile or row['gateway'] != creation['api_url']:
                continue
            if profile not in self.settings['profiles']:
                base = creation['api_url'] + '/p/' + profile
                self.profiles[profile] = {'name': row['name'], 'api_url': base, 'selkies_url': base + '/stream',
                                          'control_enabled': creation.get('allow_legacy_input', False),
                                          'allow_legacy_input': creation.get('allow_legacy_input', False)}
            self.locks.setdefault(profile, asyncio.Lock())

    def _owned(self, principal):
        creation = policy(self._read_config())
        if not creation:
            return set()
        return {row['profile_id'] for row in self.owned_profiles.all(principal.identifier)
                if row['profile_id'] and row['gateway'] == creation['api_url']}

    def _allowed(self, principal, permission, credential=None):
        entry = credential or self._credential(principal)
        allowed = set(entry.get(permission + '_profiles', []))
        if permission in entry.get('created_profile_permissions', []):
            owned = self._owned(principal)
            if permission in {'control', 'launch', 'stop'}:
                creation = policy(self._read_config())
                owned = {p for p in owned if (p in self.settings['profiles'] and self.profiles[p].get('control_enabled'))
                         or (creation and creation.get('allow_legacy_input', False))}
            allowed.update(owned)
        return allowed

    def _slots(self, principal, profile, credential):
        # New trusted-LAN workers have one implicit controller. Never advertise
        # other slots without explicitly configured upstream slot credentials.
        slots = credential.get('slots', [1])
        return [s for s in slots if s == 1] if profile in self._owned(principal) and profile not in self.settings['profiles'] else slots

    def _scope(self, principal, profile, permission):
        _id(profile, 'profile')
        entry = self._credential(principal)
        self._refresh_profiles()
        if profile not in self.profiles or profile not in self._allowed(principal, permission, entry):
            raise ServiceFailure('forbidden', 'This credential does not permit this operation for that profile.')
        if permission in {'control', 'launch', 'stop'} and not self.profiles[profile].get('control_enabled', False):
            raise ServiceFailure('control_disabled', 'This profile is configured for observation only.')
        return entry

    def tool_names(self, principal):
        credential = self._credential(principal)
        names = set(READ_TOOLS)
        if self._allowed(principal, 'control', credential):
            names.update({'claim_control', 'act', 'release_control'})
        for operation in ('launch', 'stop'):
            if self._allowed(principal, operation, credential):
                names.add(operation)
        if credential.get('create_profiles', False) and policy(self._read_config()):
            names.add('create_profile')
        return names

    def _cleanup(self):
        now = time.time()
        for mapping in (self.handles, self.leases, self.observations):
            for key in list(mapping):
                if mapping[key]['expires_at'] <= now:
                    del mapping[key]
        self.rates = {k: [t for t in v if t > now - 1] for k, v in self.rates.items() if v and v[-1] > now - 1}

    async def call(self, principal, tool_name, arguments):
        self._cleanup()
        if tool_name not in self.tool_names(principal):
            raise ServiceFailure('forbidden', 'This tool is not available to this credential.')
        recent = self.rates.setdefault(principal.identifier, [])
        if len(recent) >= 30:
            raise ServiceFailure('rate_limited', 'Too many requests; retry after one second.')
        recent.append(time.time())
        method = getattr(self, '_' + tool_name, None)
        if method is None:
            raise ServiceFailure('unknown_tool', 'Unknown game operation.')
        async with self.call_slots:
            try:
                return await method(principal, **arguments)
            except ServiceFailure:
                raise
            except Exception as error:
                from .runtime import RuntimeFailure
                if isinstance(error, RuntimeFailure):
                    raise ServiceFailure(error.code, str(error)) from error
                raise ServiceFailure('operation_failed', 'The game operation failed; inspect server diagnostics.') from error

    async def _current(self, profile, expected):
        active = _session(await self.runtime.status(profile))
        if not active or active['id'] != expected:
            raise ServiceFailure('session_changed', 'The requested game session ended or changed. Select a current session again.')
        return active

    async def _capabilities(self, principal):
        self._refresh_profiles()
        credential = self._credential(principal)
        profiles = []
        allowed = set().union(*(self._allowed(principal, k, credential) for k in PERMISSIONS))
        for key in sorted(allowed):
            try:
                snapshot = await self.runtime.status(key)
                available = snapshot.get('runtime', {}).get('platforms', {})
                runtime = {p: {k: v for k, v in state.items() if k in {'available', 'engine', 'reason'}} for p, state in available.items()}
                error = None
            except Exception:
                runtime, error = {}, 'worker_unavailable'
            profiles.append({'id': key, 'name': self.profiles[key].get('name', key),
                             'permissions': [k for k in PERMISSIONS if key in self._allowed(principal, k, credential)],
                             'owned': key in self._owned(principal),
                             'controller_slots': self._slots(principal, key, credential) if key in self._allowed(principal, 'control', credential) else [],
                             'platforms': runtime, 'error': error})
        records = self.owned_profiles.all(principal.identifier)
        return {'version': '1.1', 'profiles': profiles, 'default_launch_mode': 'stream',
                'profile_creation': {'enabled': 'create_profile' in self.tool_names(principal),
                                     'limit': credential.get('max_created_profiles', 1), 'used': len(records),
                                     'avatars': list(AVATARS),
                                     'requests': [{'request_id': r['request_id'], 'profile_id': r['profile_id'], 'name': r['name']} for r in records]},
                'observation': {'cloud': True, 'browser_publisher': False, 'max_age_seconds': self.OBSERVATION_AGE},
                'control': {'buttons': sorted(BUTTONS), 'axes': sorted(AXES), 'max_duration_ms': 1000, 'lease_seconds': self.LEASE_TTL,
                            'timing': 'real_time', 'frame_stepping': False, 'delivery_ack': False,
                            'disconnect_release_fallback_seconds': 2.1},
                'multiplayer': {'separate_profiles': True, 'same_console': 'requires_game_validation', 'native_network_tools': False}}

    async def _list_games(self, principal, profile_id, q='', platform='', limit=20, offset=0):
        self._scope(principal, profile_id, 'observe')
        result = await self.runtime.games(profile_id, q=q, platform=platform, limit=limit, offset=offset)
        return {'games': [_game(g) for g in result.get('games', [])], 'total': result.get('total', 0), 'offset': offset}

    async def _sessions(self, principal):
        self._refresh_profiles()
        allowed = sorted(self._allowed(principal, 'observe'))
        async def view(profile):
            try:
                snapshot = await self.runtime.status(profile)
                return {'profile_id': profile, 'name': self.profiles[profile].get('name', profile), 'session': _session(snapshot)}
            except Exception:
                return {'profile_id': profile, 'session': None, 'error': 'worker_unavailable'}
        return {'sessions': await asyncio.gather(*(view(p) for p in allowed))}

    @staticmethod
    def _public_profile(record):
        if (not isinstance(record, dict) or not isinstance(record.get('id'), str)
                or not PLAYER_ID.fullmatch(record['id'])
                or record.get('status') not in {'provisioning', 'ready', 'error'}):
            raise ServiceFailure('profile_response_invalid', 'The profile registry returned an invalid result.')
        return {k: record.get(k) for k in ('id', 'name', 'avatar', 'status')} | {
            'error': 'provisioning_failed' if record['status'] == 'error' else None}

    async def _profile_status(self, principal, profile_id):
        self._credential(principal)
        if profile_id not in self._owned(principal):
            raise ServiceFailure('forbidden', 'Only your own created profile preparation status is available.')
        creation = policy(self._read_config())
        snapshot = await self.runtime.list_profiles(creation['api_url'])
        record = next((p for p in snapshot.get('profiles', []) if p.get('id') == profile_id), None)
        if not record:
            raise ServiceFailure('profile_not_found', 'The created profile is no longer present; contact the administrator.')
        self._credential(principal)
        if profile_id not in self._owned(principal):
            raise ServiceFailure('forbidden', 'Profile access has changed.')
        return {'profile': self._public_profile(record), 'owned': True,
                'permissions': [p for p in PERMISSIONS if profile_id in self._allowed(principal, p)]}

    async def _create_profile(self, principal, name, request_id, avatar='rocket'):
        _id(request_id, 'profile request ID')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 20
                or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name) or avatar not in AVATARS):
            raise ServiceFailure('invalid_argument', 'Use a name of 1–20 characters without control characters and a preset avatar.')
        name = name.strip()
        async with self.creation_lock:
            credential = self._credential(principal)
            creation = policy(self._read_config())
            if not credential.get('create_profiles', False) or not creation:
                raise ServiceFailure('forbidden', 'Profile creation is not enabled for this credential.')
            row, replayed = self.owned_profiles.reserve(principal.identifier, request_id, name, avatar,
                                                       creation['api_url'], credential.get('max_created_profiles', 1))
            if not row['profile_id']:
                # The existing REST API deduplicates by UUID. Unlike generic game
                # mutations this is safe to retry after an uncertain POST/restart.
                try:
                    result = self._public_profile(await self.runtime.create_profile(creation['api_url'], name, avatar, row['upstream_id']))
                except Exception as error:
                    if isinstance(error, ServiceFailure):
                        raise
                    raise ServiceFailure('profile_creation_pending', 'Creation is unconfirmed. Retry create_profile with the same request_id and details; do not allocate a new request ID.') from error
                profile = result['id']
                if profile in self.settings['profiles'] or any(r['profile_id'] == profile for r in self.owned_profiles.all()):
                    raise ServiceFailure('profile_conflict', 'The registry returned an existing profile; no ownership was granted.')
                self.owned_profiles.bind(principal.identifier, request_id, profile)
                row['profile_id'] = profile
            self._refresh_profiles()
            self._credential(principal)
            result = await self._profile_status(principal, row['profile_id'])
            return dict(result, request_id=request_id, replayed=replayed,
                        next_step={'provisioning': 'profile_status', 'ready': 'capabilities', 'error': 'contact_administrator'}[result['profile']['status']])

    def _new_handle(self, owner, profile, session, *, family=False, viewer=None):
        if len(self.handles) >= 1024:
            raise ServiceFailure('capacity', 'Too many observation handles; wait for older handles to expire.')
        handle = secrets.token_urlsafe(32)
        value = {'owner': owner, 'profile_id': profile, 'session_id': session['id'], 'title': session.get('title', ''),
                 'expires_at': time.time() + self.WATCH_TTL, 'family': family, 'viewer': viewer}
        self.handles[handle] = value
        return {'handle': handle, 'profile_id': profile, 'session_id': session['id'], 'expires_at': value['expires_at'], 'role': 'observer'}

    async def _watch(self, principal, profile_id, expected_session_id):
        self._scope(principal, profile_id, 'observe')
        active = await self._current(profile_id, expected_session_id)
        return self._new_handle(principal.identifier, profile_id, active)

    def _owned_handle(self, principal, handle):
        grant = self.handles.get(handle)
        if not grant or grant['family'] or grant['owner'] != principal.identifier or grant['expires_at'] <= time.time():
            raise ServiceFailure('invalid_handle', 'The observation handle is expired or belongs to a different observer.')
        self._scope(principal, grant['profile_id'], 'observe')
        return grant

    async def _observe(self, principal, handle, after_action_id=None, max_width=960):
        grant = self._owned_handle(principal, handle)
        profile, session = grant['profile_id'], grant['session_id']
        if after_action_id:
            result = self.ledger.result(principal.identifier, 'act', after_action_id)
            if not result or result.get('_error') or result.get('session_id') != session or result.get('profile_id') != profile:
                raise ServiceFailure('action_not_completed', 'That action has no completed result for this session.')
        async with self.capture_slots, self.locks[profile]:
            await self._current(profile, session)
            captured = time.time()
            data, mime, width, height = await self.runtime.screenshot(profile, max_width)
            await self._current(profile, session)
            self._owned_handle(principal, handle)
            if time.time() - captured > 10:
                raise ServiceFailure('capture_stale', 'The screen capture took too long; request another image.')
            if len(data) > 8 * 1024 * 1024:
                raise ServiceFailure('capture_too_large', 'The screen capture exceeded the response limit.')
            observation = secrets.token_urlsafe(24)
            sequence = self.sequence.get((profile, session), 0)
            if len(self.observations) >= 4096:
                raise ServiceFailure('capacity', 'Too many recent observations; retry shortly.')
            self.observations[observation] = {'owner': principal.identifier, 'profile_id': profile, 'session_id': session,
                                              'sequence': sequence, 'expires_at': captured + self.OBSERVATION_AGE}
            return {'observation_id': observation, 'profile_id': profile, 'session_id': session, 'captured_at': captured,
                    'width': width, 'height': height, 'last_action_sequence': sequence,
                    'age_ms': round((time.time() - captured) * 1000), 'source': 'cloud_display',
                    '_image': {'data': base64.b64encode(data).decode(), 'mimeType': mime}}

    def _replay(self, result):
        if result.get('_error'):
            raise ServiceFailure(result['_error']['code'], result['_error']['message'])
        return dict(result, replayed=True)

    async def _mutation(self, principal, kind, identifier, arguments, callback):
        _id(identifier, kind + ' request ID')
        result = self.ledger.begin(principal.identifier, kind, identifier, arguments)
        if result is not None:
            return self._replay(result)
        try:
            result = await callback()
        except ServiceFailure as error:
            self.ledger.finish(principal.identifier, kind, identifier, {'_error': {'code': error.code, 'message': error.message}})
            raise
        except Exception as error:
            from .runtime import RuntimeFailure
            safe = ServiceFailure(error.code, str(error)) if isinstance(error, RuntimeFailure) else ServiceFailure('operation_failed', 'The operation failed; inspect the current session before retrying.')
            self.ledger.finish(principal.identifier, kind, identifier, {'_error': {'code': safe.code, 'message': safe.message}})
            raise safe from error
        self.ledger.finish(principal.identifier, kind, identifier, result)
        return result

    async def _launch(self, principal, profile_id, game_id, request_id, mode='stream', replace_session_id=None):
        self._scope(principal, profile_id, 'launch')
        self._scope(principal, profile_id, 'observe')
        _id(game_id, 'game')
        if mode not in {'auto', 'stream'}:
            raise ServiceFailure('browser_not_observable', 'AI launches require cloud mode (stream); browser publishers are not available.')
        arguments = dict(profile_id=profile_id, game_id=game_id, mode=mode, replace_session_id=replace_session_id)
        async def operation():
            async with self.locks[profile_id]:
                self._scope(principal, profile_id, 'launch')
                current = _session(await self.runtime.status(profile_id))
                if current and current.get('game_id') != game_id:
                    if replace_session_id != current['id']:
                        raise ServiceFailure('session_busy', 'Another game is running. Explicitly authorize replacement using its session ID.')
                    self._scope(principal, profile_id, 'stop')
                elif replace_session_id and (not current or current['id'] != replace_session_id):
                    raise ServiceFailure('session_changed', 'The session selected for replacement no longer matches.')
                response = await self.runtime.launch(profile_id, game_id, 'stream', replace_session_id)
                active = _session(response) or _session(await self.runtime.status(profile_id))
                if not active:
                    raise ServiceFailure('launch_failed', 'No active game session was reported after launch.')
                return {'mode': 'stream', 'profile_id': profile_id, 'session': active, **self._new_handle(principal.identifier, profile_id, active)}
        return await self._mutation(principal, 'launch', request_id, arguments, operation)

    async def _claim_control(self, principal, profile_id, expected_session_id, slot=1):
        credential = self._scope(principal, profile_id, 'control')
        self._scope(principal, profile_id, 'observe')
        if type(slot) is not int or slot not in range(1, 5):
            raise ServiceFailure('invalid_slot', 'Controller slot must be between 1 and 4.')
        if slot not in self._slots(principal, profile_id, credential):
            raise ServiceFailure('forbidden', 'This credential does not permit that controller slot.')
        configured = self.profiles[profile_id].get('selkies_tokens', {})
        legacy_owned = slot == 1 and profile_id in self._owned(principal) and self.profiles[profile_id].get('allow_legacy_input') is True
        if str(slot) not in configured and not (slot == 1 and self.profiles[profile_id].get('selkies_token')) and not legacy_owned:
            raise ServiceFailure('slot_unavailable', 'This controller slot has no configured runtime credential.')
        async with self.locks[profile_id]:
            await self._current(profile_id, expected_session_id)
            for lease_id, lease in self.leases.items():
                if (lease['profile_id'], lease['session_id'], lease['slot']) == (profile_id, expected_session_id, slot) and lease['expires_at'] > time.time():
                    if lease['owner'] != principal.identifier:
                        raise ServiceFailure('slot_busy', 'This controller slot is held by another actor.')
                    lease['expires_at'] = time.time() + self.LEASE_TTL
                    return {'lease_id': lease_id, **{k: lease[k] for k in ('profile_id', 'session_id', 'slot', 'expires_at')}}
            lease_id = secrets.token_urlsafe(32)
            self.leases[lease_id] = {'owner': principal.identifier, 'profile_id': profile_id, 'session_id': expected_session_id,
                                     'slot': slot, 'expires_at': time.time() + self.LEASE_TTL}
            return {'lease_id': lease_id, **{k: self.leases[lease_id][k] for k in ('profile_id', 'session_id', 'slot', 'expires_at')}}

    def _lease(self, principal, lease_id):
        lease = self.leases.get(lease_id)
        if not lease or lease['owner'] != principal.identifier or lease['expires_at'] <= time.time():
            raise ServiceFailure('invalid_lease', 'The controller lease has expired or belongs to another actor.')
        credential = self._scope(principal, lease['profile_id'], 'control')
        if lease['slot'] not in self._slots(principal, lease['profile_id'], credential):
            raise ServiceFailure('forbidden', 'This controller slot is no longer authorized.')
        return lease

    async def _act(self, principal, lease_id, expected_session_id, action_id, observation_id, buttons=None, axes=None, duration_ms=100):
        lease = self._lease(principal, lease_id)
        profile = lease['profile_id']
        if expected_session_id != lease['session_id']:
            raise ServiceFailure('session_changed', 'The action does not match the leased session.')
        buttons, axes = buttons or [], axes or {}
        if (type(duration_ms) is not int or not 1 <= duration_ms <= 1000 or not isinstance(buttons, list)
                or not all(b in BUTTONS for b in buttons) or len(set(buttons)) != len(buttons)
                or not isinstance(axes, dict) or not all(k in AXES and type(v) in (int, float) and -1 <= v <= 1 for k, v in axes.items())):
            raise ServiceFailure('invalid_action', 'Unsupported controller input or duration.')
        arguments = dict(lease_id=lease_id, expected_session_id=expected_session_id, observation_id=observation_id,
                         buttons=buttons, axes=axes, duration_ms=duration_ms)
        async def operation():
            async with self.locks[profile]:
                current_lease = self._lease(principal, lease_id)
                observation = self.observations.get(observation_id)
                key = (profile, expected_session_id)
                if (not observation or observation['owner'] != principal.identifier or observation['profile_id'] != profile
                        or observation['session_id'] != expected_session_id or observation['expires_at'] <= time.time()
                        or observation['sequence'] != self.sequence.get(key, 0)):
                    raise ServiceFailure('stale_observation', 'Capture a fresh image of this session before acting.')
                await self._current(profile, expected_session_id)
                # Once input might reach the runtime, every prior frame is stale,
                # including when delivery disconnects or this task is cancelled.
                self.sequence[key] = self.sequence.get(key, 0) + 1
                submitted = await self.runtime.perform(profile, current_lease['slot'], buttons, axes, duration_ms, expected_session_id)
                self._scope(principal, profile, 'control')
                await self._current(profile, expected_session_id)
                current_lease['expires_at'] = time.time() + self.LEASE_TTL
                return {'action_id': action_id, 'profile_id': profile, 'session_id': expected_session_id,
                        'sequence': self.sequence[key], 'duration_ms': duration_ms,
                        'delivery': 'submitted', 'game_effect': 'verify_with_next_observation',
                        'completed_at': time.time(), 'neutralization_submitted': bool(submitted.get('neutralization_submitted')),
                        'input_release': 'explicit_neutral_and_disconnect' if submitted.get('neutralization_submitted') else 'disconnect_watchdog'}
        return await self._mutation(principal, 'act', action_id, arguments, operation)

    async def _release_control(self, principal, lease_id):
        lease = self._lease(principal, lease_id)
        async with self.locks[lease['profile_id']]:
            self._lease(principal, lease_id)
            self.leases.pop(lease_id, None)
        # Inputs are finite and released before perform returns, also on socket loss.
        return {'released': True, 'session_stopped': False}

    async def _stop(self, principal, profile_id, expected_session_id, request_id):
        self._scope(principal, profile_id, 'stop')
        arguments = dict(profile_id=profile_id, expected_session_id=expected_session_id)
        async def operation():
            async with self.locks[profile_id]:
                self._scope(principal, profile_id, 'stop')
                await self._current(profile_id, expected_session_id)
                await self.runtime.stop(profile_id, expected_session_id)
                self.handles = {k: v for k, v in self.handles.items() if (v['profile_id'], v['session_id']) != (profile_id, expected_session_id)}
                self.leases = {k: v for k, v in self.leases.items() if (v['profile_id'], v['session_id']) != (profile_id, expected_session_id)}
                return {'stopped': True, 'profile_id': profile_id, 'session_id': expected_session_id}
        return await self._mutation(principal, 'stop', request_id, arguments, operation)

    def _family_profiles(self):
        config = self._read_config()
        family = config.get('family_watch', {})
        if not family.get('enabled', False):
            raise ServiceFailure('spectating_disabled', 'Family spectating is not enabled on this server.')
        self._refresh_profiles()
        allowed = list(dict.fromkeys(p for p in family.get('profiles', []) if p in self.profiles))
        creation = policy(config)
        if creation and creation.get('share_with_family', False):
            allowed.extend(r['profile_id'] for r in self.owned_profiles.all()
                           if r['profile_id'] and r['gateway'] == creation['api_url'] and r['profile_id'] not in allowed)
        return allowed

    async def family_sessions(self):
        self._cleanup()
        async def view(profile):
            try:
                snapshot = await self.runtime.status(profile)
                return {'profile_id': profile, 'name': self.profiles[profile].get('name', profile), 'session': _session(snapshot)}
            except Exception:
                return {'profile_id': profile, 'session': None, 'error': 'worker_unavailable'}
        return {'sessions': await asyncio.gather(*(view(p) for p in self._family_profiles()))}

    async def mint_family_watch(self, viewer_profile_id, target_profile_id, expected_session_id):
        allowed = self._family_profiles()
        if viewer_profile_id not in allowed or target_profile_id not in allowed:
            raise ServiceFailure('forbidden', 'This profile is not enabled for family spectating.')
        active = await self._current(target_profile_id, expected_session_id)
        grant = self._new_handle('family', target_profile_id, active, family=True, viewer=viewer_profile_id)
        return {'watch_url': '/spectate/' + grant['handle'] + '/', 'expires_at': grant['expires_at'], 'session_id': expected_session_id}

    async def resolve_watch(self, grant):
        self._cleanup()
        handle = self.handles.get(grant)
        if not handle or not handle['family'] or handle['profile_id'] not in self._family_profiles():
            raise ServiceFailure('invalid_handle', 'This spectator link is unavailable or expired.')
        active = await self._current(handle['profile_id'], handle['session_id'])
        profile = self.profiles[handle['profile_id']]
        return {'profile_id': handle['profile_id'], 'session_id': handle['session_id'], 'stream_base_url': profile['selkies_url'].rstrip('/') + '/',
                'title': active.get('title', ''), 'profile_name': profile.get('name', handle['profile_id']),
                'expires_at': handle['expires_at'], 'selkies_token': profile.get('selkies_viewer_token')}

    async def close(self):
        await self.runtime.close()
        self.ledger.close()


def create_service_from_env():
    config = Path(os.environ.get('RETROONE_MCP_CONFIG', '/config/mcp.json'))
    state = Path(os.environ.get('RETROONE_MCP_STATE', '/state/requests.sqlite'))
    return Service(config, state)
