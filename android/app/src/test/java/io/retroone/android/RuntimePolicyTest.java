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

public class RuntimePolicyTest {
  @Rule public final TemporaryFolder temporary = new TemporaryFolder();

  @Test
  public void fixedRegistryOnlyEnablesTheIntendedSingleFileRuntimes() {
    String[][] aliases = {
      {"n64", "Nintendo - Nintendo 64"},
      {"nds", "Nintendo - Nintendo DS", "Nintendo DS"},
      {"psx", "ps1", "PlayStation", "Sony - PlayStation"},
      {"dreamcast", "dc", "Sega - Dreamcast"},
      {"saturn", "Sega - Saturn", "SEGA Saturn"}
    };
    String[] cores = {
      "mupen64plus_next_gles3", "melondsds", "pcsx_rearmed", "flycast", "mednafen_saturn"
    };
    assertEquals(aliases.length, RuntimePolicy.all().size());
    for (int i = 0; i < aliases.length; i++) {
      RuntimePolicy.Spec spec = RuntimePolicy.spec(aliases[i][0]);
      assertEquals(cores[i] + "_libretro_android.so", spec.core);
      for (String alias : aliases[i]) {
        assertTrue(alias, RuntimePolicy.supports(alias));
        assertSame(spec, RuntimePolicy.spec(" " + alias.toUpperCase() + " "));
      }
    }
    for (String unsupported :
        new String[] {
          "naomi", "naomi2", "nds-dsi", "dsi", "n64dd", "ps2", "psp", "nes", "../nds", "", null
        }) assertFalse(String.valueOf(unsupported), RuntimePolicy.supports(unsupported));
    assertThrows(UnsupportedOperationException.class, () -> RuntimePolicy.all().clear());
    assertThrows(
        UnsupportedOperationException.class,
        () -> RuntimePolicy.spec("nds").extensions.add(".dsi"));
    assertEquals(64L * 1024 * 1024, RuntimePolicy.spec("n64").maxBytes);
    assertEquals(512L * 1024 * 1024, RuntimePolicy.spec("nds").maxBytes);
    for (String platform : new String[] {"psx", "saturn", "dreamcast"}) {
      assertEquals(2L * 1024 * 1024 * 1024, RuntimePolicy.spec(platform).maxBytes);
      assertEquals(0, RuntimePolicy.spec(platform).maxArchiveBytes);
    }
  }

  @Test
  public void archiveEntrypointsAreLimitedToN64AndNdsAndCannotBecomeStagedNames() throws Exception {
    for (RuntimePolicy.Spec spec : RuntimePolicy.all()) {
      boolean cartridge = spec.id.equals("n64") || spec.id.equals("nds");
      assertEquals(cartridge, RuntimePolicy.formatReason(spec.id, "测试.ZIP").isEmpty());
      assertFalse(RuntimePolicy.isRomName(spec, "测试.ZIP"));
      assertThrows(
          IOException.class, () -> RuntimePolicy.stagedName(spec, "a".repeat(64), "game.zip"));
      for (String extension : spec.extensions) {
        assertTrue(RuntimePolicy.isRomName(spec, "测试" + extension.toUpperCase()));
        assertEquals("", RuntimePolicy.formatReason(spec.id, "测试" + extension));
        String generated =
            RuntimePolicy.stagedName(spec, "a".repeat(64), "测试" + extension.toUpperCase());
        assertEquals("ro-" + spec.id + "-" + "a".repeat(64) + extension, generated);
        assertTrue(RuntimePolicy.validStagedName(generated));
      }
    }
    assertFalse(RuntimePolicy.formatReason("naomi", "game.zip").isEmpty());
    for (String value : new String[] {"game.ndd", "game.bin", "game.gb", "game.u1"})
      assertFalse(RuntimePolicy.formatReason("n64", value).isEmpty());
    for (String value : new String[] {"game.ids", "game.dsi", "game.7z", "game.m3u"})
      assertFalse(RuntimePolicy.formatReason("nds", value).isEmpty());
    for (String value : new String[] {"game.cdi", "game.gdi", "game.cue", "game.iso", "game.m3u"})
      assertFalse(RuntimePolicy.formatReason("dreamcast", value).isEmpty());
  }

