package io.retroone.android;

import static org.junit.Assert.*;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.file.Files;
import java.security.MessageDigest;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class DolphinImageTest {
  private static final int H2 = 0x48;
  private static final int RAW = 512;
  private static final int GROUP = 768;

  @Rule public final TemporaryFolder temporary = new TemporaryFolder();

  private File raw() throws Exception {
    byte[] bytes = new byte[DolphinImage.MIN_GC_BYTES];
    put(bytes, 0x1c, 0xc2339f3dL, 4);
    return write(bytes);
  }

  /**
   * Outer-container fixtures with no commercial content. Compressed table payloads deliberately
   * are not valid Zstd streams: acceptance proves only the documented outer screening, never
   * decompression, filesystem validation, NKit exclusion inside a container, or bootability.
   */
  private File rvz() throws Exception {
    byte[] bytes = new byte[1024];
    put(bytes, 0, 0x52565a01L, 4);
    put(bytes, 4, 0x01000000, 4);
    put(bytes, 8, 0x00030000, 4);
    put(bytes, 0x0c, 0xdc, 4);
    put(bytes, 0x24, 1024 * 1024, 8);
    put(bytes, 0x2c, bytes.length, 8);
    put(bytes, H2, 1, 4);
    put(bytes, H2 + 4, 5, 4);
    put(bytes, H2 + 0x0c, 128 * 1024, 4);
    put(bytes, H2 + 0x10 + 0x1c, 0xc2339f3dL, 4);
    // Empty GC partition table, including zero entry size and offset, is intentional.
    System.arraycopy(hash(new byte[0], 0, 0), 0, bytes, H2 + 0xa0, 20);
    put(bytes, H2 + 0xb4, 1, 4);
    put(bytes, H2 + 0xb8, RAW, 8);
    put(bytes, H2 + 0xc0, 4, 4);
    put(bytes, H2 + 0xc4, 1, 4);
    put(bytes, H2 + 0xc8, GROUP, 8);
    put(bytes, H2 + 0xd0, 4, 4);
    File file = write(bytes);
    seal(file);
    return file;
  }

  private File write(byte[] bytes) throws Exception {
    File file = temporary.newFile();
    Files.write(file.toPath(), bytes);
    return file;
  }

  private static void put(byte[] bytes, int offset, long value, int width) {
    for (int i = width - 1; i >= 0; i--) {
      bytes[offset + i] = (byte) value;
      value >>>= 8;
    }
  }

  private static long get(byte[] bytes, int offset, int width) {
    long value = 0;
    for (int i = 0; i < width; i++) value = (value << 8) | (bytes[offset + i] & 255L);
    return value;
  }

  private static byte[] hash(byte[] bytes, int offset, int length) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-1");
    digest.update(bytes, offset, length);
    return digest.digest();
  }

  private static void seal(File file) throws Exception {
    byte[] bytes = Files.readAllBytes(file.toPath());
    long length = get(bytes, 0x0c, 4);
    if (length <= bytes.length - H2)
      System.arraycopy(hash(bytes, H2, (int) length), 0, bytes, 0x10, 20);
    System.arraycopy(hash(bytes, 0, 0x34), 0, bytes, 0x34, 20);
    Files.write(file.toPath(), bytes);
  }

  private static void patch(File file, int offset, long value, int width) throws Exception {
    byte[] bytes = Files.readAllBytes(file.toPath());
    put(bytes, offset, value, width);
    Files.write(file.toPath(), bytes);
  }

  private static void rvzPatch(File file, int offset, long value, int width) throws Exception {
    patch(file, offset, value, width);
    seal(file);
  }

  private static DolphinImage.Result accept(File file, String platform, String name) throws Exception {
    byte[] before = Files.readAllBytes(file.toPath());
    long modified = file.lastModified();
    DolphinImage.Result result = DolphinImage.inspect(file, platform, name);
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
    assertEquals(modified, file.lastModified());
    return result;
  }

  private static void reject(File file, String name, String reason) throws Exception {
    byte[] before = Files.readAllBytes(file.toPath());
    long modified = file.lastModified();
    IOException failure = assertThrows(IOException.class, () -> DolphinImage.inspect(file, "gc", name));
    assertTrue(failure.getMessage(), failure.getMessage().contains(reason));
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
    assertEquals(modified, file.lastModified());
  }

  @Test
  public void onlyGcAliasesAndThreeStandaloneExtensionsAreCandidates() {
    for (String platform : new String[] {"gc", "gamecube", " GameCube ", "GC"}) {
      assertTrue(DolphinImage.supports(platform));
      for (String name : new String[] {"测试.iso", "游戏.GCM", "Animal Crossing.RvZ"})
        assertEquals("", DolphinImage.formatReason(platform, name));
    }
    for (String platform : new String[] {"wii", "wiiu", "dolphin", "n64", "", null}) {
      assertFalse(DolphinImage.supports(platform));
      assertFalse(DolphinImage.formatReason(platform, "game.iso").isEmpty());
    }
    for (String name : new String[] {"g.wia", "g.wbfs", "g.wbf1", "g.wad", "g.zip", "g.m3u", "g.dol"})
      assertFalse(name, DolphinImage.formatReason("gc", name).isEmpty());
  }

  @Test
  public void unsafeNamesExplicitNkitAndSplitNamesNeverBecomeCandidates() {
    for (String name : new String[] {
      null, "", ".iso", ".gcm", ".rvz", "../g.iso", "dir/g.iso", "dir\\g.iso", "x:g.iso",
      "g\n.iso", "g\u0085.iso", "g.iso\0", "a".repeat(1025) + ".iso"
    }) assertFalse(String.valueOf(name), DolphinImage.formatReason("gc", name).isEmpty());
    for (String name : new String[] {"game.nkit.iso", "GAME.NKIT.RVZ", "nkit.gcm"})
      assertTrue(name, DolphinImage.formatReason("gc", name).contains("NKit"));
    for (String name : new String[] {"g.part0.iso", "g.PART12.GCM", "g.part1.rvz", "g.part0"})
      assertTrue(name, DolphinImage.formatReason("gc", name).contains("分卷"));
    assertEquals("", DolphinImage.formatReason("gc", "particular.iso"));
  }

  @Test
  public void rawGcAtMinimumBootHeaderSizeIsAcceptedWithoutRetailIdOrCapacityRequirements()
      throws Exception {
    File file = raw();
    for (String name : new String[] {"homebrew.iso", "homebrew.GCM"}) {
      DolphinImage.Result result = accept(file, " gamecube ", name);
      assertEquals(name.endsWith("GCM") ? "gcm" : "iso", result.format);
      assertEquals("gc", result.platform);
      assertEquals(DolphinImage.MIN_GC_BYTES, result.logicalSize);
    }
  }

  @Test
  public void physicalAndLogicalGameCubeSizeBudgetsHaveRealSparseFileBoundaries() throws Exception {
    File file = raw();
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.setLength(DolphinImage.MAX_BYTES);
    }
    assertEquals(DolphinImage.MAX_BYTES, DolphinImage.inspect(file, "gc", "g.iso").logicalSize);
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.setLength(DolphinImage.MAX_BYTES + 1);
    }
    IOException failure = assertThrows(IOException.class, () -> DolphinImage.inspect(file, "gc", "g.iso"));
    assertTrue(failure.getMessage().contains("2 GiB"));
    assertEquals(DolphinImage.MAX_BYTES + 1, file.length());
    File rvz = rvz();
    rvzPatch(rvz, 0x24, DolphinImage.MAX_BYTES, 8);
    assertEquals(DolphinImage.MAX_BYTES, accept(rvz, "gc", "g.rvz").logicalSize);
    rvzPatch(rvz, 0x24, DolphinImage.MAX_BYTES + 1, 8);
    reject(rvz, "g.rvz", "逻辑大小");
  }

  @Test
  public void missingNonRegularAndTruncatedFilesAreRejected() throws Exception {
    assertThrows(IOException.class, () -> DolphinImage.inspect(null, "gc", "g.iso"));
    assertThrows(IOException.class, () -> DolphinImage.inspect(temporary.newFolder(), "gc", "g.iso"));
    for (int length : new int[] {0, 71, DolphinImage.MIN_GC_BYTES - 1}) {
      byte[] bytes = new byte[length];
      if (length >= 32) put(bytes, 0x1c, 0xc2339f3dL, 4);
      assertThrows(IOException.class, () -> DolphinImage.inspect(write(bytes), "gc", "g.iso"));
    }
  }

  @Test
  public void wiiAndAmbiguousHeadersAreRejectedEvenWhenTheNameClaimsGc() throws Exception {
    File file = raw();
    patch(file, 0x18, 0x5d1c9ea3, 4); // both GC and Wii markers: Wii must not be admitted
    reject(file, "g.iso", "Wii");
    patch(file, 0x1c, 0, 4);
    reject(file, "g.gcm", "Wii");
    File rvz = rvz();
    rvzPatch(rvz, H2 + 0x10 + 0x18, 0x5d1c9ea3, 4);
    reject(rvz, "g.rvz", "Wii");
    rvzPatch(rvz, 0x24, 4699979776L, 8);
    reject(rvz, "g.rvz", "Wii"); // distinguish Wii even when its logical size exceeds the GC cap
  }

  @Test
  public void rawNkitIsDetectedByBytesAndExplicitNamesAreRejectedBeforeReading() throws Exception {
    File file = raw();
    patch(file, 0x200, 0x4e4b4954, 4);
    reject(file, "innocent.iso", "NKIT");
    reject(raw(), "g.nkit.gcm", "NKit");
    reject(rvz(), "g.part0.rvz", "分卷");
  }

  @Test
  public void renamedContainersAndInvalidPlatformMagicDoNotPass() throws Exception {
    for (long magic : new long[] {0x52565a01, 0x57494101, 0x57424653}) {
      File file = raw();
      patch(file, 0, magic, 4);
      reject(file, "renamed.iso", "容器签名");
    }
    File raw = raw();
    patch(raw, 0x1c, 0, 4);
    reject(raw, "g.gcm", "GameCube 光盘头");
    reject(raw(), "g.rvz", "RVZ 签名");
    File rvz = rvz();
    rvzPatch(rvz, 0, 0x57494101, 4);
    reject(rvz, "g.rvz", "RVZ 签名");
  }

  @Test
  public void rvzOuterScreeningIsReadOnlyAndDoesNotPretendToDecodeThePayload() throws Exception {
    File file = rvz();
    patch(file, RAW, 0x4e4b4954, 4); // physical 0x200 is not logical 0x200 in RVZ
    DolphinImage.Result result = accept(file, "gc", "g.rvz");
    assertEquals("rvz", result.format);
    assertEquals("gc", result.platform);
    assertEquals(1024 * 1024, result.logicalSize);
  }

  @Test
  public void rvzHeaderHashesCoverBothHeadersAndUnusedTailBytes() throws Exception {
    File first = rvz();
    patch(first, 0x24, 12345, 8);
    reject(first, "g.rvz", "Header 1 SHA-1");
    File second = rvz();
    patch(second, H2 + 0xdb, 1, 1);
    reject(second, "g.rvz", "Header 2 SHA-1");
  }

  @Test
  public void declaredPhysicalSizeMustMatchTheWholeFile() throws Exception {
    for (long declared : new long[] {0, 1023, 1025, Long.MAX_VALUE, Long.MIN_VALUE}) {
      File file = rvz();
      rvzPatch(file, 0x2c, declared, 8);
      reject(file, "g.rvz", declared == Long.MIN_VALUE ? "64 位" : "物理大小");
    }
    File file = rvz();
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.setLength(1023);
    }
    reject(file, "g.rvz", "物理大小");
  }

  @Test
  public void logicalSizeRejectsZeroTinyOverflowAndUnsignedHighBit() throws Exception {
    for (long length : new long[] {0, DolphinImage.MIN_GC_BYTES - 1, Long.MAX_VALUE, Long.MIN_VALUE}) {
      File file = rvz();
      rvzPatch(file, 0x24, length, 8);
      reject(file, "g.rvz", length == Long.MIN_VALUE ? "64 位" : "逻辑大小");
    }
  }

  @Test
  public void versionCompatibilityUsesThePinnedReaderWindow() throws Exception {
    for (long[] pair : new long[][] {{0x20000, 0x20000}, {0x1000000, 0x1000001}, {0x30000, 0x40000}}) {
      File file = rvz();
      rvzPatch(file, 4, pair[0], 4);
      rvzPatch(file, 8, pair[1], 4);
      reject(file, "g.rvz", "版本");
    }
    File futureCompatible = rvz();
    rvzPatch(futureCompatible, 4, 0x02000000, 4);
    rvzPatch(futureCompatible, 8, 0x01000000, 4);
    accept(futureCompatible, "gc", "g.rvz");
  }

  @Test
  public void header2LengthIsBudgetedBeforeAllocationAndCheckedAgainstPhysicalFile() throws Exception {
    for (long length : new long[] {0, 0xd4, 4097, 0xffffffffL}) {
      File file = rvz();
      rvzPatch(file, 0xc, length, 4);
      reject(file, "g.rvz", "Header 2 长度");
    }
    File truncated = rvz();
    rvzPatch(truncated, 0xc, 1000, 4);
    reject(truncated, "g.rvz", "Header 2的偏移");
    File minimum = rvz();
    rvzPatch(minimum, 0xc, 0xd5, 4);
    accept(minimum, "gc", "g.rvz");
  }

  @Test
  public void rvzDiscTypeAndPartitionsCannotSmuggleWiiIntoGc() throws Exception {
    File unknownType = rvz();
    rvzPatch(unknownType, H2, 0, 4);
    accept(unknownType, "gc", "g.rvz"); // GC magic still establishes the platform
    for (long type : new long[] {2, 3, 0xffffffffL}) {
      File file = rvz();
      rvzPatch(file, H2, type, 4);
      reject(file, "g.rvz", type == 2 ? "Wii" : "光盘类型");
    }
    File file = rvz();
    rvzPatch(file, H2 + 0x90, 1, 4);
    reject(file, "g.rvz", "Wii 分区");
  }

  @Test
  public void zeroTablesAndUnusedZeroEntrySizeAreLegalOuterStructures() throws Exception {
    File file = rvz();
    for (int offset : new int[] {0xb4, 0xc0, 0xc4, 0xd0}) rvzPatch(file, H2 + offset, 0, 4);
    rvzPatch(file, H2 + 0xb8, 0, 8);
    rvzPatch(file, H2 + 0xc8, 0, 8);
    accept(file, "gc", "g.rvz"); // no claim that a disc with empty tables can actually boot
    rvzPatch(file, H2 + 0x94, 0xffffffffL, 4); // unused partition entry size must not allocate
    accept(file, "gc", "g.rvz");
    rvzPatch(file, H2 + 0xa0, 0, 1);
    reject(file, "g.rvz", "空分区表 SHA-1");
  }

  @Test
  public void chunkZeroTooSmallNonPowerAndOversizeAreRealRejectedHeaders() throws Exception {
    for (long chunk : new long[] {0, 16384, 32769, 3 * 1024 * 1024, 0xffffffffL}) {
      File file = rvz();
      rvzPatch(file, H2 + 0xc, chunk, 4);
      reject(file, "g.rvz", "chunk");
    }
    for (long chunk : new long[] {32768, DolphinImage.MAX_CHUNK_BYTES}) {
      File file = rvz();
      rvzPatch(file, H2 + 0xc, chunk, 4);
      accept(file, "gc", "g.rvz");
    }
  }

  @Test
  public void rawAndGroupCountsCannotOverflowOrExceedBudgets() throws Exception {
    for (long[] change : new long[][] {
      {0xb4, DolphinImage.MAX_RAW_ENTRIES + 1L}, {0xb4, 0xffffffffL},
      {0xc4, DolphinImage.MAX_GROUP_ENTRIES + 1L}, {0xc4, 0xffffffffL},
      {0xc0, DolphinImage.MAX_TABLE_BYTES + 1L}, {0xd0, 0xffffffffL}
    }) {
      File file = rvz();
      rvzPatch(file, H2 + (int) change[0], change[1], 4);
      reject(file, "g.rvz", "预算");
    }
    for (int offset : new int[] {0xc0, 0xd0}) {
      File file = rvz();
      rvzPatch(file, H2 + offset, 0, 4);
      reject(file, "g.rvz", "声明非空");
    }
  }

  @Test
  public void tableRangesRejectTruncationUnsignedOffsetsAndHeaderOverlap() throws Exception {
    for (int field : new int[] {0xb8, 0xc8}) {
      for (long offset : new long[] {0, 0x48, 1022, Long.MAX_VALUE, Long.MIN_VALUE}) {
        File file = rvz();
        rvzPatch(file, H2 + field, offset, 8);
        reject(file, "g.rvz", offset == Long.MIN_VALUE ? "64 位" : "偏移或长度");
      }
    }
    File emptyOffset = rvz();
    rvzPatch(emptyOffset, H2 + 0x98, 1025, 8);
    reject(emptyOffset, "g.rvz", "空分区表的偏移");
  }

  @Test
  public void tablesNeedNotBeInAscendingOrderButCannotOverlap() throws Exception {
    File reverse = rvz();
    rvzPatch(reverse, H2 + 0xb8, GROUP, 8);
    rvzPatch(reverse, H2 + 0xc8, RAW, 8);
    accept(reverse, "gc", "g.rvz");
    File overlap = rvz();
    rvzPatch(overlap, H2 + 0xc8, RAW + 2, 8);
    reject(overlap, "g.rvz", "互相重叠");
  }

  @Test
  public void compressionMethodsAndParameterLengthsAreCheckedWithoutDecoding() throws Exception {
    for (long method : new long[] {1, 6, 0xffffffffL}) {
      File file = rvz();
      rvzPatch(file, H2 + 4, method, 4);
      reject(file, "g.rvz", "压缩算法");
    }
    File bzip = rvz();
    rvzPatch(bzip, H2 + 4, 2, 4);
    accept(bzip, "gc", "g.rvz");
    File negativeZstdLevel = rvz();
    rvzPatch(negativeZstdLevel, H2 + 8, 0xffffffffL, 4);
    accept(negativeZstdLevel, "gc", "g.rvz");
    for (int count : new int[] {1, 8, 255}) {
      File file = rvz();
      rvzPatch(file, H2 + 0xd4, count, 1);
      reject(file, "g.rvz", count > 7 ? "参数长度" : "额外参数");
    }
    File truncated = rvz();
    rvzPatch(truncated, 0xc, 0xd5, 4);
    rvzPatch(truncated, H2 + 0xd4, 1, 1);
    reject(truncated, "g.rvz", "参数长度");
  }

  @Test
  public void lzmaAndLzma2DictionaryLimitsAreCheckedBeforeAnyDecoderCouldAllocate() throws Exception {
    File lzma = rvz();
    rvzPatch(lzma, H2 + 4, 3, 4);
    rvzPatch(lzma, H2 + 0xd4, 5, 1);
    rvzPatch(lzma, H2 + 0xd5, 0x5d, 1);
    rvzPatch(lzma, H2 + 0xd6, 0x00000001, 4); // little-endian dictionary = 16 MiB
    accept(lzma, "gc", "g.rvz");
    rvzPatch(lzma, H2 + 0xd6, 0x00000002, 4);
    reject(lzma, "g.rvz", "LZMA 字典");
    rvzPatch(lzma, H2 + 0xd5, 225, 1);
    reject(lzma, "g.rvz", "LZMA 参数");
    File lzma2 = rvz();
    rvzPatch(lzma2, H2 + 4, 4, 4);
    rvzPatch(lzma2, H2 + 0xd4, 1, 1);
    rvzPatch(lzma2, H2 + 0xd5, 24, 1);
    accept(lzma2, "gc", "g.rvz");
    for (int property : new int[] {25, 40, 41}) {
      rvzPatch(lzma2, H2 + 0xd5, property, 1);
      reject(lzma2, "g.rvz", property == 41 ? "LZMA2 参数" : "LZMA2 字典");
    }
  }

  @Test
  public void noneCompressionChecksDeclaredExpandedTableSizes() throws Exception {
    File file = rvz();
    rvzPatch(file, H2 + 4, 0, 4);
    reject(file, "g.rvz", "未压缩长度");
    rvzPatch(file, H2 + 0xc0, 0x18, 4);
    rvzPatch(file, H2 + 0xd0, 0x0c, 4);
    accept(file, "gc", "g.rvz");
  }

  @Test
  public void entryCancellationPreservesTheInterruptFlagAndSource() throws Exception {
    File file = rvz();
    byte[] before = Files.readAllBytes(file.toPath());
    try {
      Thread.currentThread().interrupt();
      IOException failure = assertThrows(IOException.class, () -> DolphinImage.inspect(file, "gc", "g.rvz"));
      assertTrue(failure.getMessage().contains("已取消"));
      assertTrue(Thread.currentThread().isInterrupted());
    } finally {
      Thread.interrupted();
    }
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void cancellationDuringTheFinalIdentityCheckCannotReturnSuccess() throws Exception {
    File original = raw();
    byte[] before = Files.readAllBytes(original.toPath());
    File cancelAtReturn = new File(original.getPath()) {
      private int calls;

      @Override
      public long lastModified() {
        if (++calls == 2) Thread.currentThread().interrupt();
        return super.lastModified();
      }
    };
    try {
      IOException failure = assertThrows(IOException.class, () -> DolphinImage.inspect(cancelAtReturn, "gc", "g.iso"));
      assertTrue(failure.getMessage().contains("已取消"));
      assertTrue(Thread.currentThread().isInterrupted());
    } finally {
      Thread.interrupted();
    }
    assertArrayEquals(before, Files.readAllBytes(original.toPath()));
  }
}
