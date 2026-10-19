"""Platform names shared by the importer, catalog and emulator launcher.

An extension identifies a candidate, not proof that an emulator can run it.
Browser core names are those documented by EmulatorJS; native cores are Libretro.
"""
import re


def platform(name, browser_core, native_core, extensions, **extra):
    return dict(name=name, browser_core=browser_core, native_core=native_core,
                extensions=extensions.split(), **extra)


PLATFORMS = {
    "nes": platform("FC · 红白机", "fceumm", "nestopia", ".nes .unf .unif .zip .7z"),
    "fds": platform("FC 磁碟机", "fceumm", "nestopia", ".fds .zip .7z", requires_bios=True),
    "snes": platform("SFC · 超级任天堂", "snes9x", "snes9x", ".sfc .smc .bs .zip .7z"),
    "gb": platform("Game Boy", "gambatte", "gambatte", ".gb .zip .7z"),
    "gbc": platform("Game Boy Color", "gambatte", "gambatte", ".gbc .gb .zip .7z"),
    "gba": platform("Game Boy Advance", "mgba", "mgba", ".gba .zip .7z"),
    "nds": platform("Nintendo DS", "melonds", "melonds", ".nds .zip .7z"),
    "3ds": platform("Nintendo 3DS", None, "azahar", ".3ds .cci .cxi .cia .zip .7z"),
    "n64": platform("Nintendo 64", "mupen64plus_next", "mupen64plus_next", ".z64 .n64 .v64 .zip .7z"),
    "gamecube": platform("Nintendo GameCube", None, "dolphin", ".iso .gcm .rvz .ciso .gcz .zip .7z"),
    "wii": platform("Nintendo Wii", None, "dolphin", ".iso .wbfs .rvz .gcz .wad .zip .7z"),
    "switch": platform("Nintendo Switch", None, None, ".xci .nsp .nsz .xcz .zip .7z", requires_bios=True),
    "virtualboy": platform("Virtual Boy", "beetle_vb", "mednafen_vb", ".vb .vboy .zip .7z"),
    "pokemini": platform("Pokémon Mini", None, "pokemini", ".min .zip .7z"),
    "psx": platform("PlayStation", "pcsx_rearmed", "pcsx_rearmed", ".chd .cue .m3u .pbp .iso .img .mdf .zip .7z", requires_bios=True),
    "ps2": platform("PlayStation 2", None, None, ".iso .chd .cso .bin .mdf .m3u .zip .7z", requires_bios=True),
    "ps3": platform("PlayStation 3", None, None, ".iso .pkg .self .bin .zip .7z", requires_bios=True),
    "psp": platform("PSP", "ppsspp", "ppsspp", ".iso .cso .pbp .chd .zip .7z"),
    "megadrive": platform("MD · Mega Drive", "genesis_plus_gx", "genesis_plus_gx", ".md .gen .smd .bin .zip .7z"),
    "sega32x": platform("SEGA 32X", "picodrive", "picodrive", ".32x .bin .zip .7z"),
    "segacd": platform("Mega CD", "genesis_plus_gx", "genesis_plus_gx", ".chd .cue .m3u .iso .zip .7z", requires_bios=True),
    "mastersystem": platform("SEGA Master System", "smsplus", "genesis_plus_gx", ".sms .zip .7z"),
    "gamegear": platform("SEGA Game Gear", "genesis_plus_gx", "genesis_plus_gx", ".gg .zip .7z"),
    "sg1000": platform("SEGA SG-1000", None, "genesis_plus_gx", ".sg .sc .zip .7z"),
    "pico": platform("SEGA PICO", None, "picodrive", ".md .bin .zip .7z"),
    "saturn": platform("SEGA Saturn", "yabause", "mednafen_saturn", ".chd .cue .m3u .iso .zip .7z", requires_bios=True),
    "dreamcast": platform("SEGA Dreamcast", None, "flycast", ".chd .gdi .cdi .m3u .zip .7z", requires_bios=True),
    "naomi": platform("SEGA NAOMI", None, "flycast", ".zip .7z .chd .lst .dat", requires_bios=True),
    "model2": platform("SEGA Model 2", None, None, ".zip .7z"),
    "model3": platform("SEGA Model 3", None, None, ".zip .7z"),
    "fbneo": platform("街机 · FinalBurn Neo", "fbneo", "fbneo", ".zip .7z", arcade=True),
    "mame": platform("街机 · MAME", "mame2003_plus", "mame", ".zip .7z .chd", arcade=True),
    "hbmame": platform("街机 · HBMAME", None, None, ".zip .7z", arcade=True),
    "lightgun": platform("街机 · 光枪", None, "mame", ".zip .7z .chd", arcade=True),
    "teknoparrot": platform("街机 · TeknoParrot", None, None, ".exe .elf .zip .7z", kind="windows"),
    "3do": platform("Panasonic 3DO", "opera", "opera", ".chd .cue .iso .zip .7z", requires_bios=True),
    "atari2600": platform("Atari 2600", "stella2014", "stella", ".a26 .bin .zip .7z"),
    "atari5200": platform("Atari 5200", "a5200", "a5200", ".a52 .bin .zip .7z", requires_bios=True),
    "atari7800": platform("Atari 7800", "prosystem", "prosystem", ".a78 .bin .zip .7z"),
    "jaguar": platform("Atari Jaguar", "virtualjaguar", "virtualjaguar", ".j64 .jag .rom .zip .7z"),
    "lynx": platform("Atari Lynx", "handy", "handy", ".lnx .zip .7z"),
    "wonderswan": platform("WonderSwan", "mednafen_wswan", "mednafen_wswan", ".ws .zip .7z"),
    "wonderswancolor": platform("WonderSwan Color", "mednafen_wswan", "mednafen_wswan", ".wsc .ws .zip .7z"),
    "ngp": platform("Neo Geo Pocket", "mednafen_ngp", "mednafen_ngp", ".ngp .zip .7z"),
    "ngpc": platform("Neo Geo Pocket Color", "mednafen_ngp", "mednafen_ngp", ".ngc .ngp .zip .7z"),
    "neogeocd": platform("Neo Geo CD", None, "neocd", ".chd .cue .iso .zip .7z", requires_bios=True),
    "pce": platform("PC Engine", "mednafen_pce", "mednafen_pce_fast", ".pce .sgx .zip .7z"),
    "pcecd": platform("PC Engine CD", "mednafen_pce", "mednafen_pce_fast", ".chd .cue .m3u .iso .zip .7z", requires_bios=True),
    "pcfx": platform("PC-FX", "mednafen_pcfx", "mednafen_pcfx", ".chd .cue .iso .zip .7z", requires_bios=True),
    "colecovision": platform("ColecoVision", "gearcoleco", "gearcoleco", ".col .rom .zip .7z", requires_bios=True),
    "channelf": platform("Fairchild Channel F", None, "freechaf", ".bin .chf .zip .7z", requires_bios=True),
    "vectrex": platform("Vectrex", None, "vecx", ".vec .bin .zip .7z"),
    "odyssey2": platform("Magnavox Odyssey²", None, "o2em", ".bin .zip .7z", requires_bios=True),
    "msx": platform("MSX", None, "bluemsx", ".rom .dsk .cas .zip .7z", requires_bios=True),
    "msx2": platform("MSX2", None, "bluemsx", ".rom .dsk .cas .zip .7z", requires_bios=True),
    "supervision": platform("Watara Supervision", None, "potator", ".sv .bin .zip .7z"),
    "gamewatch": platform("Game & Watch", None, "gw", ".mgw .zip .7z"),
    "dos": platform("DOS · 经典电脑", "dosbox_pure", "dosbox_pure", ".zip .dosz .7z .exe .com .bat .conf", kind="dos"),
    "windows": platform("PC · Windows", None, None, ".exe .zip .7z .rar", kind="windows"),
    "openbor": platform("OpenBOR", None, None, ".pak .exe .zip .7z"),
    "xbox360": platform("Xbox 360", None, None, ".iso .xex .zip .7z"),
    "unknown": platform("待识别收藏", None, None, ".zip .7z .rar .iso .bin .rom"),
}

