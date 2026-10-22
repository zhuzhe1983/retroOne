#!/usr/bin/env python3
"""Prepare a reviewable metadata-only batch; never edit ROMs or a live database.

Inputs may contain private title databases. Only explicit presentation fields are
read into the output. Nintendo Title IDs, not fuzzy names, join Switch releases.
Network reads are optional, bounded and restricted to official publisher hosts.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import subprocess
import unicodedata
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
TITLE_ID = re.compile(r'^0100[0-9A-F]{9}000$')
SOURCES = ('US.en.json', 'titles.json', 'JP.ja.json', 'HK.zh.json')
OFFICIAL_HOSTS = ('nintendo.com', 'nintendo.net', 'nintendo.co.jp', 'capcom.co.jp')
LANGUAGES = {'ja': '日本語', 'en': 'English', 'zh': '中文', 'ko': '한국어',
             'es': 'Español', 'fr': 'Français', 'de': 'Deutsch', 'it': 'Italiano',
             'nl': 'Nederlands', 'ru': 'Русский', 'pt': 'Português'}


def official_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        valid_host = any(host == domain or host.endswith('.' + domain)
                         for domain in OFFICIAL_HOSTS)
        if (parsed.scheme == 'https' and valid_host and not parsed.username and
                not parsed.password and parsed.port in (None, 443)):
            return value
    except ValueError:
        pass
    return None


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not official_url(new_url):
            raise ValueError('Redirect outside official HTTPS source')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def fetch_bytes(url, *, timeout=20, ca_file=None):
    if not official_url(url):
        raise ValueError('Only approved official publisher HTTPS sources are supported')
    opener = build_opener(OfficialRedirects(), HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
    with opener.open(Request(url, headers={'User-Agent': 'RetroOneMetadata/1.0',
                                          'Accept': 'text/html'}), timeout=timeout) as response:
        data = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError('Source document exceeds size limit')
        return data


def fetch(url, *, timeout=20, ca_file=None):
    return fetch_bytes(url, timeout=timeout, ca_file=ca_file).decode('utf-8')


def iso_date(value):
    if not value:
        return None
    value = str(value)
    try:
        if re.fullmatch(r'\d{8}', value):
            return datetime.strptime(value, '%Y%m%d').date().isoformat()
        return datetime.strptime(value[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


def text(value, limit=512):
    return value.strip()[:limit] if isinstance(value, str) and '\x00' not in value else ''


def strings(value, limit=64):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(text(item, 128) for item in value if text(item, 128)))[:limit]


def identity_map(audit):
    """Reject ambiguous multi-base bundles; updates/DLC cannot become base IDs."""
    result = {}
    for row in audit.get('games', []):
        ids = {str(value).upper() for value in row.get('meta_title_ids', [])}
        bases = {value for value in ids if TITLE_ID.fullmatch(value)}
        if row.get('header_valid') is True and len(bases) == 1:
            result[row['id']] = next(iter(bases))
    return result


def title_index(databases):
    """A source can be a sanitized list or a private titleDB dictionary.

    Never copy source objects wholesale: some title databases carry key fields.
    """
    index = defaultdict(list)
    for source in SOURCES:
        rows = databases.get(source, [])
        rows = rows.values() if isinstance(rows, dict) else rows
        for row in rows:
            if not isinstance(row, dict):
                continue
            tid = str(row.get('id', '')).upper()
            if TITLE_ID.fullmatch(tid):
                index[tid].append((source, row))
    return index


class DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'iframe', 'noscript'}:
            self.hidden += 1
        if tag in {'p', 'br', 'div', 'li'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'iframe', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'div', 'li'} and not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def short_description(row):
    """A short source excerpt, never an invented summary or executable markup."""
    value = next((row.get(key) for key in ('intro', 'description')
                  if isinstance(row.get(key), str) and row[key].strip()), '')
    if not value:
        return None
    parser = DescriptionText(); parser.feed(value[:32768])
    plain = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', ''.join(parser.parts))
    paragraphs = [re.sub(r'\s+', ' ', part).strip() for part in plain.split('\n') if part.strip()]
    if not paragraphs:
        return None
    # Prefer the source's intro, or the first substantive paragraph. Do not
    # import legal footers, promotional lists, or entire store descriptions.
    plain = next((part for part in paragraphs if len(part) >= 40), paragraphs[0])
    sentences = re.split(r'(?<=[.!?。！？])(?:\s+|(?=[\u4e00-\u9fff]))', plain)
    plain = ' '.join(sentences[:2])
    if len(plain) > 700:
        boundary = plain.rfind(' ', 350, 700)
        plain = plain[:boundary if boundary >= 350 else 700].rstrip() + '…'
    return plain


def snapshot_source(source, row):
    country = {'JP.ja.json': 'JP', 'HK.zh.json': 'HK', 'US.en.json': 'US'}.get(source, row.get('region'))
    nsuid = str(row.get('nsuId') or '')
    url = ('https://ec.nintendo.com/apps/' + nsuid + '/' + country
           if re.fullmatch(r'\d{14}', nsuid) and country in ('US', 'JP', 'HK') else
           'https://www.nintendo.com/us/store/games/')
    return {'name': '本地 Nintendo eShop 资料快照（' + source + '）', 'url': url}


def snapshot_details(rows):
    result, used = {}, []
    for source, row in rows:
        detail = {'release_date': iso_date(row.get('releaseDate')),
                  'publisher': text(row.get('publisher')), 'developer': text(row.get('developer')),
                  'genres': strings(row.get('category')),
                  'languages': [LANGUAGES.get(code, code) for code in strings(row.get('languages'))],
                  'screenshots': list(dict.fromkeys(url for url in row.get('screenshots') or []
                                                   if official_url(url)))[:8]}
        players = row.get('numberOfPlayers')
        if type(players) is int and 0 < players <= 100:
            # Historical titleDB has a maximum only. Do not invent local/online support.
            detail['players'] = '最多 %d 人（原始商店资料，联机方式未核验）' % players
        selected = {key: value for key, value in detail.items() if value and not result.get(key)}
        if not selected:
            continue
        result.update(selected)
        # Standard eShop item deep link. Snapshot fields are explicitly labelled
        # as historic, not presented as a freshly fetched official product page.
        used.append(snapshot_source(source, row))
    priorities = {'HK.zh.json': 0, 'US.en.json': 1, 'titles.json': 2, 'JP.ja.json': 3}
    for source, row in sorted(rows, key=lambda item: priorities.get(item[0], 99)):
        description = short_description(row)
        if description:
            result['description'] = description
            ref = snapshot_source(source, row)
            if ref not in used:
                used.append(ref)
            break
    if result:
        result['sources'] = used
    return result


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.payload = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.payload.append(data)


def store_details(document, source_url, expected_id):
    """Match the page's own software rights, avoiding recommended-product cards."""
    if not official_url(source_url) or not TITLE_ID.fullmatch(expected_id):
        raise ValueError('Invalid official page identity')
    parser = Scripts(); parser.feed(document)
    page = json.loads(''.join(parser.payload))['props']['pageProps']
    state = page['initialApolloState']
    candidates = []
    for value in state.values():
        if not isinstance(value, dict):
            continue
        ids = {str(item.get('titleId', '')).upper() for item in (value.get('softwareDetails') or {}).get('rights', [])}
        if expected_id in ids and str(value.get('applicationId', '')).upper() == expected_id:
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError('Official product does not identify exactly one matching Title ID')
    product = candidates[0]
    details = {'official_url': source_url,
               'sources': [{'name': 'Nintendo 官方商店（US）', 'url': source_url}]}
    for target, key in [('release_date', 'releaseDate'), ('publisher', 'softwarePublisher'),
                        ('developer', 'softwareDeveloper')]:
        value = iso_date(product.get(key)) if target == 'release_date' else text(product.get(key))
        if value:
            details[target] = value
    if product.get('supportedLanguages'):
        details['languages'] = strings(product['supportedLanguages'])
    genres = strings([item.get('label') for item in (product.get('tags') or {}).get('genres', [])
                      if isinstance(item, dict)])
    if genres:
        details['genres'] = genres
    players = []
    for mode, label in [('system', '同机'), ('local', '本地无线'), ('online', '在线')]:
        counts = (product.get('numberOfPlayers') or {}).get(mode)
        if counts and type(counts.get('min')) is int and type(counts.get('max')) is int:
            players.append('%s %d–%d 人' % (label, counts['min'], counts['max']))
        elif counts and counts.get('min') == 1 and counts.get('label') == 'Single player':
            players.append(label + ' 1 人')
    if players:
        details['players'] = ' · '.join(players) + '（原机支持，模拟器联机需另核验）'
    screenshots = []
    for asset in product.get('productGallery') or []:
        path = asset.get('publicId', '')
        if (asset.get('resourceType') == 'image' and isinstance(path, str) and
                re.fullmatch(r'[A-Za-z0-9_./-]+', path) and '..' not in path.split('/')):
            screenshots.append('https://assets.nintendo.com/image/upload/q_auto/f_auto/w_1280/' + path)
    if screenshots:
        details['screenshots'] = list(dict.fromkeys(screenshots))[:8]
    return details


