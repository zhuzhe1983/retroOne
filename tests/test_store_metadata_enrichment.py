import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('enrich_store_metadata', Path(__file__).parents[1] / 'scripts/enrich_store_metadata.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TID = '0100000000010000'


def audit(*ids):
    return {'games': [{'id': 'game1', 'header_valid': True, 'meta_title_ids': list(ids)}]}


def test_private_titledb_fields_never_escape_and_snapshot_has_provenance():
    databases = {'US.en.json': [{'id': TID, 'name': 'A game', 'key': 'private-key-secret',
                                'rightsId': 'private-rights-secret', 'size': 999,
                                'releaseDate': 20171027, 'publisher': 'Example',
                                'numberOfPlayers': 4, 'category': ['Action'], 'languages': ['en'],
                                'screenshots': ['https://img-eshop.cdn.nintendo.net/i/a.jpg',
                                                'http://localhost/private', 'https://attacker.example/a.jpg'],
                                'nsuId': 70010000001130}]}
    rows, report = module.prepare([{'id': 'game1', 'platform': 'switch', 'title': 'different local title'}],
                                  audit(TID, '0100000000010800'), databases)
    encoded = json.dumps(rows)
    assert 'private-key-secret' not in encoded and 'private-rights-secret' not in encoded
    assert 'size' not in rows[0]['details']
    assert rows[0]['details']['release_date'] == '2017-10-27'
    assert rows[0]['details']['screenshots'] == ['https://img-eshop.cdn.nintendo.net/i/a.jpg']
    assert 'popularity' not in rows[0]['details']
    assert '快照' in rows[0]['details']['sources'][0]['name']
    assert report['enriched_games'] == 1


def test_ambiguous_and_failed_identity_cannot_enrich_switch():
    assert module.identity_map(audit(TID, '010093801237C000')) == {}
    assert module.identity_map(audit('0100000000010800')) == {}
    value = audit(TID); value['games'][0]['header_valid'] = False
    assert module.identity_map(value) == {}


def test_snapshot_fallback_keeps_real_dates_and_cannot_invent_developer():
    rows = [('US.en.json', {'releaseDate': 'bad', 'publisher': 'Known', 'screenshots': []}),
            ('JP.ja.json', {'releaseDate': 20190229, 'publisher': 'Other'}),
            ('HK.zh.json', {'releaseDate': 20171027, 'screenshots': ['https://assets.nintendo.com/screen.jpg']})]
    details = module.snapshot_details(rows)
    assert details['publisher'] == 'Known'
    assert details['release_date'] == '2017-10-27'
    assert 'developer' not in details
    assert len(details['sources']) == 2


def official_document(tid=TID):
    wrong = {'applicationId': '010093801237C000', 'softwareDeveloper': 'Wrong suggested product',
             'softwareDetails': {'rights': [{'titleId': '010093801237C000'}]}}
    right = {'applicationId': tid, 'softwareDetails': {'rights': [{'titleId': tid}]},
             'softwareDeveloper': 'Actual developer', 'releaseDate': '2017-10-27T00:00:00.000Z',
             'numberOfPlayers': {'system': {'min': 1, 'max': 2}},
             'productGallery': [{'resourceType': 'image', 'publicId': 'store/software/a/screenshot'},
                                {'resourceType': 'video', 'publicId': 'video'},
                                {'resourceType': 'image', 'publicId': '../../secret'}]}
    data = {'props': {'pageProps': {'initialApolloState': {'suggested': wrong, 'actual': right}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'


def test_official_product_identity_avoids_related_product_metadata():
    details = module.store_details(official_document(), 'https://www.nintendo.com/us/store/products/a/', TID)
    assert details['developer'] == 'Actual developer'
    assert len(details['screenshots']) == 1
    assert '原机' in details['players']
    assert module.store_details(official_document(TID.lower()), 'https://www.nintendo.com/a', TID)['developer'] == 'Actual developer'
    with pytest.raises(ValueError):
        module.store_details(official_document(), 'https://www.nintendo.com/us/store/products/a/', '0100FE500D0CA000')


def test_sales_requires_date_and_reports_units_not_fake_live_heat():
    document = '''<div>( As of June 30, 2026 )</div>
    <p class="sales_title">Game A</p><p class="sales_value"><span>20.00</span> million pcs.</p>
    <p class="sales_title">Game B</p><p class="sales_value"><span>10.00</span> million pcs.</p>'''
    ranks = module.parse_sales(document, 'https://www.nintendo.co.jp/ir/en/finance/software/switch.html', 'switch')
    assert ranks[1]['popularity'] == {'score': 50.0, 'source': 'Nintendo 官方累计销量（本平台榜单归一化）',
        'url': 'https://www.nintendo.co.jp/ir/en/finance/software/switch.html',
        'kind': 'official_lifetime_sales', 'value': 10000000, 'unit': 'copies', 'as_of': '2026-06-30'}
    with pytest.raises(ValueError):
        module.parse_sales(document.replace('As of', 'Removed'), 'https://www.nintendo.co.jp/a', 'switch')


def test_sales_matching_is_exact_platform_scoped_and_unknown_is_absent():
    popularity = {'score': 100, 'source': 'official', 'url': 'https://www.nintendo.co.jp/a', 'kind': 'sales'}
    rankings = [{'platform': '3ds', 'title': 'Mario Kart 7', 'popularity': popularity}]
    games = [{'id': 'a', 'title': 'Mario Kart 7', 'platform': '3ds'},
             {'id': 'b', 'title': 'Mario Kart 7', 'platform': 'switch'},
             {'id': 'c', 'title': 'Mario Kart 7 MOD', 'platform': '3ds'},
             {'id': 'd', 'title': 'Mario Kart 8', 'platform': '3ds'}]
    rows, _ = module.prepare(games, {'games': []}, {}, rankings=rankings)
    assert [row['id'] for row in rows] == ['a']
    assert module.normalized_name('Mario Kart™ 8 Deluxe') == module.normalized_name('Mario Kart 8 Deluxe')


@pytest.mark.parametrize('url', ['http://www.nintendo.com/a', 'https://nintendo.com.attacker.example/a',
    'https://localhost/a', 'https://192.168.199.5/a', 'https://x@nintendo.com/a',
    'https://nintendo.com:8443/a', 'https://nintendo.com:notaport/a'])
def test_fetch_rejects_nonofficial_urls_before_network(url):
    assert module.official_url(url) is None
    with pytest.raises(ValueError):
        module.fetch(url)


def test_capcom_rejects_multiplatform_and_combined_variant_totals():
    header = '<time class="entry-date" datetime="2026-06-30">dated</time>'
    def entry(title, systems, value):
        return (f'<p class="name">{title}</p><p class="txt">Units : <span class="number">{value}</span>'
                f' million units<br>Release : Jun 1992<br>Platform : {systems}<br></p>')
    rows = module.parse_capcom_sales(header + entry('Street Fighter II', 'SNES', 6.3) +
        entry('Street Fighter II Turbo', 'SNES', 4.1) + entry('Resident Evil 2', 'PS4, PS5, NSW, PC, DL', 19.7) +
        entry('Mega Man Battle Network 4', 'GBA', 1.35), 'https://www.capcom.co.jp/ir/english/business/million.html')
    assert [row['title'] for row in rows] == ['Street Fighter II', 'Street Fighter II Turbo']
    assert rows[0]['popularity']['value'] == 6300000
    assert rows[1]['popularity']['score'] == 65.08


def test_financial_table_uses_lifetime_not_year_sales_and_retains_platform():
    document = '''Million-Seller Titles of NINTENDO Products
FY3/2007                         units in ten thousands
                       FY3/2007 Life-to-date
Game Boy Advance
      Pokémon Emerald                 113   18   96   632
      Pokémon FireRed/LeafGreen        148   18  130  1066
Nintendo GameCube
      The Legend of Zelda: Twilight Princess    132   7  125  132
Wii
      The Legend of Zelda: Twilight Princess    327  56  271  327
[Note] Software units include quantity bundled with hardware.'''
    rows = module.parse_financial_sales(document, 'https://www.nintendo.co.jp/ir/pdf/2007/070427e.pdf')
    assert len(rows) == 3
    assert rows[0]['popularity']['value'] == 6320000
    assert rows[0]['popularity']['as_of'] == '2007-03-31'
    assert [r['platform'] for r in rows[1:]] == ['gamecube', 'wii']


def test_same_platform_sources_share_denominator_and_latest_fact_wins():
    def fact(title, value, date):
        return {'platform': 'wii', 'title': title, 'popularity': {
            'value': value, 'unit': 'copies', 'score': 100, 'kind': 'official_lifetime_sales', 'as_of': date}}
    rows = module.normalize_rankings([fact('Wii Sports', 80000000, '2016-09-30'),
                                     fact('Wii Sports', 20000000, '2008-03-31'),
                                     fact('Resident Evil 4 Wii Edition', 2000000, '2026-06-30')])
    assert rows[0]['popularity']['value'] == 80000000
    assert rows[1]['popularity']['score'] == 2.5


def test_retail_matching_never_discards_edition_or_unknown_tags():
    assert module.normalized_name(module.retail_name('Mega Man 3 (Europe) (Rev A)')) == 'megaman3'
    assert module.retail_name('Mega Man 3 (Beta)') == 'Mega Man 3 (Beta)'
    assert module.retail_name('Mega Man 3 [HACKED]') == 'Mega Man 3 [HACKED]'
    assert module.retail_name('Mario Kart Wii [RMCE01]', 'wii') == 'Mario Kart Wii'
    assert module.modified_or_unverified({'title': 'game_patched'})
    assert module.modified_or_unverified({'title': 'game', 'rom_path': '/library/PS2 hack/003.chd'})


def test_translation_reference_is_explicit_and_cannot_bypass_mod_exclusion():
    pop = {'score': 100, 'source': 'Nintendo 官方累计销量', 'url': 'https://www.nintendo.co.jp/a',
           'kind': 'official_lifetime_sales', 'value': 1000, 'unit': 'copies'}
    ranks = [{'platform': '3ds', 'title': 'Mario Kart 7', 'popularity': pop}]
    games = [{'id': 'a', 'platform': '3ds', 'title': '马里奥赛车7 汉化版'},
             {'id': 'b', 'platform': '3ds', 'title': '马里奥赛车7 汉化 MOD'},
             {'id': 'c', 'platform': '3ds', 'title': 'Mario Kart 7 (CHS)'}]
    alias = {'platform': '3ds', 'title': 'Mario Kart 7', 'original_title_reference': True,
             'evidence': 'Reviewed exact title, platform and translation-only catalog evidence.'}
    rows, _ = module.prepare(games, {'games': []}, {}, rankings=ranks,
                             aliases={'a': alias, 'b': alias, 'c': dict(alias, evidence='')})
    assert [row['id'] for row in rows] == ['a']
    assert rows[0]['details']['popularity']['kind'] == 'original_title_sales_reference'
    assert '非当前版本独立销量' in rows[0]['details']['popularity']['source']


def test_snapshot_description_prefers_existing_chinese_and_strips_active_markup():
    details = module.snapshot_details([
        ('US.en.json', {'intro': 'An existing official English intro.', 'publisher': 'Publisher'}),
        ('HK.zh.json', {'description': '<style>.secret{}</style><script>privateCode()</script>'
                                      '<p>探索奇妙世界，與朋友一起展開冒險。</p>', 'nsuId': 70010000001130}),
    ])
    assert details['description'] == '探索奇妙世界，與朋友一起展開冒險。'
    assert 'privateCode' not in json.dumps(details)
    assert any('HK.zh.json' in source['name'] for source in details['sources'])
    assert module.short_description({}) is None
    assert len(module.short_description({'description': 'A' * 5000})) <= 701
