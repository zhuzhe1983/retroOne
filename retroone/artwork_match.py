"""Conservative, auditable cover matching. No fuzzy or translated-title guesses."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path, PurePosixPath
import re
import unicodedata

REGIONS = {
    'usa': 'usa', 'us': 'usa', 'u': 'usa', 'europe': 'europe', 'eu': 'europe', 'e': 'europe',
    'japan': 'japan', 'jp': 'japan', 'j': 'japan', 'world': 'world', 'asia': 'asia',
    'china': 'china', 'taiwan': 'taiwan', 'hong kong': 'hong kong', 'korea': 'korea',
    'australia': 'australia', 'brazil': 'brazil', 'canada': 'canada', 'france': 'france',
    'germany': 'germany', 'spain': 'spain', 'italy': 'italy', 'sweden': 'sweden',
    'netherlands': 'netherlands', 'united kingdom': 'uk', 'uk': 'uk', 'russia': 'russia',
}
LANGUAGES = {'en', 'ja', 'fr', 'de', 'es', 'it', 'nl', 'sv', 'no', 'da', 'fi', 'pt', 'ru', 'zh', 'ko', 'pl', 'cs', 'hu', 'tr', 'el'}
TAGS = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]')


def exact_name(value: str) -> str:
    return unicodedata.normalize('NFC', value).casefold().strip()


def normalized_title(value: str) -> dict:
    removed, regions = [], set()
    def tag(match):
        content = exact_name(match.group(1) or match.group(2))
        pieces = [part.strip() for part in content.split(',')]
        kind = None
        if pieces and all(part in REGIONS for part in pieces):
            regions.update(REGIONS[part] for part in pieces); kind = 'region'
        elif pieces and all(part in LANGUAGES for part in pieces):
            kind = 'language'
        elif re.fullmatch(r'rev(?:ision)?\s+(?:[a-z]|[0-9]+[a-z0-9.\-]*)|v(?:er(?:sion)?)?\s*[0-9]+[a-z0-9.\-]*', content):
            kind = 'revision'
        elif content in {'汉化', '汉化版', '中文', '中文版', '简体中文', '繁体中文', '中文汉化版'} or re.fullmatch(r't[+-](?:chi|chs|cht|chinese|zh)[a-z0-9 ._\-]*', content):
            kind = 'translation'
        if kind:
            removed.append({'tag': match.group(0), 'kind': kind}); return ' '
        return match.group(0)
    title = re.sub(r'\s+', ' ', TAGS.sub(tag, unicodedata.normalize('NFC', value))).strip()
    return {'title': exact_name(title), 'regions': sorted(regions), 'removed': removed}


class NameIndex:
    """All candidates must already belong to ONE platform. Preserve unknown tags."""
    def __init__(self, names):
        self.exact = defaultdict(list)
        self.normalized = defaultdict(list)
        self.details = {}
        for name in sorted(set(names)):
            self.exact[exact_name(name)].append(name)
            detail = self.details[name] = normalized_title(name)
            if detail['title']:
                self.normalized[detail['title']].append(name)

    def match(self, names, normalized=True):
        for name in names:
            matches = self.exact.get(exact_name(name), [])
            if matches:
                return {'matched_name': matches[0], 'original_name': name, 'normalized_name': exact_name(name), 'method': 'exact-name', 'removed_tags': []}
        if not normalized:
            return None
        for name in names:
            detail = normalized_title(name)
            # Avoid generic resource names/numbers masquerading as game titles.
            if len(detail['title']) < 3 or detail['title'].isdecimal() or detail['title'] in {'game', 'boot', 'eboot', 'default', 'start'}:
                continue
            candidates = self.normalized.get(detail['title'], [])
            if not candidates:
                continue
            regions = set(detail['regions'])
            def score(candidate):
                other = self.details[candidate]
                same_region = bool(regions & set(other['regions']))
                return (0 if same_region else 1 if not other['regions'] else 2, len(other['removed']), candidate)
            selected = min(candidates, key=score)
            return {'matched_name': selected, 'original_name': name, 'normalized_name': detail['title'],
                    'method': 'same-title-known-tags', 'removed_tags': detail['removed'],
                    'source_removed_tags': self.details[selected]['removed'], 'same_region': bool(regions & set(self.details[selected]['regions']))}
        return None


def generic_artwork_name(value: str) -> bool:
    name = exact_name(value)
    return name.isdecimal() or name in {'game', 'boot', 'eboot', 'default', 'start'}


def artwork_collection(layout, path, record):
    """Honor declared locations, then explicit legacy catalog provenance."""
    path = Path(path)
    for binding in layout.collections:
        if path == binding.path or binding.path in path.parents:
            return binding.path
    source, collection = record.get('source'), record.get('collection')
    platform = record.get('platform')
    parts = (platform, source, collection)
    if all(isinstance(value, str) and value and value not in {'.', '..'}
           and not any(char in value for char in ('/', '\\', '\x00')) for value in parts):
        boundary = layout.root / 'library' / platform / source / collection
        if boundary == path or boundary in path.parents:
            # Old arbitrary source names are evidence only when the actual
            # catalog path agrees, never a reason to walk another directory.
            if boundary.is_dir() and all(not parent.is_symlink() for parent in
                                         (boundary, boundary.parent, boundary.parent.parent)):
                return boundary
    return layout.collection_for(path)


def collection_path(path: str, collections=()) -> str | None:
    """Find a recorded collection boundary; never infer it from path depth.

    Artwork inventories use logical /roms paths so they remain usable off-host.
    Unknown boundaries deliberately cannot establish a numbered game's identity.
    """
    if not isinstance(path, str) or not path or '\\' in path or '\x00' in path:
        return None
    candidate = PurePosixPath(path)
    if '..' in candidate.parts:
        return None
    matches = []
    for value in collections:
        if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
            continue
        boundary = PurePosixPath(value)
        if '..' in boundary.parts:
            continue
        try:
            candidate.relative_to(boundary)
        except ValueError:
            continue
        matches.append(boundary)
    return str(max(matches, key=lambda item: len(item.parts))) if matches else None