def plain_html(value):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]*>', ' ', value))).strip()


def parse_sales(document, source_url, platform):
    """Official cumulative sales, not popularity telemetry or review ratings."""
    date_match = re.search(r'As of\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})', document)
    if not date_match:
        raise ValueError('Sales source has no dated data')
    as_of = datetime.strptime(date_match[1], '%B %d, %Y').date().isoformat()
    titles = re.findall(r'<p\b[^>]*class="sales_title"[^>]*>(.*?)</p>', document, re.S)
    values = re.findall(r'<p\b[^>]*class="sales_value"[^>]*>\s*<span>([0-9.]+)</span>\s*million pcs.', document, re.S)
    if len(titles) != len(values) or not 1 <= len(titles) <= 50 or not official_url(source_url):
        raise ValueError('Sales page structure changed or untrusted source')
    maximum = max(float(value) for value in values)
    if maximum <= 0:
        raise ValueError('Invalid sales values')
    return [{'platform': platform, 'title': plain_html(title),
             'popularity': {'score': round(float(value) / maximum * 100, 2),
                            'source': 'Nintendo 官方累计销量（本平台榜单归一化）',
                            'url': source_url, 'kind': 'official_lifetime_sales',
                            'value': round(float(value) * 1000000), 'unit': 'copies', 'as_of': as_of}}
            for title, value in zip(titles, values) if individual_sales_title(plain_html(title))]


