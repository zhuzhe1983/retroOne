"""Durable ownership of profiles allocated through the existing family API.

The upstream request UUID is persisted before HTTP and reused after a timeout or
restart. Neither player IDs, worker URLs nor permissions come from tool input.
"""
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit


AVATARS = ('pixel', 'leaf', 'star', 'rocket', 'cat')
PERMISSIONS = ('observe', 'control', 'launch', 'stop')
PLAYER_ID = re.compile(r'player[1-9][0-9]{0,5}\Z')


def policy(config):
    value = config.get('profile_creation', {})
    if not isinstance(value, dict):
        raise ValueError('profile_creation')
    for key in ('enabled', 'allow_legacy_input', 'share_with_family'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('profile_creation flag')
    if not value.get('enabled', False):
        return None
    endpoint = value.get('api_url', '')
    if not isinstance(endpoint, str):
        raise ValueError('profile_creation endpoint')
    base = endpoint.rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.path
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('profile_creation endpoint must be a gateway origin')
    return dict(value, api_url=base)


def validate_credential(credential):
    if type(credential.get('create_profiles', False)) is not bool:
        raise ValueError('create_profiles')
    limit = credential.get('max_created_profiles', 1)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('max_created_profiles')
    grants = credential.get('created_profile_permissions', [])
    if not isinstance(grants, list) or any(p not in PERMISSIONS for p in grants):
        raise ValueError('created_profile_permissions')


class OwnedProfiles:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS created_profiles (
            actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            upstream_id TEXT NOT NULL UNIQUE, gateway TEXT NOT NULL, name TEXT NOT NULL,
            avatar TEXT NOT NULL, profile_id TEXT UNIQUE, created REAL NOT NULL,
            PRIMARY KEY(actor, request_id))''')
        db.commit()

    def all(self, actor=None):
        sql = 'SELECT actor,request_id,upstream_id,gateway,name,avatar,profile_id FROM created_profiles'
        cursor = self.db.execute(sql + (' WHERE actor=?' if actor else ''), (actor,) if actor else ())
        keys = [item[0] for item in cursor.description]
        return [dict(zip(keys, row)) for row in cursor.fetchall()]

    def reserve(self, actor, request_id, name, avatar, gateway, limit):
        # Called under the service's creation lock; pending requests count too.
        from .service import ServiceFailure
        fingerprint = hashlib.sha256(json.dumps([name, avatar, gateway]).encode()).hexdigest()
        previous = self.db.execute('SELECT fingerprint FROM created_profiles WHERE actor=? AND request_id=?',
                                   (actor, request_id)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise ServiceFailure('request_reused', 'This request ID was used with different profile details.')
        else:
            if len(self.all(actor)) >= limit:
                raise ServiceFailure('profile_quota', 'The Agent profile limit is reached, including pending creations. Reuse your own profile or retry the same pending request ID.')
            self.db.execute('INSERT INTO created_profiles VALUES (?,?,?,?,?,?,?,NULL,?)',
                            (actor, request_id, fingerprint, str(uuid.uuid4()), gateway, name, avatar, time.time()))
            self.db.commit()
        record = next(row for row in self.all(actor) if row['request_id'] == request_id)
        return record, bool(previous)

    def bind(self, actor, request_id, profile_id):
        self.db.execute('UPDATE created_profiles SET profile_id=? WHERE actor=? AND request_id=?',
                        (profile_id, actor, request_id))
        self.db.commit()
