#!/usr/bin/env python3
"""Generate an original, public-domain PSP test program (no SDK/game/firmware bytes).

Hand-encoded MIPS ELF in an EBOOT.PBP; imports public PSP display/controller APIs.
Draws our own 5x7 lettering and changes a color band when CROSS is held.
No file I/O, networking, audio, save data, BIOS, or commercial content.
Output is deliberately outside version control. This source is dedicated to CC0.
"""
import argparse
import hashlib
import struct
from pathlib import Path

BASE = 0x08804000
PIXELS = BASE + 0x1000
FONT = {
'A':['01110','10001','10001','11111','10001','10001','10001'],
'C':['01111','10000','10000','10000','10000','10000','01111'],
'D':['11110','10001','10001','10001','10001','10001','11110'],
'E':['11111','10000','10000','11110','10000','10000','11111'],
'F':['11111','10000','10000','11110','10000','10000','10000'],
'G':['01111','10000','10000','10111','10001','10001','01111'],
'H':['10001','10001','10001','11111','10001','10001','10001'],
'I':['11111','00100','00100','00100','00100','00100','11111'],
'K':['10001','10010','10100','11000','10100','10010','10001'],
'L':['10000','10000','10000','10000','10000','10000','11111'],
'M':['10001','11011','10101','10101','10001','10001','10001'],
'N':['10001','11001','11001','10101','10011','10011','10001'],
'O':['01110','10001','10001','10001','10001','10001','01110'],
'P':['11110','10001','10001','11110','10000','10000','10000'],
'R':['11110','10001','10001','11110','10100','10010','10001'],
'S':['01111','10000','10000','01110','00001','00001','11110'],
'T':['11111','00100','00100','00100','00100','00100','00100'],
'U':['10001','10001','10001','10001','10001','10001','01110'],
'W':['10001','10001','10001','10101','10101','11011','10001'],
'X':['10001','10001','01010','00100','01010','10001','10001'],
'Y':['10001','10001','01010','00100','00100','00100','00100'],
' ':['00000']*7,
}


def sfo(category=b'MG\0'):
    values = [('BOOTABLE', 0x404, struct.pack('<I', 1)), ('CATEGORY', 0x204, category),
              ('DISC_ID', 0x204, b'RONE00001\0'), ('PSP_SYSTEM_VER', 0x204, b'1.00\0'),
              ('TITLE', 0x204, b'RetroOne Original Self Test\0')]
    keys, data, entries = bytearray(), bytearray(), bytearray()
    for key, fmt, value in values:
        offset = len(keys); keys.extend(key.encode() + b'\0')
        length = (len(value) + 3) & ~3
        entries.extend(struct.pack('<HHIII', offset, fmt, len(value), length, len(data)))
        data.extend(value.ljust(length, b'\0'))
    key_offset = 20 + len(entries); data_offset = (key_offset + len(keys) + 3) & ~3
    return struct.pack('<5I', 0x46535000, 0x101, key_offset, data_offset, len(values)) + entries + keys + bytes(data_offset-key_offset-len(keys)) + data