def individual_sales_title(title):
    """Do not assign a grouped-version total to any one retail version."""
    name = normalized_name(title)
    return '/' not in title and name not in {
        'nintendogs', 'nintendogscats', 'megamanbattlenetwork4',
    }


def parse_capcom_sales(document, source_url):
    """Capcom separates platforms/editions; multi-platform totals stay unused."""
    if not official_url(source_url) or urlsplit(source_url).hostname != 'www.capcom.co.jp':
        raise ValueError('Untrusted Capcom sales source')
    match = re.search(r'<time\b[^>]*datetime="(\d{4}-\d{2}-\d{2})"', document)
    as_of = iso_date(match[1]) if match else None
    if not as_of:
        raise ValueError('Capcom sales source has no dated data')
    entries = re.findall(
        r'<p class="name">(.*?)</p>.*?<p class="txt">Units : <span class="number">([0-9.]+)</span>'
        r'.*?Release : (.*?)<br>Platform : (.*?)<br>', document, re.S)
    if not 1 <= len(entries) <= 500:
        raise ValueError('Capcom sales page structure changed')
    platforms = {'NES': 'nes', 'SNES': 'snes', 'MD': 'megadrive', 'PS': 'psx',
                 'PS2': 'ps2', 'PSP': 'psp', '3DS': '3ds', 'GBA': 'gba',
                 'GC': 'gamecube', 'DC': 'dreamcast', 'Wii': 'wii', 'NSW': 'switch'}
    result = []
    for title, value, release, physical in entries:
        title, physical = plain_html(title), plain_html(physical)
        physical = re.sub(r'DL\s*\(([^()]+)\)', r'\1', physical)
        systems = {part.strip() for part in physical.split(',') if part.strip() != 'DL'}
        if len(systems) != 1 or not individual_sales_title(title):
            continue
        platform = platforms.get(next(iter(systems)))
        if not platform:
            continue
        copies = round(float(value) * 1000000)
        if not 0 < copies <= 1000000000:
            raise ValueError('Invalid Capcom sales value')
        result.append({'platform': platform, 'title': title, 'source_release': plain_html(release),
                       'popularity': {'score': 0, 'source': 'Capcom 官方累计销量（同平台已知销量归一化）',
                                      'url': source_url, 'kind': 'official_lifetime_sales',
                                      'value': copies, 'unit': 'copies', 'as_of': as_of}})
    return normalize_rankings(result)


