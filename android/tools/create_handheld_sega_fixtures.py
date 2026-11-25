#!/usr/bin/env python3
"""Create original CC0 GB/GBC/GBA/Mega Drive/Master System/Game Gear probes.

Python standard library only; no compiler, SDK, BIOS, commercial game bytes,
Nintendo logo bytes, audio, saves, or networking. GB/GBC/GBA logo fields are
zero: use emulator HLE/skip-BIOS mode, not unmodified original-hardware boot.
The manifest describes expected behavior, never claims device verification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from create_psp_fixture import FONT as _FONT

FONT = dict(_FONT, B=['11110', '10001', '10001', '11110', '10001', '10001', '11110'],
            V=['10001', '10001', '10001', '10001', '10001', '01010', '00100'])
MANIFEST = 'handheld-sega-fixtures-manifest.json'


class ByteCode:
    """Tiny label fixer for the explicitly emitted LR35902/Z80 instructions."""
    def __init__(self, base=0):
        self.base, self.code, self.labels, self.refs = base, bytearray(), {}, []

    def emit(self, *values):
        self.code.extend(values)

    def label(self, name):
        if name in self.labels:
            raise ValueError('Duplicate label: ' + name)
        self.labels[name] = len(self.code)

    def branch(self, opcode, name):
        self.refs.append((len(self.code), name, False))
        self.emit(opcode, 0)

    def jump(self, name):
        self.refs.append((len(self.code), name, True))
        self.emit(0xc3, 0, 0)

    def finish(self):
        for at, name, absolute in self.refs:
            if absolute:
                struct.pack_into('<H', self.code, at + 1, self.base + self.labels[name])
            else:
                distance = self.labels[name] - at - 2
                if not -128 <= distance < 128:
                    raise ValueError('Branch out of range: ' + name)
                self.code[at + 1] = distance & 255
        return bytes(self.code)


def tile_scene(width, height, label, visible_width, left=0, top=0, band_rows=(3, 4, 14, 15), instruction='HOLD A'):
    """Return 8x8 palette-index tiles and tile IDs, using original 5x7 letters."""
    tiles = [[0] * 64, [1] * 64]
    glyphs = {}
    for letter in sorted(FONT):
        glyphs[letter] = len(tiles)
        tile = [0] * 64
        for y, row in enumerate(FONT[letter]):
            for x, bit in enumerate(row):
                tile[y * 8 + x + 1] = 3 if bit == '1' else 0
        tiles.append(tile)
    names = [0] * (width * height)
    for y in band_rows:
        for x in range(1, visible_width - 1):
            names[(top + y) * width + left + x] = 1
    for y, line in [(6, 'RETROONE'), (8, label), (10, 'ORIGINAL'), (12, instruction)]:
        if len(line) > visible_width:
            raise ValueError('Label wider than screen')
        start = left + (visible_width - len(line)) // 2
        for x, letter in enumerate(line):
            names[(top + y) * width + start + x] = glyphs[letter]
    return tiles, names


def planar_tiles(tiles, planes):
    result = bytearray()
    for tile in tiles:
        for y in range(8):
            for plane in range(planes):
                result.append(sum(((tile[y * 8 + x] >> plane) & 1) << (7-x) for x in range(8)))
    return bytes(result)


def build_gb(color=False):
    tiles, names = tile_scene(32, 32, 'GBC SELF TEST' if color else 'GB SELF TEST', 20)
    patterns = planar_tiles(tiles, 2)
    b = ByteCode(0x150)
    e = b.emit
    def value(value, low): e(0x3e, value, 0xe0, low)
    def copy(source, destination, size, name):
        e(0x21, source & 255, source >> 8, 0x11, destination & 255, destination >> 8,
          0x01, size & 255, size >> 8)
        b.label(name); e(0x2a, 0x12, 0x13, 0x0b, 0x78, 0xb1); b.branch(0x20, name)
    e(0xf3, 0x31, 0xf0, 0xdf, 0xaf, 0xea, 0xff, 0xff)
    e(0xf0, 0x40, 0xe6, 0x80); b.branch(0x28, 'lcd_off')
    b.label('warm'); e(0xf0, 0x44, 0xfe, 144); b.branch(0x38, 'warm')
    b.label('lcd_off'); value(0, 0x40)
    if color:
        # Clear the CGB attribute plane; never depend on boot-ROM VRAM contents.
        value(1, 0x4f); e(0x21, 0x00, 0x98, 0x01, 0x00, 0x04)
        b.label('attributes'); e(0xaf, 0x22, 0x0b, 0x78, 0xb1); b.branch(0x20, 'attributes')
        value(0, 0x4f)
    copy(0x1000, 0x8000, len(patterns), 'tiles')
    copy(0x1400, 0x9800, len(names), 'names')
    value(0, 0x42); value(0, 0x43); value(0xe4, 0x47)
    if color:
        value(0x80, 0x68)
        for v in struct.pack('<4H', 0, 0x7c00, 0x03e0, 0x7fff): value(v, 0x69)
    value(0x91, 0x40)
    b.label('visible'); e(0xf0, 0x44, 0xfe, 144); b.branch(0x30, 'visible')
    b.label('frame'); e(0xf0, 0x44, 0xfe, 144); b.branch(0x38, 'frame')
    # Select action keys (P15 low), settle, then read A bit0 active-low.
    value(0x10, 0x00); e(0xf0, 0x00, 0xf0, 0x00, 0xe6, 1); b.branch(0x20, 'idle')
    if color:
        value(0x82, 0x68); value(0x1f, 0x69); value(0, 0x69)
    else: value(0xec, 0x47)
    b.jump('end_frame')
    b.label('idle')
    if color:
        value(0x82, 0x68); value(0, 0x69); value(0x7c, 0x69)
    else: value(0xe4, 0x47)
    b.label('end_frame'); value(0x30, 0x00); b.jump('visible')
    code = b.finish()
    if 0x150 + len(code) > 0x1000 or len(patterns) > 0x400:
        raise ValueError('GB data overlap')
    rom = bytearray(32768)
    rom[0x100:0x104] = bytes([0, 0xc3, 0x50, 1])
    title = b'RETROONE GBC' if color else b'RETROONE GB'
    rom[0x134:0x143] = title.ljust(15, b'\0')
    rom[0x143] = 0xc0 if color else 0
    rom[0x148] = 0  # 32KiB ROM-only cartridge; no RAM or save memory.
    rom[0x14a] = 1
    rom[0x150:0x150+len(code)] = code
    rom[0x1000:0x1000+len(patterns)] = patterns
    rom[0x1400:0x1800] = bytes(names)
    rom[0x14d] = (-sum(rom[0x134:0x14d])-25) & 255
    struct.pack_into('>H', rom, 0x14e, sum(rom) & 65535)
    return bytes(rom)


def build_sms(gamegear=False):
    # GG displays the central 160x144 crop: columns6..25, rows3..20.
    tiles, names = tile_scene(32, 28, 'GG SELF TEST' if gamegear else 'SMS SELF TEST',
                             20 if gamegear else 32, left=6 if gamegear else 0,
                             top=3 if gamegear else 2, instruction='HOLD II')
    patterns = planar_tiles(tiles, 4)
    table = struct.pack('<'+'H'*len(names), *names)
    b = ByteCode(); e = b.emit
    def out(value, port): e(0x3e, value, 0xd3, port)
    def address(where, kind=0x40): out(where & 255, 0xbf); out(kind | (where >> 8), 0xbf)
    def register(index, value): out(value, 0xbf); out(0x80 | index, 0xbf)
    def copy(source, size, name):
        e(0x21, source & 255, source >> 8, 0x01, size & 255, size >> 8)
        b.label(name); e(0x7e, 0xd3, 0xbe, 0x23, 0x0b, 0x78, 0xb1); b.branch(0x20, name)
    e(0xf3, 0x31, 0xf0, 0xdf, 0xed, 0x56)
    # PSG attenuation = silence; no sound driver or audio content.
    for attenuation in (0x9f, 0xbf, 0xdf, 0xff): out(attenuation, 0x7f)
    for index, value in enumerate((0x04, 0x00, 0x0e, 0xff, 0xff, 0x7e, 0, 0, 0, 0, 0xff)):
        register(index, value)
    address(0)
    e(0x01, 0x00, 0x40)
    b.label('clear'); e(0xaf, 0xd3, 0xbe, 0x0b, 0x78, 0xb1); b.branch(0x20, 'clear')
    address(0); copy(0x1000, len(patterns), 'tiles')
    address(0x3800); copy(0x1800, len(table), 'names')
    address(0x3f00); out(0xd0, 0xbe)  # End the sprite list.
    address(0, 0xc0)
    palette = ([0, 0x0f00, 0x00f0, 0x0fff] + [0]*28) if gamegear else ([0, 0x30, 0x0c, 0x3f] + [0]*28)
    palette_bytes = struct.pack('<32H', *palette) if gamegear else bytes(palette)
    for value in palette_bytes: out(value, 0xbe)
    register(1, 0x40)
    b.label('frame'); e(0xdb, 0xbf, 0xe6, 0x80); b.branch(0x28, 'frame')
    # Native button II is active-low bit5 and maps to RetroPad A in this core.
    e(0xdb, 0xdc, 0xe6, 0x20); b.branch(0x20, 'idle')
    address(2 if gamegear else 1, 0xc0)
    out(0x0f if gamegear else 0x03, 0xbe)
    if gamegear: out(0, 0xbe)
    b.jump('frame')
    b.label('idle'); address(2 if gamegear else 1, 0xc0)
    out(0 if gamegear else 0x30, 0xbe)
    if gamegear: out(0x0f, 0xbe)
    b.jump('frame')
    code = b.finish()
    if len(code) > 0x1000 or len(patterns) > 0x800 or len(table) > 0x800:
        raise ValueError('SMS/GG data overlap')
    rom = bytearray(32768)
    rom[:len(code)] = code
    rom[0x1000:0x1000+len(patterns)] = patterns
    rom[0x1800:0x1800+len(table)] = table
    rom[0x7ff0:0x7ff8] = b'TMR SEGA'  # Standard cartridge identification, not artwork.
    rom[0x7fff] = 0x6c if gamegear else 0x4c  # Export; 32KiB.
    struct.pack_into('<H', rom, 0x7ffa, sum(rom[:0x7ff0]) & 65535)
    return bytes(rom)


def gba_framebuffer():
    pixels = [0x0842] * (240 * 160)
    for y, text, scale in [(12, 'RETROONE', 3), (42, 'GBA SELF TEST', 2),
                           (64, 'ORIGINAL CONTENT', 2), (88, 'HOLD A', 1)]:
        start = (240 - len(text) * 6 * scale) // 2
        for i, letter in enumerate(text):
            for row, bits in enumerate(FONT[letter]):
                for col, bit in enumerate(bits):
                    if bit == '1':
                        for dy in range(scale):
                            for dx in range(scale):
                                pixels[(y+row*scale+dy)*240+start+i*6*scale+col*scale+dx] = 0x7fff
    return struct.pack('<'+'H'*len(pixels), *pixels)


def build_gba():
    code_at, pool_at, image_at = 0xc0, 0x300, 0x400
    words, labels, branches, literals = [], {}, [], {}
    def emit(word): words.append(word & 0xffffffff)
    def here(): return code_at + len(words) * 4
    def label(name): labels[name] = here()
    def load(reg, value):
        if value not in literals: literals[value] = len(literals)
        displacement = pool_at + literals[value]*4 - (here()+8)
        if not 0 <= displacement <= 4095: raise ValueError('ARM literal out of range')
        emit(0xe59f0000 | reg << 12 | displacement)
    def branch(name, condition=14):
        branches.append((len(words), name, condition)); emit(0)
    def strh(rd, rn, offset=0):
        emit(0xe1c000b0 | rn << 16 | rd << 12 | (offset & 0xf0) << 4 | offset & 15)
    emit(0xe321f0df)  # ARM system mode, IRQ/FIQ off; no stack or BIOS calls.
    load(0, 0x04000000); load(1, 0x0483); strh(1, 0)  # Mode3 BG2, forced blank.
    load(1, 0x0100); strh(1, 0, 0x20); strh(1, 0, 0x26)
    emit(0xe3a01000); strh(1, 0, 0x22); strh(1, 0, 0x24)
    emit(0xe5801028); emit(0xe580102c)  # BG2 identity transform/reference points.
    load(0, 0x06000000); load(1, 0x08000000 + image_at); load(2, 240*160//2)
    label('copy'); emit(0xe4913004); emit(0xe4803004); emit(0xe2522001); branch('copy', 1)
    load(0, 0x04000000); load(1, 0x0403); strh(1, 0)
    load(5, 0x04000006); load(6, 0x04000130)
    load(8, 0x7c007c00); load(9, 0x001f001f)
    label('visible'); emit(0xe1d500b0); emit(0xe35000a0); branch('visible', 2)
    label('vblank'); emit(0xe1d500b0); emit(0xe35000a0); branch('vblank', 3)
    emit(0xe1d600b0); emit(0xe3100001); emit(0x01a03009); emit(0x11a03008)
    load(0, 0x06000000 + 104*240*2); load(1, 240*40//2)
    label('band'); emit(0xe4803004); emit(0xe2511001); branch('band', 1); branch('visible')
    if here() > pool_at or pool_at+len(literals)*4 > image_at:
        raise ValueError('ARM code/data overlap')
    for index, name, condition in branches:
        offset = (labels[name] - (code_at+index*4+8)) // 4
        if not -(1 << 23) <= offset < (1 << 23): raise ValueError('ARM branch out of range')
        words[index] = condition << 28 | 0x0a000000 | offset & 0xffffff
    rom = bytearray(128 * 1024)
    struct.pack_into('<I', rom, 0, 0xea00002e)
    rom[0xa0:0xac] = b'RETROONE GBA'.ljust(12, b'\0')
    rom[0xac:0xb0] = b'ROGE'; rom[0xb0:0xb2] = b'00'; rom[0xb2] = 0x96
    rom[0xbd] = (-sum(rom[0xa0:0xbd])-0x19) & 255
    for i, word in enumerate(words): struct.pack_into('<I', rom, code_at+i*4, word)
    for value, i in literals.items(): struct.pack_into('<I', rom, pool_at+i*4, value)
    pixels = gba_framebuffer(); rom[image_at:image_at+len(pixels)] = pixels
    return bytes(rom)


def build_megadrive():
    tiles, names = tile_scene(64, 32, 'MEGA DRIVE SELF TEST', 40, top=2, band_rows=(3, 4, 18, 19, 20))
    # Packed nibbles; glyph index3 is mapped to white with CRAM entry3.
    tile_data = bytes(tile[i] << 4 | tile[i+1] for tile in tiles for i in range(0, 64, 2))
    nametable = struct.pack('>'+'H'*len(names), *names)
    code, labels, fixes = bytearray(), {}, []
    def w(value): code.extend(struct.pack('>H', value & 65535))
    def l(value): code.extend(struct.pack('>I', value & 0xffffffff))
    def label(name): labels[name] = 0x200+len(code)
    def branch(condition, name):
        w(0x6000 | condition << 8); fixes.append(('relative', len(code), name)); w(0)
    def dbra(reg, name):
        w(0x51c8 | reg); fixes.append(('relative', len(code), name)); w(0)
    def mb(value, address): w(0x13fc); w(value); l(address)
    def mw(value, address): w(0x33fc); w(value); l(address)
    def ml(value, address): w(0x23fc); l(value); l(address)
    data, control = 0xc00000, 0xc00004
    def fill(command, count, name):
        ml(command, control); w(0x7000); w(0x323c); w(count-1)
        label(name); w(0x33c0); l(data); dbra(1, name)
    def upload(command, name, size):
        ml(command, control); w(0x41f9); fixes.append(('absolute', len(code), name)); l(0)
        w(0x323c); w(size//2-1)
        label(name+'_copy'); w(0x33d8); l(data); dbra(1, name+'_copy')
    w(0x46fc); w(0x2700)
    w(0x1039); l(0xa10001); w(0x0200); w(15); branch(7, 'tmss_done')
    ml(0x53454741, 0xa14000)  # TMSS hardware protocol signature, not artwork.
    label('tmss_done'); w(0x3039); l(control)
    mw(0x0100, 0xa11100); mw(0, 0xa11200)  # Keep unused Z80 in reset.
    mb(0x40, 0xa10009); mb(0x40, 0xa10003)
    for attenuation in (0x9f, 0xbf, 0xdf, 0xff): mb(attenuation, 0xc00011)
    registers = [0x04, 0x04, 0x30, 0x34, 0x07, 0x78, 0, 0, 0, 0, 0xff, 0,
                 0x81, 0x3f, 0, 2, 1, 0, 0, 0, 0, 0, 0, 0]
    for i, value in enumerate(registers): mw(0x8000 | i << 8 | value, control)
    fill(0x40000000, 32768, 'clear_vram'); fill(0xc0000000, 64, 'clear_cram')
    fill(0x40000010, 40, 'clear_vsram')
    ml(0xc0000000, control)
    for value in (0, 0x0e60, 0x00e0, 0x0eee): mw(value, data)
    upload(0x40000000, 'tiles', len(tile_data)); upload(0x40000003, 'names', len(nametable))
    mw(0x8144, control)
    label('frame'); w(0x3039); l(control); w(0x0240); w(8); branch(6, 'frame')
    label('vblank'); w(0x3039); l(control); w(0x0240); w(8); branch(7, 'vblank')
    mb(0, 0xa10003)
    for _ in range(4): w(0x4e71)
    w(0x1039); l(0xa10003); mb(0x40, 0xa10003); w(0x0200); w(0x10); branch(7, 'held')
    w(0x303c); w(0x0e60); branch(0, 'color')
    label('held'); w(0x303c); w(0x000e)
    label('color'); ml(0xc0020000, control); w(0x33c0); l(data); branch(0, 'frame')
    label('exception'); w(0x60fe)
    label('tiles'); code.extend(tile_data)
    label('names'); code.extend(nametable)
    for kind, at, name in fixes:
        if kind == 'absolute': struct.pack_into('>I', code, at, labels[name])
        else:
            distance = labels[name] - (0x200+at)
            if not -32768 <= distance <= 32767: raise ValueError('68000 branch out of range')
            struct.pack_into('>h', code, at, distance)
    size = max(0x10000, 1 << (0x200+len(code)-1).bit_length())
    rom = bytearray(size); rom[0x200:0x200+len(code)] = code
    struct.pack_into('>II', rom, 0, 0x00ffff00, 0x200)
    for i in range(2, 64): struct.pack_into('>I', rom, i*4, labels['exception'])
    rom[0x100:0x200] = b' '*256
    def header(at, size, text): rom[at:at+size] = text.encode('ascii')[:size].ljust(size, b' ')
    header(0x100, 16, 'SEGA MEGA DRIVE'); header(0x110, 16, 'RETROONE CC0 2026')
    header(0x120, 48, 'RETROONE MD ORIGINAL INPUT TEST'); header(0x150, 48, 'RETROONE MD ORIGINAL INPUT TEST')
    header(0x180, 14, 'GM RO000001-00'); header(0x190, 16, 'J')
    struct.pack_into('>IIII', rom, 0x1a0, 0, size-1, 0xff0000, 0xffffff)
    rom[0x1b0:0x1bc] = bytes(12)
    header(0x1c8, 40, 'ORIGINAL CC0 VISUAL AND A BUTTON TEST'); header(0x1f0, 16, 'JUE')
    struct.pack_into('>H', rom, 0x18e, sum(struct.unpack('>'+'H'*((size-0x200)//2), rom[0x200:])) & 65535)
    return bytes(rom)


BUILDERS = {
    'gb': ('.gb', lambda: build_gb(False), 'gambatte', 'GB SELF TEST', 'Light gray band becomes black while native A is held.', 8),
    'gbc': ('.gbc', lambda: build_gb(True), 'gambatte', 'GBC SELF TEST', 'Blue band becomes red while native A is held.', 8),
    'gba': ('.gba', build_gba, 'mgba', 'GBA SELF TEST', 'Blue band becomes red while native A is held.', 8),
    'megadrive': ('.md', build_megadrive, 'genesis_plus_gx', 'MEGA DRIVE SELF TEST', 'Blue band becomes red while native Sega A is held (RetroPad Y).', 1),
    'mastersystem': ('.sms', lambda: build_sms(False), 'genesis_plus_gx', 'SMS SELF TEST', 'Blue band becomes red while native II is held (RetroPad A).', 8),
    'gamegear': ('.gg', lambda: build_sms(True), 'genesis_plus_gx', 'GG SELF TEST', 'Blue band becomes red while native II is held (RetroPad A).', 8),
}


def build_all():
    """Return fresh deterministic ROM bytes without touching the filesystem."""
    return {platform: ('retroone-'+platform+'-probe'+spec[0], spec[1]())
            for platform, spec in BUILDERS.items()}


def generate(output_dir, replace=False):
    output_dir = Path(output_dir)
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ValueError('Output must be a real directory, not a symlink or file')
    if output_dir.exists() and any(output_dir.iterdir()) and not replace:
        raise ValueError('Output directory is not empty; use --replace for fixed generated files')
    blobs, records = {}, []
    for platform, (extension, builder, core, title, behavior, button) in BUILDERS.items():
        name = 'retroone-' + platform + '-probe' + extension
        content = builder(); blobs[name] = content
        records.append({'platform': platform, 'filename': name, 'bytes': len(content),
                        'sha256': hashlib.sha256(content).hexdigest(), 'core': core,
                        'expected_text': ['RETROONE', title, 'ORIGINAL'],
                        'expected_input': behavior + ' Release restores the original band.',
                        'libretro_joypad_id': button,
                        'boot': 'HLE/skip BIOS; logo bytes intentionally zero' if platform in ('gb', 'gbc', 'gba') else 'No BIOS required',
                        'device_validation': 'not claimed by generator'})
    manifest = {'schema': 1, 'license': 'CC0-1.0',
                'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
                'copyright': 'Original RetroOne source, font, graphics, and executable instructions dedicated to CC0.',
                'scope': 'Visual and input probes only. No audio, save data, game assets, BIOS, Nintendo logo, or network activity.',
                'compatibility': 'GB/GBC/GBA omit vendor logo fields and are not compatible with unmodified original-hardware boot ROM checks.',
                'fixtures': records}
    blobs[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode()
    # Preflight every fixed destination before writing anything; unrelated files
    # in --replace mode are untouched, and symlinks are never followed.
    for name in blobs:
        target = output_dir / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('Refusing non-regular destination: ' + str(target))
        if target.exists() and not replace: raise ValueError('Destination already exists: ' + str(target))
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in blobs.items():
        with (output_dir/name).open('wb' if replace else 'xb') as stream: stream.write(content)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--replace', action='store_true', help='Replace only the six fixed ROM names and this generator manifest')
    args = parser.parse_args()
    try:
        result = generate(args.output_dir, args.replace)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error)+'\n')
    for fixture in result['fixtures']:
        print(fixture['filename'], fixture['sha256'])