ALIASES = {
    "FC": "nes", "FC-HD": "nes", "SFC": "snes", "SFC-MSU1": "snes",
    "GB": "gb", "GBC": "gbc", "GBA": "gba", "NDS": "nds", "3DS": "3ds",
    "N64": "n64", "NGC": "gamecube", "WII": "wii", "WII Ware": "wii",
    "PS1": "psx", "PS2": "ps2", "PS3": "ps3", "psp": "psp",
    "MD": "megadrive", "MD-32X": "sega32x", "MD-CD": "segacd", "SMS": "mastersystem",
    "GG": "gamegear", "SS": "saturn", "SS plus": "saturn", "DC": "dreamcast",
    "NAOMI": "naomi", "MODEL2": "model2", "MODEL3": "model3", "3DO": "3do",
    "ATARI2600": "atari2600", "ATARI5200": "atari5200", "ATARI7800": "atari7800",
    "LYNX": "lynx", "WS": "wonderswan", "WSC": "wonderswancolor", "NGPC": "ngpc",
    "NEOGEO-CD": "neogeocd", "PCE": "pce", "PCE-CD": "pcecd", "PC-FX": "pcfx",
    "POKE MINI": "pokemini", "Virtual Boy": "virtualboy", "DOS": "dos", "PC": "windows",
    "OPENBOR": "openbor", "GAME WATCH": "gamewatch", "LIGHT GUN": "lightgun",
    "TeknoParrot": "teknoparrot", "HBMAME": "hbmame", "Arcade": "mame",
    "Atari - 2600": "atari2600", "Atari - 5200": "atari5200", "Atari - 7800": "atari7800",
    "Atari - Jaguar": "jaguar", "Atari - Lynx": "lynx",
    "Bandai - WonderSwan": "wonderswan", "Bandai - WonderSwan Color": "wonderswancolor",
    "Coleco - ColecoVision": "colecovision", "Fairchild - Channel F": "channelf",
    "GCE - Vectrex": "vectrex", "Handheld Electronic Game": "gamewatch",
    "Magnavox - Odyssey2": "odyssey2", "Microsoft - MSX": "msx", "Microsoft - MSX2": "msx2",
    "Microsoft - Xbox 360": "xbox360", "NEC - PC Engine - TurboGrafx 16": "pce",
    "NEC - PC Engine SuperGrafx": "pce", "Nintendo - 3DS": "3ds", "Nintendo - DS": "nds",
    "Nintendo - Family Computer Disk System": "fds", "Nintendo - Game Boy": "gb",
    "Nintendo - Game Boy Advance": "gba", "Nintendo - Game Boy Color": "gbc",
    "Nintendo - GameCube": "gamecube", "Nintendo - Nintendo 64": "n64",
    "Nintendo - Nintendo Entertainment System": "nes",
    "Nintendo - Super Nintendo Entertainment System": "snes", "Nintendo - Switch": "switch",
    "Nintendo - Virtual Boy": "virtualboy", "Nintendo - Wii": "wii",
    "SNK - Neo Geo Pocket": "ngp", "SNK - Neo Geo Pocket Color": "ngpc",
    "Sega - Dreamcast": "dreamcast", "Sega - Game Gear": "gamegear",
    "Sega - Master System - Mark III": "mastersystem", "Sega - Mega Drive - Genesis": "megadrive",
    "Sega - PICO": "pico", "Sega - SG-1000": "sg1000", "Sega - Saturn": "saturn",
    "Sony - PlayStation": "psx", "Sony - PlayStation 2": "ps2", "Sony - PlayStation 3": "ps3",
    "Sony - PlayStation Portable": "psp", "Watara - Supervision": "supervision",
}
_ALIASES = {k.casefold(): v for k, v in ALIASES.items()}


def resolve_platform(collection):
    name = collection.strip()
    name = re.sub(r" - CN$", "", name, flags=re.I)
    if name.casefold() in _ALIASES:
        return _ALIASES[name.casefold()]
    if name.lower().startswith(("fbneo", "fba ")):
        return "fbneo"
    if name.lower().startswith("mame "):
        return "mame"
    name = re.sub(r" hack.*$", "", name, flags=re.I)
    return _ALIASES.get(name.casefold(), "unknown")