def parse_financial_sales(document, source_url):
    """Read Nintendo's 2007/2008 million-seller tables after pdftotext -layout.

    The final column is life-to-date, in ten-thousands, not the fiscal-year total.
    Reject wrapped/unknown rows instead of inferring missing column positions.
    """
    if not official_url(source_url) or urlsplit(source_url).hostname != 'www.nintendo.co.jp':
        raise ValueError('Untrusted financial source')
    match = re.search(r'Million-Seller Titles of NINTENDO Products\s*FY3/(\d{4})',
                      unicodedata.normalize('NFKC', document))
    if not match:
        raise ValueError('Nintendo million-seller table not found')
    table = unicodedata.normalize('NFKC', document)[match.end():].split('[Note]', 1)[0]
    if 'units in ten thousands' not in table or 'Life-to-date' not in table:
        raise ValueError('Financial table units or lifetime column missing')
    platforms = {'Game Boy Advance': 'gba', 'Nintendo DS': 'nds',
                 'Nintendo GameCube': 'gamecube', 'Wii': 'wii'}
    platform, output = None, []
    for line in table.splitlines():
        if line.strip() in platforms:
            platform = platforms[line.strip()]
            continue
        row = re.fullmatch(r'\s+(.+?)\s{2,}([\d,]+|-)\s+([\d,]+|-)\s+([\d,]+|-)\s+([\d,]+)\s*', line)
        if not row or not platform or not individual_sales_title(row[1]):
            continue
        title = row[1].strip()
        # A publisher's bundle note is not a different version of Wii Play.
        if title == 'Wii Play (Wii Remote bundled)':
            title = 'Wii Play'
        copies = int(row[5].replace(',', '')) * 10000
        output.append({'platform': platform, 'title': title,
                       'popularity': {'score': 0, 'source': 'Nintendo 官方财报累计销量（同平台已知销量归一化）',
                                      'url': source_url, 'kind': 'official_lifetime_sales',
                                      'value': copies, 'unit': 'copies', 'as_of': match[1] + '-03-31'}})
    if not output:
        raise ValueError('Financial table has no supported rows')
    return normalize_rankings(output)


def normalized_name(value):
    value = unicodedata.normalize('NFKD', value.replace('™', '').replace('®', '')).casefold()
    return ''.join(char for char in value if char.isalnum() and not unicodedata.combining(char))


def normalize_rankings(rows):
    """Prefer the latest dated fact; use one denominator across all same-platform sources."""
    newest = {}
    for row in rows:
        key = row['platform'], normalized_name(row['title'])
        previous = newest.get(key)
        if not previous or row['popularity'].get('as_of', '') > previous['popularity'].get('as_of', ''):
            newest[key] = row
    maximum = defaultdict(float)
    for row in newest.values():
        pop = row['popularity']
        if pop.get('unit') == 'copies' and pop.get('kind') == 'official_lifetime_sales':
            maximum[row['platform']] = max(maximum[row['platform']], pop.get('value', 0))
    output = []
    for row in newest.values():
        copy = dict(row, popularity=dict(row['popularity']))
        if maximum[row['platform']] and copy['popularity'].get('unit') == 'copies':
            copy['popularity']['score'] = round(copy['popularity']['value'] / maximum[row['platform']] * 100, 2)
        output.append(copy)
    return output