  @Test
  public void unsafeBasenamesAndUnregisteredStageNamesAreRejected() {
    for (RuntimePolicy.Spec spec : RuntimePolicy.all()) {
      String suffix = spec.extensions.get(0);
      for (String name :
          new String[] {
            null,
            "",
            suffix,
            "../g" + suffix,
            "dir/g" + suffix,
            "C:\\g" + suffix,
            "x:g" + suffix,
            "g\n" + suffix,
            "g\u0085" + suffix,
            "g" + suffix + "\n"
          }) {
        assertFalse(String.valueOf(name), RuntimePolicy.isRomName(spec, name));
        assertFalse(String.valueOf(name), RuntimePolicy.formatReason(spec.id, name).isEmpty());
        assertThrows(IOException.class, () -> RuntimePolicy.stagedName(spec, "a".repeat(64), name));
      }
      for (String sha : new String[] {null, "", "A".repeat(64), "a".repeat(63), "a".repeat(65)})
        assertThrows(IOException.class, () -> RuntimePolicy.stagedName(spec, sha, "game" + suffix));
    }
    for (String name :
        new String[] {
          null,
          "",
          "ro-nds-" + "a".repeat(64) + ".chd",
          "ro-naomi-" + "a".repeat(64) + ".zip",
          "ro-psx-" + "a".repeat(64) + ".nds",
          "ro-n64-" + "a".repeat(64) + ".ZIP",
          "../ro-nds-" + "a".repeat(64) + ".nds",
          "ro-nds-" + "a".repeat(64) + ".nds\n",
          "ro-nds-" + "A".repeat(64) + ".nds"
        }) assertFalse(String.valueOf(name), RuntimePolicy.validStagedName(name));
    assertFalse(RuntimePolicy.isRomName(null, "game.nds"));
    assertThrows(
        IOException.class, () -> RuntimePolicy.stagedName(null, "a".repeat(64), "game.nds"));
  }

  @Test
  public void n64ChecksAllThreeByteOrdersWithoutConvertingTheOriginal() throws Exception {
    for (long magic : new long[] {0x80371240L, 0x37804012L, 0x40123780L}) {
      byte[] rom = new byte[0x1000];
      putBe32(rom, 0, magic);
      acceptUnchanged("n64", rom);
      rejectUnchanged("nds", rom, "N64 平台");
    }
    byte[] bigEndian = new byte[0x1001];
    putBe32(bigEndian, 0, 0x80371240L);
    acceptUnchanged("n64", bigEndian);
    byte[] swapped16 = new byte[0x1002];
    putBe32(swapped16, 0, 0x37804012L);
    acceptUnchanged("n64", swapped16);
    rejectUnchanged("n64", Arrays.copyOf(swapped16, 0x1001), "对齐");
    byte[] swapped32 = new byte[0x1004];
    putBe32(swapped32, 0, 0x40123780L);
    acceptUnchanged("n64", swapped32);
    rejectUnchanged("n64", Arrays.copyOf(swapped32, 0x1002), "对齐");
    rejectUnchanged("n64", new byte[0x1000], "未识别");
    rejectUnchanged("n64", nds(0), "未识别");
    rejectUnchanged("n64", Arrays.copyOf(swapped32, 0xfff), "大小");
  }

  @Test
  public void ndsAcceptsDsAndDualModeAndDoesNotImposeRetailPaddingOrChecksums() throws Exception {
    for (int unit : new int[] {0, 2}) {
      byte[] rom = nds(unit);
      // Trimmed/non-power-of-two content, no logo, no header CRC, capacity larger than actual data.
      rom[0x14] = 9;
      putLe32(rom, 0x80, 32L * 1024 * 1024);
      putLe32(rom, 0x84, 0x200);
      put(rom, 0x0c, "####");
      acceptUnchanged("nds", rom);
      rom[0x15e] = 0x34;
      rom[0x15f] = 0x12;
      acceptUnchanged("nds", rom);
    }
    rejectUnchanged("nds", nds(3), "DSi 专用");
    rejectUnchanged("nds", nds(1), "硬件类型");
    rejectUnchanged("nds", nds(255), "硬件类型");
    rejectUnchanged("nds", new byte[0x1000], "ARM9");
    rejectUnchanged("nds", Arrays.copyOf(nds(0), 0xfff), "大小");
  }

  @Test
  public void ndsExecutableRangesUseActualLengthAndCannotWrapUnsignedFields() throws Exception {
    for (int[] fields : new int[][] {{0x20, 0x2c}, {0x30, 0x3c}}) {
      for (long[] values :
          new long[][] {
            {0, 4},
            {0x1ff, 4},
            {0x200, 0},
            {0xffffffffL, 1},
            {0xfffffff0L, 0x40},
            {0x200, 0xffffffffL},
            {0x1000, 4}
          }) {
        byte[] rom = nds(0);
        putLe32(rom, fields[0], values[0]);
        putLe32(rom, fields[1], values[1]);
        rejectUnchanged("nds", rom, "段范围");
      }
      byte[] boundary = nds(0);
      putLe32(boundary, fields[0], boundary.length - 1);
      putLe32(boundary, fields[1], 1);
      acceptUnchanged("nds", boundary);
    }
    byte[] rom = nds(0);
    putLe32(rom, 0x30, rom.length - 2);
    putLe32(rom, 0x3c, 2);
    acceptUnchanged("nds", rom);
    rejectUnchanged("nds", Arrays.copyOf(rom, rom.length - 1), "ARM7");
  }

