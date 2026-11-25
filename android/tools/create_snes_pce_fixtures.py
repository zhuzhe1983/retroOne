#!/usr/bin/env python3
"""Create original CC0 SNES LoROM and PC Engine HuCard input/video probes.

Python 3 standard library only; no assembler, BIOS, commercial ROM, SDK, audio,
save data, or network access. The font is the original RetroOne PSP probe font.
Run with an output directory. --replace replaces only the three fixed outputs.
Generated artifacts describe expected behavior, not hardware verification.
This source and its generated ROM programs are dedicated to CC0-1.0.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from create_psp_fixture import FONT


class Code:
    """Only the literal instructions and label fixups used by these probes."""
    def __init__(self, base):
        self.base, self.data, self.labels, self.fixups = base, bytearray(), {}, []

    def emit(self, *values):
        self.data.extend(values)

    def word(self, value):
        self.data.extend(struct.pack('<H', value))

    def label(self, name):
        if name in self.labels:
            raise ValueError('Duplicate label: ' + name)
        self.labels[name] = len(self.data)

    def branch(self, opcode, name):
        self.fixups.append((len(self.data), name, False))
        self.emit(opcode, 0)

    def jump(self, name):
        self.fixups.append((len(self.data), name, True))
        self.emit(0x4c, 0, 0)

    def absolute(self, opcode, address):
        self.emit(opcode)
        self.word(address)

    def value(self, value, address):
        self.emit(0xa9, value)
        self.absolute(0x8d, address)

    def finish(self):
        for at, label, absolute in self.fixups:
            target = self.labels[label]
            if absolute:
                struct.pack_into('<H', self.data, at + 1, self.base + target)
            else:
                delta = target - at - 2
                if not -128 <= delta <= 127:
                    raise ValueError('Branch out of range: ' + label)
                self.data[at + 1] = delta & 255
        return bytes(self.data)


def artwork(platform, action, tile_bytes, first_tile=0):
    # Keep this local extra glyph original; the PSP fixture did not need B.
    font = dict(FONT)
    font['B'] = ['11110', '10001', '10001', '11110', '10001', '10001', '11110']
    letters = sorted(font)
    ids = {letter: index + 2 for index, letter in enumerate(letters)}
    tiles = bytearray((len(letters) + 2) * tile_bytes)
    for row in range(8):
        tiles[tile_bytes + row * 2] = 255  # Solid color-1 accent tile.
    for letter, index in ids.items():
        for row, bits in enumerate(font[letter]):
            tiles[index * tile_bytes + row * 2 + 1] = int(bits, 2) << 2
    tilemap = [first_tile] * (32 * 32)
    for y in (4, 5, 23, 24):
        for x in range(4, 28):
            tilemap[y * 32 + x] = first_tile + 1
    for y, text in [(8, 'RETROONE'), (12, platform + ' SELF TEST'),
                    (16, 'ORIGINAL CONTENT'), (20, 'HOLD ' + action + ' TO CHANGE COLOR')]:
        for x, letter in enumerate(text, (32 - len(text)) // 2):
            tilemap[y * 32 + x] = first_tile + ids[letter]
    return bytes(tiles), struct.pack('<1024H', *tilemap)


def build_snes():
    """32 KiB SlowROM, no coprocessor/SRAM; manual controller-1 B polling."""
    c = Code(0x8000)
    c.emit(0x78, 0xd8, 0x18, 0xfb)  # SEI CLD CLC XCE: native 65816.
    c.emit(0xc2, 0x30, 0xa2, 0xff, 0x1f, 0x9a)  # 16-bit X, stack $1fff.
    c.emit(0xa9, 0, 0, 0x5b, 0xe2, 0x20, 0x4b, 0xab)  # DP=0, A8, DB=PB=0.
    for address in (0x4200, 0x420b, 0x420c, 0x420d):
        c.value(0, address)
    c.value(0x80, 0x2100)  # Force blank throughout VRAM/CGRAM initialization.
    # Initialize background, window, color-math and display registers explicitly.
    for address in range(0x2101, 0x2134):
        if address not in (0x2118, 0x2119, 0x2122):
            c.value(0, address)
    for address in range(0x210d, 0x2115):
        c.value(0, address)
        c.value(0, address)
    c.value(1, 0x210b)  # BG1 character data at VRAM word $1000.
    c.value(0x80, 0x2115)  # Increment VRAM word after high-byte write.
    tiles, tilemap = artwork('SNES', 'B', 16)

    def dma(source, target, length):
        c.value(target & 255, 0x2116)
        c.value(target >> 8, 0x2117)
        for address, value in [(0x4300, 1), (0x4301, 0x18),
                               (0x4302, source & 255), (0x4303, source >> 8),
                               (0x4304, 0), (0x4305, length & 255), (0x4306, length >> 8)]:
            c.value(value, address)
        c.value(1, 0x420b)

    dma(0x9000, 0, len(tilemap))
    dma(0xa000, 0x1000, len(tiles))
    c.value(0, 0x2121)
    for color in (0x0c41, 0x7e00, 0x7fff, 0):
        c.value(color & 255, 0x2122)
        c.value(color >> 8, 0x2122)
    c.value(1, 0x212c)  # BG1 only, Mode 0, 2bpp.
    c.value(15, 0x2100)
    c.label('active')
    c.absolute(0xad, 0x4212)
    c.branch(0x30, 'active')  # Wait until preceding vblank has ended.
    c.label('vblank')
    c.absolute(0xad, 0x4212)
    c.branch(0x10, 'vblank')
    c.value(1, 0x4016)
    c.value(0, 0x4016)
    c.absolute(0xad, 0x4016)
    c.emit(0x29, 1)  # First serial bit is SNES B (not A).
    c.branch(0xf0, 'blue')
    c.value(1, 0x2121)
    c.value(0x9f, 0x2122)
    c.value(0, 0x2122)
    c.jump('active')
    c.label('blue')
    c.value(1, 0x2121)
    c.value(0, 0x2122)
    c.value(0x7e, 0x2122)
    c.jump('active')
    c.label('interrupt')
    c.emit(0x40)  # RTI; interrupt sources remain disabled.
    program = c.finish()
    if len(program) >= 0x1000:
        raise ValueError('SNES program overlaps artwork')
    rom = bytearray(32768)
    rom[:len(program)] = program
    rom[0x1000:0x1000 + len(tilemap)] = tilemap
    rom[0x2000:0x2000 + len(tiles)] = tiles
    rom[0x7fc0:0x7fd5] = b'RETROONE SNES PROBE'.ljust(21, b' ')
    # LoROM / ROM only / 32KiB / no SRAM / NTSC / homebrew / version 0.
    rom[0x7fd5:0x7fdc] = bytes([0x20, 0, 5, 0, 1, 0, 0])
    irq = 0x8000 + c.labels['interrupt']
    for address in (0x7fe4, 0x7fe6, 0x7fe8, 0x7fea, 0x7fee, 0x7ff4, 0x7ff8, 0x7ffa, 0x7ffe):
        struct.pack_into('<H', rom, address, irq)
    struct.pack_into('<H', rom, 0x7ffc, 0x8000)
    # Checksum + complement always add 510 byte-value units to the ROM sum.
    checksum = (sum(rom) + 510) & 65535
    struct.pack_into('<HH', rom, 0x7fdc, checksum ^ 65535, checksum)
    return bytes(rom)


def build_pce():
    """8 KiB HuCard, one bank, no CD/BIOS; controller-1 I polling."""
    c = Code(0xe000)
    c.emit(0x78, 0xd8, 0xd4, 0xa2, 0xff, 0x9a)  # SEI CLD CSH, stack.
    for bank, mask in ((0xff, 1), (0xf8, 2), (0, 0x80)):
        c.emit(0xa9, bank, 0x53, mask)  # TAM: I/O, work RAM, ROM bank 0.
    c.value(0, 0x0c01)  # Disable timer.
    c.value(7, 0x1402)  # Mask timer/IRQ1/IRQ2.
    c.value(0, 0x0801)  # PSG global volume zero; intentionally silent.
    c.value(0, 0x0400)  # VCE clock: 5.37MHz, 256 horizontal pixels.

    def vdc(register, value):
        c.emit(0x03, register, 0x13, value & 255, 0x23, value >> 8)

    vdc(5, 0)  # Display/interrupts disabled during initialization.
    for register, value in ((6, 0), (7, 0), (8, 0), (9, 0),
                            (10, 0x0202), (11, 0x041f), (12, 0x1702),
                            (13, 0x00df), (14, 0x000c), (15, 0)):
        vdc(register, value)
    tiles, tilemap = artwork('PCE', 'I', 32, first_tile=128)

    def transfer(source, target, length):
        vdc(0, target)
        c.emit(0x03, 2, 0xe3)  # ST0 #VWR; TIA source,$0002,length.
        c.word(source)
        c.word(2)
        c.word(length)

    transfer(0xe800, 0, len(tilemap))
    transfer(0xf000, 0x0800, len(tiles))

    def color(index, rgb):
        c.value(index & 255, 0x0402)
        c.value(index >> 8, 0x0403)
        c.value(rgb & 255, 0x0404)
        c.value(rgb >> 8, 0x0405)

    # VCE color bits are GGG RRR BBB, independent of the SNES palette layout.
    color(0, 0x001)
    color(1, 0x107)  # R=0 G=4 B=7: blue/cyan band.
    color(2, 0x1ff)
    color(256, 0x001)  # Overscan/border backdrop.
    # VBL status is generated only with CR bit 3 enabled. CPU SEI and the
    # HuC6280 IRQ mask remain set, so the program polls it without an ISR.
    vdc(5, 0x0088)  # Background + VBL status; no sprites or DMA.
    c.absolute(0xad, 0)  # Clear any startup status flags.
    c.label('frame')
    c.absolute(0xad, 0)
    c.emit(0x29, 0x20)
    c.branch(0xf0, 'frame')  # VDC status VBL is cleared by read.
    for value in (1, 3, 1, 0):
        c.value(value, 0x1000)
        c.emit(*([0xea] * 8))  # Joypad select/reset settling time.
    c.absolute(0xad, 0x1000)
    c.emit(0x29, 1)  # Active-low I; lower nibble selected by SEL=0.
    c.branch(0xd0, 'blue')
    color(1, 0x038)  # R=7 G=0 B=0.
    c.jump('frame')
    c.label('blue')
    color(1, 0x107)
    c.jump('frame')
    c.label('interrupt')
    c.emit(0x40)
    program = c.finish()
    if len(program) >= 0x800:
        raise ValueError('PCE program overlaps artwork')
    rom = bytearray(8192)
    rom[:len(program)] = program
    rom[0x800:0x800 + len(tilemap)] = tilemap
    rom[0x1000:0x1000 + len(tiles)] = tiles
    irq = 0xe000 + c.labels['interrupt']
    struct.pack_into('<5H', rom, 0x1ff6, irq, irq, irq, irq, 0xe000)
    return bytes(rom)


def build_all():
    """Return fresh deterministic binaries keyed by backend platform ID."""
    return {'snes': ('retroone-snes-probe.sfc', build_snes()),
            'pce': ('retroone-pce-probe.pce', build_pce())}


def generate(output_dir, replace=False):
    output_dir = Path(output_dir)
    built = build_all()
    artifacts = [(built[platform][0], platform, core, action, built[platform][1])
                 for platform, core, action in [('snes', 'snes9x', 'B'), ('pce', 'mednafen_pce_fast', 'I')]]
    manifest = {
        'schema': 1, 'license': 'CC0-1.0', 'generator': Path(__file__).name,
        'copyright': 'Original RetroOne code and lettering; no commercial game or firmware bytes.',
        'verification': 'Generation and structure only. Running this script does not prove emulator or device compatibility.',
        'behavior': 'RETROONE / platform SELF TEST / ORIGINAL CONTENT, white lettering and blue bands; hold the named controller-1 action to turn bands red, release to restore blue. Silent; no saves.',
        'artifacts': [{'file': name, 'platform': platform, 'core': core, 'bytes': len(data),
                       'sha256': hashlib.sha256(data).hexdigest(), 'hold_action': action,
                       'bios_required': False, 'cd_content': False}
                      for name, platform, core, action, data in artifacts],
    }
    files = [(name, data) for name, _, _, _, data in artifacts]
    files.append(('snes-pce-fixtures-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode()))
    output_dir.mkdir(parents=True, exist_ok=True)
    # Preflight every fixed output before writing anything; refuse symlinks even
    # with --replace so an output directory cannot redirect a write elsewhere.
    for name, _ in files:
        destination = output_dir / name
        if destination.is_symlink() or (destination.exists() and (not replace or not destination.is_file())):
            raise FileExistsError('Refusing to replace output: ' + str(destination))
    for name, data in files:
        with (output_dir / name).open('wb' if replace else 'xb') as stream:
            stream.write(data)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--replace', action='store_true', help='Replace only the three fixed generated outputs')
    arguments = parser.parse_args()
    result = generate(arguments.output_dir, arguments.replace)
    for artifact in result['artifacts']:
        print(artifact['file'], artifact['bytes'], artifact['sha256'])