def substantial_modification(game):
    value = (str(game.get('title', '')) + '\n' + str(game.get('rom_path', ''))).replace('_', ' ')
    return bool(re.search(r'改版|修改|破解|重制|重製|合集|合辑|\d+合1|'
                          r'\b(?:mod|hacks?|patched|prototype|proto|beta|pirate|unl|'
                          r'virtual console|msu[- ]?1|genesis mini|arcade legends|remake|remaster)\b', value, re.I))


def modified_or_unverified(game):
    value = (str(game.get('title', '')) + '\n' + str(game.get('rom_path', ''))).replace('_', ' ')
    return substantial_modification(game) or bool(re.search(
        r'汉化|漢化|改简|补丁|補丁|官改|文本移植|\b(?:translation|chs|cht)\b', value, re.I))


def retail_name(value, platform=None):
    """Remove only known retail region/revision labels, never edition or mod text."""
    def allowed(tag):
        return bool(re.fullmatch(
            r'(?:USA|Europe|Japan|World|Australia|Canada|Korea|Taiwan|China|Asia|Brazil|'
            r'UK|US|EU|JP)(?:, (?:USA|Europe|Japan|Australia|Canada|Korea|Taiwan|China|Asia|Brazil))*'
            r'|Rev(?:ision)? [A-Z0-9.]+' r'|(?:En|Ja|Fr|De|Es|It|Nl|Pt|Sv|No|Da|Fi|Ko|Zh)'
            r'(?:,(?:En|Ja|Fr|De|Es|It|Nl|Pt|Sv|No|Da|Fi|Ko|Zh))*', tag, re.I))
    tags = re.findall(r'\(([^()]*)\)', value)
    if any(not allowed(tag) for tag in tags):
        return value
    value = re.sub(r'\([^()]*\)', '', value)
    # A region-specific product code is retained in the audit; it does not alter title identity.
    if platform == 'wii':
        value = re.sub(r'\s*\[[RS][A-Z0-9]{2}[EJPWKDIFSX][0-9A-F]{2}\]\s*$', '', value)
    value = re.sub(r'^(.*?), The(?= - |$)', r'The \1', value, flags=re.I)
    return value.strip()


def prepare(catalog, audit, databases, official=None, rankings=None, aliases=None):
    identities, index = identity_map(audit), title_index(databases)
    official, aliases = official or {}, aliases or {}
    ranking_index = {(row['platform'], normalized_name(row['title'])): row for row in normalize_rankings(rankings or [])}
    output, matched = [], Counter()
    sales_matches = []
    for game in catalog:
        tid = identities.get(game['id']) if game.get('platform') == 'switch' else None
        rows = index.get(tid, [])
        details = snapshot_details(rows)
        if tid in official:
            old_sources = details.get('sources', [])
            details.update(official[tid])
            details['sources'] = official[tid].get('sources', []) + old_sources
        names = [retail_name(game.get('title', ''), game.get('platform'))] + [row.get('name', '') for _, row in rows]
        alias = aliases.get(game['id'])
        reviewed = bool(alias and alias.get('platform') == game.get('platform') and
                        isinstance(alias.get('evidence'), str) and len(alias['evidence'].strip()) >= 16)
        reference = bool(reviewed and alias.get('original_title_reference') is True)
        if reviewed:
            names.append(alias.get('title', ''))
        candidates = {normalized_name(name) for name in names if isinstance(name, str)}
        possible = [ranking_index[(game.get('platform'), name)] for name in candidates
                    if (game.get('platform'), name) in ranking_index]
        eligible = not substantial_modification(game) and (reference or not modified_or_unverified(game))
        rank = possible[0] if len(possible) == 1 and eligible else None
        if rank:
            popularity = dict(rank['popularity'])
            if reference:
                popularity['kind'] = 'original_title_sales_reference'
                publisher = popularity['source'].split(' 官方', 1)[0]
                popularity['source'] = publisher + ' 原作官方销量参考（非当前版本独立销量；同平台已知销量归一化）'
            details['popularity'] = popularity
            details.setdefault('sources', []).append({'name': popularity['source'], 'url': popularity['url']})
            sales_matches.append({'id': game['id'], 'platform': game.get('platform'),
                                  'title': game.get('title'), 'source_title': rank['title'],
                                  'source_url': rank['popularity']['url'],
                                  'match': 'reviewed_original_reference' if reference else
                                           ('reviewed_alias' if reviewed else ('title_id' if tid else 'exact_retail_title')),
                                  'evidence': alias['evidence'] if reviewed else 'Exact title and same platform; only region/revision labels removed.'})
        if details:
            output.append({'id': game['id'], 'details': details})
            matched.update(key for key, value in details.items() if value)
    return output, {'catalog_games': len(catalog), 'enriched_games': len(output),
                    'field_coverage': dict(matched),
                    'sales_coverage_by_platform': dict(Counter(row['platform'] for row in sales_matches)),
                    'sales_matches': sales_matches,
                    'notes': ['Missing popularity is unknown, never zero or family play count.',
                              'Official sales scores use the maximum among known same-platform sources; not a cross-platform popularity estimate.',
                              'Sales refer to all regions of the same platform release, not individual regional ROM unit counts.',
                              'Translations require a reviewed identity and a distinct original_title_sales_reference label; not translation sales.',
                              'Substantial mods, prototypes, re-releases and combined-version sales are not automatically inherited.',
                              'Historic metadata release dates and languages refer to source storefronts, not verified ROM regions.',
                              'Multiplayer counts describe original hardware; emulator networking is not asserted.']}