  @Test
  public void ndsOptionalTablesMayBeAbsentButNonemptyTablesMustFit() throws Exception {
    for (int offset : new int[] {0x40, 0x48, 0x50, 0x58}) {
      byte[] rom = nds(0);
      putLe32(rom, offset, 0xffffffffL);
      acceptUnchanged("nds", rom); // An unused offset with a zero length is not dereferenced.
      putLe32(rom, offset, rom.length - 8);
      putLe32(rom, offset + 4, 8);
      acceptUnchanged("nds", rom);
      putLe32(rom, offset + 4, 9);
      rejectUnchanged("nds", rom, "段范围");
      putLe32(rom, offset, 0xfffffff0L);
      putLe32(rom, offset + 4, 0x40);
      rejectUnchanged("nds", rom, "段范围");
    }
  }

  @Test
  public void explicitForeignSignaturesCannotPassByKeepingPlausibleNdsSegments() throws Exception {
    for (String magic :
        new String[] {
          "PK\003\004",
          "PK\005\006",
          "PK\007\010",
          "7z\274\257\047\034",
          "Rar!",
          "MComprHD",
          "NES\032",
          "FDS\032",
          "\177ELF",
          "SEGADISCSYSTEM",
          "SEGA SEGASATURN",
          "SEGA SEGAKATANA"
        }) {
      byte[] disguised = nds(0);
      put(disguised, 0, magic);
      rejectUnchanged("nds", disguised, "其他平台");
    }
    byte[] md = nds(0);
    put(md, 0x100, "SEGA");
    rejectUnchanged("nds", md, "其他平台");
    byte[] html = nds(0);
    put(html, 0, " <html>");
    rejectUnchanged("nds", html, "网页");
    for (int prefix : new int[] {0, 512}) {
      byte[] sega = Arrays.copyOf(nds(0), 32768 + prefix);
      put(sega, 0x7ff0 + prefix, "TMR SEGA");
      rejectUnchanged("nds", sega, "SMS / GG");
    }
    byte[] smd = Arrays.copyOf(nds(0), 16384 + 512);
    smd[512 + 8192 + 0x80] = 'S';
    smd[512 + 0x80] = 'E';
    smd[512 + 8192 + 0x81] = 'G';
    smd[512 + 0x81] = 'A';
    rejectUnchanged("nds", smd, "交织 MD / SMD");
  }