def build():
    blob = bytearray(0x1000 + 512 * 272 * 4)
    code, labels, branches = [], {}, []
    def emit(word): code.append(word)
    def imm(op, rt, rs, value): emit((op << 26) | (rs << 21) | (rt << 16) | (value & 65535))
    def li(rt, value): imm(15, rt, 0, value >> 16); imm(13, rt, rt, value)
    def call(index): emit((3 << 26) | ((BASE+0x4c0+index*8) >> 2 & 0x3ffffff)); emit(0)
    def label(name): labels[name] = len(code)
    def branch(op, rs, rt, name): branches.append((len(code), op, rs, rt, name)); emit(0); emit(0)
    # Display mode 480x272, format RGBA8888, framebuffer in our module memory.
    li(4, 0); li(5, 480); li(6, 272); call(0)
    li(4, PIXELS); li(5, 512); li(6, 3); li(7, 1); call(1)
    label('frame'); call(2)
    li(4, BASE+0x580); li(5, 1); call(3)
    li(8, BASE+0x580); imm(35, 9, 8, 4); imm(12, 9, 9, 0x4000)
    li(10, 0xffc0d866); branch(4, 9, 0, 'draw'); li(10, 0xff66a0ff)
    label('draw'); li(8, PIXELS+512*220*4); li(9, 512*24)
    label('pixel'); imm(43, 10, 8, 0); imm(9, 8, 8, 4); imm(9, 9, 9, -1); branch(5, 9, 0, 'pixel')
    branch(4, 0, 0, 'frame')
    for pos, op, rs, rt, target in branches:
        code[pos] = (op << 26) | (rs << 21) | (rt << 16) | ((labels[target]-pos-1) & 65535)
    blob[:len(code)*4] = struct.pack('<'+'I'*len(code), *code)
    blob[0x400:0x434] = struct.pack('<HH28s5I', 0, 0x100, b'RetroOneSelfTest', 0, 0, 0, BASE+0x440, BASE+0x468)
    # Two standard import tables: sceDisplay and sceCtrl. NIDs derive from names.
    blob[0x440:0x454] = struct.pack('<IHHBBHII', BASE+0x500, 0, 0x4001, 5, 0, 3, BASE+0x480, BASE+0x4c0)
    blob[0x454:0x468] = struct.pack('<IHHBBHII', BASE+0x510, 0, 0x4001, 5, 0, 1, BASE+0x48c, BASE+0x4d8)
    names = ['sceDisplaySetMode', 'sceDisplaySetFrameBuf', 'sceDisplayWaitVblankStart', 'sceCtrlReadBufferPositive']
    for i, name in enumerate(names):
        blob[0x480+i*4:0x484+i*4] = hashlib.sha1(name.encode()).digest()[:4]
        blob[0x4c0+i*8:0x4c8+i*8] = struct.pack('<II', 0x03e00008, 0)
    blob[0x500:0x50b] = b'sceDisplay\0'; blob[0x510:0x518] = b'sceCtrl\0'
    pixels = [0xff221710]*(512*272)
    def text(value, x, y, scale, color):
        for letter in value:
            for row, pattern in enumerate(FONT[letter]):
                for col, bit in enumerate(pattern):
                    if bit == '1':
                        for dy in range(scale):
                            for dx in range(scale): pixels[(y+row*scale+dy)*512+x+col*scale+dx] = color
            x += 6*scale
    text('RETROONE', 45, 28, 5, 0xffc0d866)
    text('PSP SELF TEST', 45, 88, 3, 0xfffaf4ed)
    text('CONTENT URI OK', 45, 124, 3, 0xfffaf4ed)
    text('NO GAME DATA', 45, 160, 2, 0xffc6b19e)
    text('HOLD X TO CHANGE COLOR', 45, 190, 2, 0xffc6b19e)
    blob[0x1000:] = struct.pack('<'+'I'*len(pixels), *pixels)
    strings = b'\0.text\0.rodata.sceModuleInfo\0.data\0.shstrtab\0'
    file_offset = 0x100; string_offset = file_offset+len(blob); sections_offset = (string_offset+len(strings)+3)&~3
    ident = b'\x7fELF\x01\x01\x01'+bytes(9)
    header = ident+struct.pack('<HH5I6H', 2, 8, 1, BASE, 52, sections_offset, 0x10001001, 52, 32, 1, 40, 5, 4)
    program = struct.pack('<8I', 1, file_offset, BASE, file_offset+0x400, len(blob), len(blob), 7, 0x100)
    def section(name, kind, flags, addr, offset, size, align=4):
        return struct.pack('<10I', strings.index(name.encode()+b'\0') if name else 0, kind, flags, addr, offset, size, 0, 0, align, 0)
    sections = bytes(40)+section('.text',1,6,BASE,file_offset,0x400)+section('.rodata.sceModuleInfo',1,2,BASE+0x400,file_offset+0x400,52)+section('.data',1,3,BASE+0x440,file_offset+0x440,len(blob)-0x440)+section('.shstrtab',3,0,0,string_offset,len(strings),1)
    elf = (header+program).ljust(file_offset,b'\0')+blob+strings+bytes(sections_offset-string_offset-len(strings))+sections
    param = sfo(); start=40+len(param)
    return struct.pack('<10I', 0x50425000, 0x10000, 40, start, start, start, start, start, start, start+len(elf))+param+elf


def build_iso():
    import io
    import pycdlib
    pbp = build(); offsets = struct.unpack_from('<10I', pbp)
    iso = pycdlib.PyCdlib(); iso.new(vol_ident='RETROONE_SELF_TEST', sys_ident='PSP GAME')
    iso.add_directory('/PSP_GAME'); iso.add_directory('/PSP_GAME/SYSDIR')
    for path, content in [('/PSP_GAME/PARAM.SFO;1', sfo(b'UG\0')),
                          ('/PSP_GAME/SYSDIR/EBOOT.BIN;1', pbp[offsets[8]:offsets[9]]),
                          ('/PSP_GAME/PROBE.DAT;1', b''.join(hashlib.sha256(str(i).encode()).digest() for i in range(2048))),
                          ('/UMD_DATA.BIN;1', b'RONE-00001|0000000000000000|0001|G')]:
        iso.add_fp(io.BytesIO(content), len(content), path)
    result = io.BytesIO(); iso.write_fp(result); iso.close()
    return result.getvalue()


def build_cso():
    import zlib
    source = build_iso(); blocks = [source[i:i+2048] for i in range(0, len(source), 2048)]
    offset = 24 + (len(blocks)+1)*4; indexes, compressed = [], bytearray()
    for block in blocks:
        encoder = zlib.compressobj(wbits=-15); packed = encoder.compress(block)+encoder.flush()
        indexes.append(offset); compressed.extend(packed); offset += len(packed)
    indexes.append(offset)
    return struct.pack('<4sIQIBB2x', b'CISO', 24, len(source), 2048, 1, 0) + struct.pack('<'+'I'*len(indexes), *indexes) + compressed

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    content = build_iso() if args.output.suffix.lower() == '.iso' else build_cso() if args.output.suffix.lower() == '.cso' else build()
    with args.output.open('xb') as stream: stream.write(content)
    print('Created original PSP test program:',args.output)