def load(path):
    return json.loads(Path(path).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--identity-audit', required=True)
    parser.add_argument('--title-databases', required=True)
    parser.add_argument('--source-plan', help='Local JSON with optional official_pages and sales_pages lists')
    parser.add_argument('--aliases', help='Reviewed exact game ID → {platform,title} sales matching')
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true', help='Fetch at most 32 official pages; otherwise use only cached HTML')
    parser.add_argument('--ca-file', help='Optional trusted CA bundle; TLS verification is never disabled')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = load(args.source_plan) if args.source_plan else {}
    pages = [('official', row) for row in plan.get('official_pages', [])]
    pages += [('sales', row) for row in plan.get('sales_pages', [])]
    if len(pages) > 32:
        parser.error('Source plan exceeds 32 pages')
    official, rankings, errors = {}, [], []
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    for kind, row in pages:
        name = row.get('cache_name', '')
        if not re.fullmatch(r'[a-z0-9_-]{1,80}', name):
            parser.error('Invalid cache_name')
        source_format = row.get('format', 'nintendo_html')
        path = args.cache_dir / (name + ('.txt' if source_format == 'nintendo_financial_text' else '.html'))
        try:
            if args.fetch:
                if source_format == 'nintendo_financial_text':
                    data = fetch_bytes(row['url'], ca_file=args.ca_file)
                    if not data.startswith(b'%PDF'):
                        raise ValueError('Expected a financial PDF')
                    result = subprocess.run(['pdftotext', '-layout', '-', '-'], input=data,
                                            capture_output=True, timeout=20, check=True)
                    if len(result.stdout) > MAX_DOCUMENT_BYTES:
                        raise ValueError('Extracted PDF text exceeds size limit')
                    document = result.stdout.decode('utf-8')
                else:
                    document = fetch(row['url'], ca_file=args.ca_file)
                path.write_text(document)
            else:
                if path.stat().st_size > MAX_DOCUMENT_BYTES:
                    raise ValueError('Cached document exceeds size limit')
                document = path.read_text()
            if kind == 'official':
                official[row['title_id']] = store_details(document, row['url'], row['title_id'])
            elif source_format == 'capcom_platinum':
                rankings.extend(parse_capcom_sales(document, row['url']))
            elif source_format == 'nintendo_financial_text':
                rankings.extend(parse_financial_sales(document, row['url']))
            elif source_format == 'nintendo_html':
                rankings.extend(parse_sales(document, row['url'], row['platform']))
            else:
                raise ValueError('Unsupported source format')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            errors.append({'source': row['url'], 'error': str(error)[:300]})
    catalog = load(args.catalog)
    if isinstance(catalog, dict):
        catalog = catalog['games']
    rows, report = prepare(catalog, load(args.identity_audit), load(args.title_databases),
                           official, rankings, load(args.aliases) if args.aliases else {})
    report.update(source_errors=errors, official_products=len(official), sales_titles=len(rankings))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    args.output.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