  @Test
  public void physicalLimitsAreCheckedBeforeParsingWithoutAllocatingTheRom() throws Exception {
    for (String platform : new String[] {"n64", "nds"}) {
      RuntimePolicy.Spec spec = RuntimePolicy.spec(platform);
      File file = temporary.newFile();
      try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
        byte[] header = nds(0);
        if (platform.equals("n64")) putBe32(header, 0, 0x80371240L);
        out.write(header);
        out.setLength(spec.maxBytes);
        RuntimePolicy.validateFile(spec, file);
        out.setLength(spec.maxBytes + 1);
      }
      IOException error =
          assertThrows(IOException.class, () -> RuntimePolicy.validateFile(spec, file));
      assertTrue(error.getMessage(), error.getMessage().contains("大小"));
      assertEquals(spec.maxBytes + 1, file.length());
    }
    for (String platform : new String[] {"psx", "saturn", "dreamcast"}) {
      File file = temporary.newFile();
      try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
        out.setLength(RuntimePolicy.spec(platform).maxBytes + 1);
      }
      IOException error =
          assertThrows(
              IOException.class,
              () -> RuntimePolicy.validateFile(RuntimePolicy.spec(platform), file));
      assertTrue(error.getMessage(), error.getMessage().contains("大小"));
    }
  }

  @Test
  public void discEntrypointsDelegateFormatValidationAndNeverAcceptRawCartridges()
      throws Exception {
    for (String platform : new String[] {"psx", "saturn", "dreamcast"}) {
      File raw = temporary.newFile();
      byte[] original = nds(0);
      Files.write(raw.toPath(), original);
      assertThrows(
          IOException.class, () -> RuntimePolicy.validateFile(RuntimePolicy.spec(platform), raw));
      assertArrayEquals(original, Files.readAllBytes(raw.toPath()));
    }
  }

  @Test
  public void allDiscRuntimesAllowCdButOnlyDreamcastAllowsGdMetadata() throws Exception {
    for (String platform : new String[] {"psx", "saturn", "dreamcast"}) {
      acceptUnchanged(platform, chd(false));
    }
    acceptUnchanged("dreamcast", chd(true));
    for (String platform : new String[] {"psx", "saturn"}) {
      File gd = temporary.newFile();
      byte[] original = chd(true);
      Files.write(gd.toPath(), original);
      assertThrows(
          IOException.class, () -> RuntimePolicy.validateFile(RuntimePolicy.spec(platform), gd));
      assertArrayEquals(original, Files.readAllBytes(gd.toPath()));
    }
  }

  @Test
  public void missingInputsAndCancellationFailBeforeReturningSuccess() throws Exception {
    RuntimePolicy.Spec spec = RuntimePolicy.spec("nds");
    assertThrows(IOException.class, () -> RuntimePolicy.validateFile(null, temporary.newFile()));
    assertThrows(IOException.class, () -> RuntimePolicy.validateFile(spec, null));
    assertThrows(IOException.class, () -> RuntimePolicy.validateFile(spec, temporary.newFolder()));
    assertThrows(
        IOException.class,
        () -> RuntimePolicy.validateFile(spec, new File(temporary.getRoot(), "missing")));
    File file = temporary.newFile();
    Files.write(file.toPath(), nds(0));
    try {
      Thread.currentThread().interrupt();
      IOException error =
          assertThrows(IOException.class, () -> RuntimePolicy.validateFile(spec, file));
      assertTrue(error.getMessage(), error.getMessage().contains("取消"));
      assertTrue(Thread.currentThread().isInterrupted());
    } finally {
      Thread.interrupted();
    }
  }

  private void acceptUnchanged(String platform, byte[] bytes) throws Exception {
    File file = temporary.newFile();
    Files.write(file.toPath(), bytes);
    RuntimePolicy.validateFile(RuntimePolicy.spec(platform), file);
    assertArrayEquals(bytes, Files.readAllBytes(file.toPath()));
  }

  private void rejectUnchanged(String platform, byte[] bytes, String reason) throws Exception {
    File file = temporary.newFile();
    Files.write(file.toPath(), bytes);
    IOException error =
        assertThrows(
            IOException.class,
            () -> RuntimePolicy.validateFile(RuntimePolicy.spec(platform), file));
    assertTrue(error.getMessage(), error.getMessage().contains(reason));
    assertArrayEquals(bytes, Files.readAllBytes(file.toPath()));
  }

  private static byte[] nds(int unit) {
    byte[] rom = new byte[0x1003];
    rom[0x12] = (byte) unit;
    putLe32(rom, 0x20, 0x200);
    putLe32(rom, 0x2c, 0x100);
    putLe32(rom, 0x30, 0x300);
    putLe32(rom, 0x3c, 0x100);
    return rom;
  }

  private static byte[] chd(boolean gd) {
    String metadata =
        "TRACK:1 TYPE:MODE1_RAW SUBTYPE:NONE FRAMES:4 "
            + (gd ? "PAD:0 " : "")
            + "PREGAP:0 PGTYPE:MODE1 PGSUB:NONE POSTGAP:0\0";
    byte[] value = new byte[144 + metadata.length()];
    put(value, 0, "MComprHD");
    putBe32(value, 8, 124);
    putBe32(value, 12, 5);
    putBe32(value, 36, 2448 * 4); // Logical size (low half of uint64).
    putBe32(value, 44, 124); // Map offset (low half of uint64).
    putBe32(value, 52, 128); // Metadata offset (low half of uint64).
    putBe32(value, 56, 2448 * 4);
    putBe32(value, 60, 2448);
    // One uncompressed sparse-zero hunk; uint32 map entry at 124 is zero.
    put(value, 128, gd ? "CHGD" : "CHT2");
    putBe32(value, 132, 0x01000000L | metadata.length());
    put(value, 144, metadata);
    return value;
  }

  private static void put(byte[] bytes, int offset, String text) {
    byte[] value = text.getBytes(StandardCharsets.ISO_8859_1);
    System.arraycopy(value, 0, bytes, offset, value.length);
  }

  private static void putLe32(byte[] bytes, int offset, long value) {
    for (int i = 0; i < 4; i++) bytes[offset + i] = (byte) (value >>> (i * 8));
  }

  private static void putBe32(byte[] bytes, int offset, long value) {
    for (int i = 0; i < 4; i++) bytes[offset + i] = (byte) (value >>> ((3 - i) * 8));
  }
}
