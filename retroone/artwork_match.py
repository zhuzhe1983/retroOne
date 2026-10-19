"""Conservative, auditable cover matching. No fuzzy or translated-title guesses."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
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


def collection_path(path: str) -> str:
    return str(Path(*Path(path).parts[:6]))
