#!/usr/bin/env python3
"""Original CC0 NROM-128 NES probe: lettering, a colored band, A-button color change.

No SDK, BIOS, commercial game bytes, audio, persistent saves, or networking.
Run twice with --variant alternate to validate switching content in RetroArch.
"""
import argparse
from pathlib import Path
import struct

from create_psp_fixture import FONT


def build(alternate=False):
    code, labels, branches = bytearray(), {}, []
    def emit(*values): code.extend(values)
    def label(name): labels[name] = len(code)
    def branch(op, name): branches.append((len(code), op, name)); emit(op, 0)
    def jump(name): branches.append((len(code), 0x4c, name)); emit(0x4c, 0, 0)
    def store(address): emit(0x8d, address & 255, address >> 8)
    def value(n, address): emit(0xa9, n); store(address)
    # Disable interrupts/rendering, initialize the stack, and wait for the PPU to settle.
    emit(0x78, 0xd8, 0xa2, 0xff, 0x9a, 0xe8)
    emit(0x8e, 0x00, 0x20, 0x8e, 0x01, 0x20, 0x8e, 0x10, 0x40)
    value(0x40, 0x4017)
    for name in ('warm1', 'warm2'):
        label(name); emit(0x2c, 0x02, 0x20); branch(0x10, name)
    # Palette lives at $8100, one 32-byte table. Pattern tiles use colors 1 and 2.
    value(0x3f, 0x2006); value(0x00, 0x2006); emit(0xa2, 0)
    label('palette'); emit(0xbd, 0x00, 0x81); store(0x2007); emit(0xe8, 0xe0, 32); branch(0xd0, 'palette')
    value(0x20, 0x2006); value(0x00, 0x2006)
    for page in range(4):
        emit(0xa2, 0); label('page' + str(page))
        emit(0xbd, 0x00, 0x82 + page); store(0x2007); emit(0xe8); branch(0xd0, 'page' + str(page))
    value(0, 0x2005); value(0, 0x2005); value(0x0a, 0x2001)
    label('frame'); emit(0x2c, 0x02, 0x20); branch(0x10, 'frame')
    value(1, 0x4016); value(0, 0x4016)
    emit(0xad, 0x16, 0x40, 0x29, 1); branch(0xf0, 'idle')
    emit(0xa9, 0x16); jump('color')
    label('idle'); emit(0xa9, 0x2a if alternate else 0x21)
    label('color'); emit(0x48)
    value(0x3f, 0x2006); value(1, 0x2006); emit(0x68); store(0x2007)
    # Palette writes change the PPU temporary nametable bits; restore them too.
    value(0, 0x2000); value(0, 0x2005); value(0, 0x2005); jump('frame')
    label('irq'); emit(0x40)
    for at, op, target in branches:
        if op == 0x4c: struct.pack_into('<H', code, at + 1, 0x8000 + labels[target])
        else:
            distance = labels[target] - at - 2
            assert -128 <= distance <= 127
            code[at + 1] = distance & 255
    assert len(code) < 256
    prg = bytearray(16384); prg[:len(code)] = code
    palette = bytes([0x0f, 0x2a if alternate else 0x21, 0x30, 0x16] * 8)
    prg[0x100:0x120] = palette
    chr_rom = bytearray(8192)
    chr_rom[16:24] = bytes([255] * 8)  # Tile 1: solid accent color.
    alphabet = sorted(FONT)
    tiles = {letter: index + 2 for index, letter in enumerate(alphabet)}
    for letter, tile in tiles.items():
        for y, row in enumerate(FONT[letter]):
            # Pattern plane 1 selects white independently from the changing accent color.
            chr_rom[tile * 16 + 8 + y] = int(row, 2) << 2
    nametable = bytearray(1024)
    for y in (5, 6, 24, 25, 26):
        for x in range(4, 28): nametable[y * 32 + x] = 1
    for y, text in [(9, 'RETROONE ALT' if alternate else 'RETROONE'), (13, 'NES SELF TEST'),
                    (17, 'ORIGINAL CONTENT'), (21, 'HOLD A TO CHANGE COLOR')]:
        for i, letter in enumerate(text): nametable[y * 32 + (32-len(text))//2 + i] = tiles[letter]
    prg[0x200:0x600] = nametable
    struct.pack_into('<HHH', prg, 0x3ffa, 0x8000 + labels['irq'], 0x8000, 0x8000 + labels['irq'])
    return b'NES\x1a' + bytes([1, 1]) + bytes(10) + prg + chr_rom


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--variant', choices=['normal', 'alternate'], default='normal')
    parser.add_argument('--replace', action='store_true', help='Replace the generated build asset')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('wb' if args.replace else 'xb') as file: file.write(build(args.variant == 'alternate'))
    print('Created original NES test program:', args.output)
