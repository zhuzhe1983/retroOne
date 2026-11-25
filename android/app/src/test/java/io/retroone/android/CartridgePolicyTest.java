package io.retroone.android;

import static org.junit.Assert.*;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Arrays;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class CartridgePolicyTest {
  @Rule public final TemporaryFolder temporary = new TemporaryFolder();

  @Test
  public void backendIdsAndAliasesResolveWithoutEnablingDiscOrSubsystemPlatforms() {
    String[][] aliases = {
      {"snes", "SFC", "Nintendo - Super Nintendo Entertainment System"},
      {"gb", "GB", "Nintendo - Game Boy"},
      {"gbc", "GBC", "Nintendo - Game Boy Color"},
      {"gba", "GBA", "Nintendo - Game Boy Advance"},
      {"megadrive", "MD", "Sega - Mega Drive - Genesis"},
      {"mastersystem", "SMS", "Sega - Master System - Mark III"},
      {"gamegear", "GG", "Sega - Game Gear"},
      {"pce", "pcengine", "NEC - PC Engine - TurboGrafx 16"}
    };
    assertEquals(aliases.length, CartridgePolicy.all().size());
    for (String[] group : aliases) {
      CartridgePolicy.Spec value = CartridgePolicy.spec(group[0]);
      for (String alias : group) {
        assertTrue(alias, CartridgePolicy.supports(alias));
        assertSame(value, CartridgePolicy.spec(alias));
      }
      assertTrue(value.core.matches("[a-z0-9_]+_libretro_android\\.so"));
      assertEquals("", CartridgePolicy.formatReason(value.id, value.probeAsset));
    }
    for (String platform :
        new String[] {
          "nes",
          "fds",
          "psp",
          "switch",
          "fbneo",
          "segacd",
          "sega32x",
          "pico",
          "sg1000",
          "pcecd",
          "pcenginecd",
          "SFC-MSU1",
          "NEC - PC Engine SuperGrafx",
          "../../gb",
          "",
          null
        }) assertFalse(String.valueOf(platform), CartridgePolicy.supports(platform));
    assertThrows(UnsupportedOperationException.class, () -> CartridgePolicy.all().clear());
    assertThrows(
        UnsupportedOperationException.class,
        () -> CartridgePolicy.spec("gba").extensions.add(".elf"));
    assertEquals("mednafen_pce_fast_libretro_android.so", CartridgePolicy.spec("pce").core);
  }

  @Test
  public void safeStagedNamesKeepOnlyTheSelectedPlatformsOriginalExtension() throws Exception {
    String sha = "a".repeat(64);
    for (CartridgePolicy.Spec value : CartridgePolicy.all()) {
      for (String extension : value.extensions) {
        String name = CartridgePolicy.stagedName(value, sha, "我的测试" + extension.toUpperCase());
        assertEquals("ro-" + value.id + "-" + sha + extension, name);
        assertTrue(CartridgePolicy.isStagedName(value, name));
        assertFalse(CartridgePolicy.isStagedName(value, "../" + name));
        assertFalse(CartridgePolicy.isStagedName(value, name + ".zip"));
        assertFalse(CartridgePolicy.isStagedName(value, name + "\n"));
      }
      for (String invalid :
          new String[] {
            "../game" + value.extensions.get(0),
            "dir/game" + value.extensions.get(0),
            "C:\\game" + value.extensions.get(0),
            "game\n\n" + value.extensions.get(0),
            "game\u0085" + value.extensions.get(0),
            value.extensions.get(0),
            "game.zip",
            "game.7z",
            "game.cue",
            "game.chd",
            "game.m3u",
            "game",
            "",
            null
          }) {
        assertFalse(
            String.valueOf(invalid), CartridgePolicy.formatReason(value.id, invalid).isEmpty());
        assertThrows(IOException.class, () -> CartridgePolicy.stagedName(value, sha, invalid));
      }
    }
    CartridgePolicy.Spec md = CartridgePolicy.spec("megadrive");
    for (String shaValue : new String[] {"a".repeat(63), "A".repeat(64), "../escape", "", null})
      assertThrows(IOException.class, () -> CartridgePolicy.stagedName(md, shaValue, "game.md"));
    for (String name : new String[] {"game.32x", "game.iso"})
      assertFalse(CartridgePolicy.formatReason("megadrive", name).isEmpty());
    assertFalse(CartridgePolicy.formatReason("pce", "game.sgx").isEmpty());
    assertFalse(CartridgePolicy.isStagedName(CartridgePolicy.spec("gb"), "ro-gbc-" + sha + ".gb"));
    assertFalse(CartridgePolicy.isStagedName(null, "game.gb"));
  }

  @Test
  public void gameBoyLengthChecksumAndColorOnlyFlagsAreCheckedWithoutChangingBytes()
      throws Exception {
    byte[] gb = gameBoy(false);
    acceptUnchanged("gb", gb);
    acceptUnchanged("gbc", gb);
    byte[] gbc = gameBoy(true);
    acceptUnchanged("gbc", gbc);
    rejectUnchanged("gb", gbc, "仅适用于 GBC");
    rejectUnchanged("gb", Arrays.copyOf(gb, gb.length - 1), "长度");
    gb[0x148] = 1;
    gbChecksum(gb);
    rejectUnchanged("gb", gb, "容量");
    gb[0x148] = 0;
    gbChecksum(gb);
    gb[0x134] ^= 1;
    rejectUnchanged("gb", gb, "头部");
  }

  @Test
  public void gbaChecksFixedByteAndHeaderChecksumButDoesNotRequireProprietaryLogo()
      throws Exception {
    byte[] gba = gba();
    acceptUnchanged("gba", gba);
    rejectUnchanged("gba", Arrays.copyOf(gba, 191), "大小");
    rejectUnchanged("gba", Arrays.copyOf(gba, 192), "长度");
    rejectUnchanged("gba", Arrays.copyOf(gba, gba.length - 1), "长度");
    gba[0xb2] = 0;
    rejectUnchanged("gba", gba, "固定字节");
    gba[0xb2] = (byte) 0x96;
    gba[0xbd] ^= 1;
    rejectUnchanged("gba", gba, "头部校验");
  }

  @Test
  public void obviousForeignImagesCannotBeEnabledByRenamingTheExtension() throws Exception {
    rejectUnchanged("gba", gameBoy(false), "GB / GBC");
    rejectUnchanged("gb", gba(), "GBA");
    rejectUnchanged("pce", md(true), "MD 平台");
    for (String magic :
        new String[] {
          "PK\003\004",
          "PK\005\006",
          "7z\274\257\047\034",
          "Rar!",
          "MComprHD",
          "NES\032",
          "FDS\032",
          "\177ELF",
          "SEGADISCSYSTEM"
        }) {
      byte[] content = new byte[32768];
      put(content, 0, magic);
      rejectUnchanged("snes", content, "其他平台或光盘");
    }
    byte[] html = new byte[32768];
    put(html, 0, "  \n<html><title>Download expired</title>");
    rejectUnchanged("snes", html, "网页");
  }

  @Test
  public void mdAllowsOriginalHomebrewButRejectsTruncatedPayloadAndOtherSegaSystems()
      throws Exception {
    acceptUnchanged("megadrive", md(false));
    byte[] commercialHeader = md(true);
    acceptUnchanged("megadrive", commercialHeader);
    rejectUnchanged("megadrive", Arrays.copyOf(commercialHeader, 1024), "范围超出");
    rejectUnchanged("megadrive", Arrays.copyOf(commercialHeader, 1023), "长度");
    for (String platform : new String[] {"SEGA 32X", "SEGA MARS", "SEGA PICO"}) {
      byte[] foreign = md(true);
      put(foreign, 0x100, platform);
      rejectUnchanged("megadrive", foreign, "32X / PICO");
    }
    byte[] vectors = md(false);
    put32(vectors, 4, vectors.length);
    rejectUnchanged("megadrive", vectors, "复位向量");
  }

  @Test
  public void smdReadsOnlyLogicalHeaderAndKeepsCopierHeaderAndInterleavedPayload()
      throws Exception {
    byte[] normal = Arrays.copyOf(md(true), 16384);
    put32(normal, 0x1a4, normal.length - 1);
    byte[] smd = new byte[normal.length + 512];
    // Copier signatures vary; Genesis Plus GX uses length and the interleaved logical header.
    smd[8] = (byte) 0xaa;
    smd[9] = (byte) 0xbb;
    smd[10] = 6;
    for (int i = 0; i < 8192; i++) {
      smd[512 + i] = normal[i * 2 + 1];
      smd[512 + 8192 + i] = normal[i * 2];
    }
    acceptUnchanged("megadrive", smd);
    smd[8] = 0;
    acceptUnchanged("megadrive", smd);
    rejectUnchanged("megadrive", Arrays.copyOf(smd, smd.length - 1), "长度");
    smd[512 + 8192 + 2] = 0x7f;
    rejectUnchanged("megadrive", smd, "复位向量");
  }

  @Test
  public void snesAcceptsHeaderedLoRomAndHiRomAndOriginalHeaderlessPrograms() throws Exception {
    byte[] lorom = snes(32768, 0x7fc0, 0x20);
    acceptUnchanged("snes", lorom);
    byte[] copier = new byte[lorom.length + 512];
    System.arraycopy(lorom, 0, copier, 512, lorom.length);
    acceptUnchanged("snes", copier);
    acceptUnchanged("snes", snes(65536, 0xffc0, 0x21));
    byte[] headerless = snes(32768, 0x7fc0, 0);
    acceptUnchanged("snes", headerless);
    rejectUnchanged("snes", Arrays.copyOf(lorom, lorom.length - 1), "长度");
    rejectUnchanged("snes", new byte[32768], "复位向量");
    put(lorom, 0, "BANDAI SFC-ADX");
    rejectUnchanged("snes", lorom, "Sufami Turbo");
  }

  @Test
  public void smsAndGgHeaderRegionsAndDeclaredPayloadDoNotCrossPlatforms() throws Exception {
    byte[] sms = sega8(false);
    byte[] gg = sega8(true);
    acceptUnchanged("mastersystem", sms);
    acceptUnchanged("gamegear", gg);
    rejectUnchanged("mastersystem", gg, "平台标识");
    rejectUnchanged("gamegear", sms, "平台标识");
    byte[] headered = new byte[sms.length + 512];
    System.arraycopy(sms, 0, headered, 512, sms.length);
    acceptUnchanged("mastersystem", headered);
    sms[0x7fff] = 0x4e;
    rejectUnchanged("mastersystem", sms, "容量");
    rejectUnchanged("gamegear", Arrays.copyOf(gg, gg.length - 1), "长度");
    byte[] original = new byte[8192];
    original[0] = (byte) 0xf3;
    acceptUnchanged("mastersystem", original);
    acceptUnchanged("gamegear", original);
  }

  @Test
  public void pceHasNoUniversalSignatureButRequiresBoundedBanksAndResetVector() throws Exception {
    byte[] pce = new byte[8192];
    pce[0] = 0x78;
    pce[0x1ffe] = 0;
    pce[0x1fff] = (byte) 0xe0;
    acceptUnchanged("pce", pce);
    byte[] copier = new byte[pce.length + 512];
    System.arraycopy(pce, 0, copier, 512, pce.length);
    acceptUnchanged("pce", copier);
    rejectUnchanged("pce", Arrays.copyOf(pce, pce.length - 1), "长度");
    pce[0x1fff] = 0;
    rejectUnchanged("pce", pce, "复位向量");
    rejectUnchanged("pce", new byte[8192], "空白");
  }

  @Test
  public void oversizeMissingAndNonFileInputsFailBeforeReadingRomData() throws Exception {
    for (CartridgePolicy.Spec value : CartridgePolicy.all()) {
      File sparse = temporary.newFile();
      try (RandomAccessFile out = new RandomAccessFile(sparse, "rw")) {
        out.setLength(value.maxBytes + 1);
      }
      IOException error =
          assertThrows(IOException.class, () -> CartridgePolicy.validateFile(value, sparse));
      assertTrue(error.getMessage(), error.getMessage().contains("大小"));
      assertEquals(value.maxBytes + 1, sparse.length());
    }
    CartridgePolicy.Spec value = CartridgePolicy.spec("gba");
    assertThrows(
        IOException.class, () -> CartridgePolicy.validateFile(value, temporary.newFolder()));
    assertThrows(
        IOException.class,
        () -> CartridgePolicy.validateFile(value, new File(temporary.getRoot(), "missing")));
    assertThrows(IOException.class, () -> CartridgePolicy.validateFile(value, null));
    assertThrows(IOException.class, () -> CartridgePolicy.validateFile(null, temporary.newFile()));
  }

  private File content(byte[] data) throws Exception {
    // Storage names may be hashes; the original download name is validated separately.
    File file = temporary.newFile();
    Files.write(file.toPath(), data);
    return file;
  }

  private void acceptUnchanged(String platform, byte[] data) throws Exception {
    File file = content(data);
    CartridgePolicy.validateFile(CartridgePolicy.spec(platform), file);
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
  }

  private void rejectUnchanged(String platform, byte[] data, String reason) throws Exception {
    File file = content(data);
    IOException error =
        assertThrows(
            IOException.class,
            () -> CartridgePolicy.validateFile(CartridgePolicy.spec(platform), file));
    assertTrue(error.getMessage(), error.getMessage().contains(reason));
    assertArrayEquals(data, Files.readAllBytes(file.toPath()));
  }

  private static byte[] gameBoy(boolean colorOnly) {
    byte[] bytes = new byte[32768];
    bytes[0x100] = (byte) 0xc3;
    bytes[0x102] = 0x01;
    put(bytes, 0x134, "RETROONE TEST");
    bytes[0x143] = colorOnly ? (byte) 0xc0 : 0;
    gbChecksum(bytes);
    return bytes;
  }

  private static void gbChecksum(byte[] bytes) {
    int sum = 0;
    for (int i = 0x134; i <= 0x14c; i++) sum = (sum - (bytes[i] & 255) - 1) & 255;
    bytes[0x14d] = (byte) sum;
  }

  private static byte[] gba() {
    byte[] bytes = new byte[512];
    bytes[3] = (byte) 0xea;
    put(bytes, 0xa0, "RETROONE");
    bytes[0xb2] = (byte) 0x96;
    int sum = 0x19;
    for (int i = 0xa0; i <= 0xbc; i++) sum += bytes[i] & 255;
    bytes[0xbd] = (byte) -sum;
    return bytes;
  }

  private static byte[] md(boolean segaHeader) {
    byte[] bytes = new byte[2048];
    put32(bytes, 0, 0xff0000);
    put32(bytes, 4, 512);
    if (segaHeader) {
      put(bytes, 0x100, "SEGA MEGA DRIVE");
      put32(bytes, 0x1a4, bytes.length - 1);
    }
    return bytes;
  }

  private static byte[] snes(int size, int header, int mapMode) {
    byte[] bytes = new byte[size];
    bytes[header + 0x15] = (byte) mapMode;
    bytes[header + 0x3d] = (byte) 0x80;
    bytes[header == 0x7fc0 ? 0 : 0x8000] = 0x78;
    return bytes;
  }

  private static byte[] sega8(boolean gameGear) {
    byte[] bytes = new byte[32768];
    bytes[0] = (byte) 0xf3;
    put(bytes, 0x7ff0, "TMR SEGA");
    bytes[0x7fff] = gameGear ? (byte) 0x6c : 0x4c;
    return bytes;
  }

  private static void put(byte[] bytes, int offset, String text) {
    byte[] encoded = text.getBytes(StandardCharsets.ISO_8859_1);
    System.arraycopy(encoded, 0, bytes, offset, encoded.length);
  }

  private static void put32(byte[] bytes, int offset, long value) {
    for (int i = 3; i >= 0; i--) {
      bytes[offset + i] = (byte) value;
      value >>>= 8;
    }
  }
}
