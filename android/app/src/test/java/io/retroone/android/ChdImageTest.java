package io.retroone.android;

import static org.junit.Assert.*;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class ChdImageTest {
  private static final int FRAME = 2448;
  private static final int HUNK = FRAME * 4;
  private static final long META = 128;
  private static final long COMPRESSED_MAP = 512;
  private static final String TRACK = "TRACK:1 TYPE:MODE1_RAW SUBTYPE:NONE FRAMES:4";
  private static final String GAPS = " PREGAP:0 PGTYPE:MODE1 PGSUB:NONE POSTGAP:0";

  @Rule public TemporaryFolder temporary = new TemporaryFolder();

  /**
   * Synthetic structural fixtures, containing no downloaded ROM. The compressed fixture's map
   * payload deliberately is not a usable Huffman stream: this policy must not claim codec, map CRC,
   * full-hunk or game verification merely because the enclosing structure passes.
   */
  private File fixture(boolean compressed, String tag) throws Exception {
    String track = TRACK;
    if (tag.equals("CHGD") || tag.equals("CHGT")) track += " PAD:0";
    if (!tag.equals("CHTR")) track += GAPS;
    return fixture(compressed, tag, text(track));
  }

  private File fixture(boolean compressed, String tag, byte[] metadata) throws Exception {
    File file = temporary.newFile();
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.write("MComprHD".getBytes(StandardCharsets.US_ASCII));
      out.writeInt(124);
      out.writeInt(5);
      out.writeInt(compressed ? 0x7a6c6962 : 0); // zlib, followed by empty codec slots
      out.write(new byte[12]);
      out.writeLong(HUNK);
      out.writeLong(compressed ? COMPRESSED_MAP : 124);
      out.writeLong(META);
      out.writeInt(HUNK);
      out.writeInt(FRAME);
      out.write(new byte[60]); // zero hashes are valid for uncompressed sparse images
      out.writeInt(0); // uncompressed sparse zero-hunk map
    }
    metadata(file, META, tag, metadata, 0);
    if (compressed) {
      try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
        out.seek(256);
        out.write(42);
        out.seek(COMPRESSED_MAP);
        out.writeInt(8);
        out.write(new byte[] {0, 0, 0, 0, 1, 0}); // first data at 256
        out.writeShort(0); // not a verified map CRC
        out.write(new byte[] {0, 0, 0, 0}); // zero bit widths are permitted
        out.write(new byte[8]);
      }
    }
    return file;
  }

  private static byte[] text(String value) {
    return (value + "\0").getBytes(StandardCharsets.US_ASCII);
  }

  private static void patch(File file, long offset, long value, int bytes) throws Exception {
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.seek(offset);
      for (int i = bytes - 1; i >= 0; i--) out.write((int) (value >>> (i * 8)) & 255);
    }
  }

  private static void length(File file, long length) throws Exception {
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.setLength(length);
    }
  }

  private static void metadata(File file, long offset, String tag, byte[] payload, long next)
      throws Exception {
    try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
      out.seek(offset);
      out.write(tag.getBytes(StandardCharsets.US_ASCII));
      out.writeInt(0x01000000 | payload.length);
      out.writeLong(next);
      out.write(payload);
    }
  }

  private static void rejects(File file, boolean gd, String reason) throws Exception {
    byte[] before = Files.readAllBytes(file.toPath());
    try {
      ChdImage.validate(file, gd);
      fail("Expected rejection: " + reason);
    } catch (IOException expected) {
      assertTrue(expected.getMessage(), expected.getMessage().contains(reason));
    }
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void sparseUncompressedCdIsReadOnlyAndNeedNotOccupyItsLogicalSize() throws Exception {
    File file = fixture(false, "CHT2");
    byte[] before = Files.readAllBytes(file.toPath());
    assertTrue(file.length() < HUNK);
    ChdImage.validate(file, false);
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void compressedStructureDoesNotPretendToValidateMapOrHunkContents() throws Exception {
    File file = fixture(true, "CHT2");
    byte[] before = Files.readAllBytes(file.toPath());
    ChdImage.validate(file, false);
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void ordinaryUncompressedMappedHunkAndPaddedLastHunkAreAccepted() throws Exception {
    File file = fixture(false, "CHT2", text(TRACK.replace("FRAMES:4", "FRAMES:3") + GAPS));
    patch(file, 32, FRAME * 3, 8); // logical last hunk shorter than its stored hunk
    patch(file, 124, 1, 4);
    length(file, HUNK * 2L);
    patch(file, HUNK, 0x12345678, 4);
    ChdImage.validate(file, false);
    length(file, HUNK * 2L - 1);
    rejects(file, false, "未压缩数据块");
  }

  @Test
  public void gdIsExplicitAndBothCurrentAndLegacyTrackTagsAreHandled() throws Exception {
    for (String tag : new String[] {"CHTR", "CHT2", "CHGT", "CHGD"}) {
      File file = fixture(false, tag);
      ChdImage.validate(file, true);
      if (tag.equals("CHGT") || tag.equals("CHGD")) rejects(file, false, "GD-ROM");
      else ChdImage.validate(file, false);
    }
  }

  @Test
  public void shortHomebrewCanDeclareVirtualGapsLongerThanItsStoredContent() throws Exception {
    File file =
        fixture(
            false,
            "CHT2",
            text(TRACK + GAPS.replace("PREGAP:0", "PREGAP:150").replace("POSTGAP:0", "POSTGAP:20")));
    ChdImage.validate(file, false);
  }

  @Test
  public void metadataChainNeedNotBeInAscendingFileOrder() throws Exception {
    File file = fixture(false, "CHT2");
    patch(file, 48, 512, 8);
    metadata(file, 512, "XTRA", new byte[] {1}, META);
    ChdImage.validate(file, false);
  }

  @Test
  public void oldBinaryCdMetadataSupportsBothHistoricalByteOrders() throws Exception {
    for (boolean little : new boolean[] {false, true}) {
      byte[] old = new byte[(1 + 99 * 6) * 4];
      oldWord(old, 0, 1, little);
      oldWord(old, 4, 1, little); // MODE1_RAW
      oldWord(old, 8, 2, little); // no subchannel
      oldWord(old, 12, 2352, little);
      oldWord(old, 20, 4, little);
      File file = fixture(false, "CHCD", old);
      ChdImage.validate(file, false);
      patch(file, META + 4, 0x01000000 | (old.length - 1), 4);
      rejects(file, false, "旧 CD 元数据不完整");
    }
  }

  private static void oldWord(byte[] bytes, int offset, int value, boolean little) {
    for (int i = 0; i < 4; i++) bytes[offset + i] = (byte) (value >>> ((little ? i : 3 - i) * 8));
  }

  @Test
  public void boundedUnknownMetadataCanAccompanyButCannotEstablishDiscType() throws Exception {
    File file = fixture(false, "CHSE", text("SESSION:1"));
    rejects(file, false, "未找到");
    patch(file, META + 8, 256, 8);
    metadata(file, 256, "CHT2", text(TRACK + GAPS), 512);
    metadata(file, 512, "XTRA", new byte[] {0, 1, 2, 3}, 0);
    ChdImage.validate(file, false);
  }

  @Test
  public void knownOtherMediaIsRejectedEvenAlongsideCdMetadata() throws Exception {
    for (String tag : new String[] {"GDDD", "IDNT", "KEY ", "CIS ", "DVD ", "AVAV", "AVLD"}) {
      File file = fixture(false, "CHT2");
      patch(file, META + 8, 512, 8);
      metadata(file, 512, tag, new byte[0], 0);
      rejects(file, true, "非 CD / GD");
    }
  }

  @Test
  public void headerMagicVersionLengthAndTruncationAreScreened() throws Exception {
    for (long[] change : new long[][] {{0, 0, 1}, {8, 108, 4}, {12, 4, 4}, {12, 6, 4}}) {
      File file = fixture(false, "CHT2");
      patch(file, change[0], change[1], (int) change[2]);
      rejects(file, false, "CHD v5");
    }
    File file = fixture(false, "CHT2");
    length(file, 123);
    rejects(file, false, "文件长度");
    try {
      ChdImage.validate(null, false);
      fail();
    } catch (IOException expected) {
      assertTrue(expected.getMessage().contains("不存在"));
    }
  }

  @Test
  public void anyParentHashByteRejectsDependentImages() throws Exception {
    for (int offset : new int[] {104, 113, 123}) {
      File file = fixture(false, "CHT2");
      patch(file, offset, 1, 1);
      rejects(file, false, "父 CHD");
    }
  }

  @Test
  public void physicalLimitIsCheckedBeforeReadingOrAllocating() throws Exception {
    File file = temporary.newFile();
    length(file, ChdImage.MAX_FILE_BYTES + 1); // sparse, no 2 GiB allocation
    try {
      ChdImage.validate(file, false);
      fail();
    } catch (IOException expected) {
      assertTrue(expected.getMessage().contains("2 GiB"));
    }
    assertEquals(ChdImage.MAX_FILE_BYTES + 1, file.length());
  }

  @Test
  public void logicalLimitsAndUnsigned64OverflowDoNotAllocateByDeclaredSize() throws Exception {
    for (long logical : new long[] {0, 1, ChdImage.MAX_LOGICAL_BYTES + 1, Long.MAX_VALUE, -1L}) {
      File file = fixture(true, "CHT2");
      patch(file, 32, logical, 8);
      rejects(file, false, logical < 0 ? "64 位" : "逻辑长度");
    }
    File allowed = fixture(true, "CHT2");
    patch(allowed, 32, ChdImage.MAX_LOGICAL_BYTES / FRAME * FRAME, 8);
    ChdImage.validate(allowed, false); // bounded map screen, not proof of those hunks
  }

  @Test
  public void discGeometryMustUseWhole2448ByteFramesWithinHunkLimit() throws Exception {
    for (long[] change :
        new long[][] {{56, 0}, {60, 0}, {60, 2048}, {56, ChdImage.MAX_HUNK_BYTES + 1}, {56, FRAME + 1}}) {
      File file = fixture(false, "CHT2");
      patch(file, change[0], change[1], 4);
      rejects(file, false, "hunk / unit");
    }
  }

  @Test
  public void emptyCodecSlotsCannotBeFollowedByNonemptySlots() throws Exception {
    for (boolean compressed : new boolean[] {false, true}) {
      File file = fixture(compressed, "CHT2");
      patch(file, compressed ? 24 : 20, 0x7a6c6962, 4);
      rejects(file, false, "压缩器列表");
    }
  }

  @Test
  public void zeroHeaderMapOffsetIsNotACompletedSparseMap() throws Exception {
    for (boolean compressed : new boolean[] {false, true}) {
      File file = fixture(compressed, "CHT2");
      patch(file, 40, 0, 8);
      rejects(file, false, "map 尚未写入");
    }
  }

  @Test
  public void mapOffsetsRejectHeaderOverlapTruncationAndUnsignedOverflow() throws Exception {
    for (boolean compressed : new boolean[] {false, true}) {
      for (long offset : new long[] {123, 1000000, Long.MAX_VALUE, -1L}) {
        File file = fixture(compressed, "CHT2");
        patch(file, 40, offset, 8);
        rejects(file, false, offset < 0 ? "64 位" : "map");
      }
      File truncated = fixture(compressed, "CHT2");
      patch(truncated, 40, truncated.length() - (compressed ? 15 : 3), 8);
      rejects(truncated, false, "map");
    }
  }

  @Test
  public void compressedMapLengthAndNativeBitWidthsAreBounded() throws Exception {
    for (long bytes : new long[] {0, 9, ChdImage.MAX_MAP_BYTES + 1L, 0xffffffffL}) {
      File file = fixture(true, "CHT2");
      patch(file, COMPRESSED_MAP, bytes, 4);
      rejects(file, false, "map");
    }
    for (int offset = 12; offset <= 14; offset++) {
      File file = fixture(true, "CHT2");
      patch(file, COMPRESSED_MAP + offset, 33, 1);
      rejects(file, false, "位宽");
    }
  }

  @Test
  public void compressedDataStartCannotPointOutsideOrIntoMapOrMetadata() throws Exception {
    for (long offset : new long[] {0, 123, 1000000, 0xffffffffffffL, COMPRESSED_MAP, META}) {
      File file = fixture(true, "CHT2");
      patch(file, COMPRESSED_MAP + 4, offset, 6);
      rejects(file, false, "首个数据块");
    }
  }

  @Test
  public void uncompressedEntriesCannotWrapOrPointToTruncatedData() throws Exception {
    for (long block : new long[] {1, 0xffffffffL}) {
      File file = fixture(false, "CHT2");
      patch(file, 124, block, 4);
      rejects(file, false, "未压缩数据块");
    }
  }

  @Test
  public void uncompressedDataMayNotOverlapTheMapOrMetadata() throws Exception {
    File mapOverlap = fixture(false, "CHT2");
    patch(mapOverlap, 40, HUNK, 8);
    length(mapOverlap, HUNK * 2L);
    patch(mapOverlap, HUNK, 1, 4);
    rejects(mapOverlap, false, "数据块与 map 重叠");
    File metadataOverlap = fixture(false, "CHT2");
    patch(metadataOverlap, 48, HUNK, 8);
    patch(metadataOverlap, 124, 1, 4);
    metadata(metadataOverlap, HUNK, "CHT2", text(TRACK + GAPS), 0);
    length(metadataOverlap, HUNK * 2L);
    rejects(metadataOverlap, false, "数据块与元数据重叠");
  }

  @Test
  public void metadataOffsetsLengthsAndMapOverlapAreRejected() throws Exception {
    for (long offset : new long[] {0, 123, 1000000, Long.MAX_VALUE, -1L}) {
      File file = fixture(false, "CHT2");
      patch(file, 48, offset, 8);
      rejects(file, false, offset < 0 ? "64 位" : offset == 0 ? "未找到" : "元数据");
    }
    File truncated = fixture(false, "CHT2");
    patch(truncated, META + 4, 0x01000000 | 1000, 4);
    rejects(truncated, false, "元数据内容");
    File overlap = fixture(false, "CHT2");
    metadata(overlap, 124, "CHT2", text(TRACK + GAPS), 0);
    patch(overlap, 48, 124, 8);
    rejects(overlap, false, "元数据与 map");
  }

  @Test
  public void metadataChainCyclesAndNextOffsetOverflowAreRejected() throws Exception {
    File self = fixture(false, "CHT2");
    patch(self, META + 8, META, 8);
    rejects(self, false, "循环");
    File cycle = fixture(false, "CHT2");
    patch(cycle, META + 8, 512, 8);
    metadata(cycle, 512, "XTRA", new byte[] {1}, META);
    rejects(cycle, false, "循环");
    for (long next : new long[] {-1L, Long.MAX_VALUE, 1000000}) {
      File file = fixture(false, "CHT2");
      patch(file, META + 8, next, 8);
      rejects(file, false, next < 0 ? "64 位" : "元数据头部");
    }
  }

  @Test
  public void metadataCountAndTotalBytesAreBoundedWithoutReadingUnknownPayloads()
      throws Exception {
    File entries = fixture(false, "CHT2");
    long previous = META;
    for (int i = 1; i < ChdImage.MAX_METADATA_ENTRIES; i++) {
      long offset = 1024 + i * 32L;
      patch(entries, previous + 8, offset, 8);
      metadata(entries, offset, "XTRA", new byte[0], 0);
      previous = offset;
    }
    ChdImage.validate(entries, false);
    patch(entries, previous + 8, previous + 32, 8);
    metadata(entries, previous + 32, "XTRA", new byte[0], 0);
    rejects(entries, false, "条目数量");

    File bytes = fixture(false, "CHT2");
    int trackBytes = text(TRACK + GAPS).length;
    patch(bytes, META + 8, 512, 8);
    metadata(bytes, 512, "XTRA", new byte[ChdImage.MAX_METADATA_BYTES - trackBytes], 0);
    ChdImage.validate(bytes, false);
    patch(bytes, 516, 0x01000000 | (ChdImage.MAX_METADATA_BYTES - trackBytes + 1), 4);
    length(bytes, bytes.length() + 1);
    rejects(bytes, false, "元数据总长度");
  }

  @Test
  public void incompleteTrackFieldsInvalidTypesAndOverflowAreRejected() throws Exception {
    for (String track :
        new String[] {
          "",
          TRACK,
          TRACK.replace("TRACK:1", "TRACK:0") + GAPS,
          TRACK.replace("TRACK:1", "TRACK:100") + GAPS,
          TRACK.replace("MODE1_RAW", "HARD_DISK") + GAPS,
          TRACK.replace("SUBTYPE:NONE", "SUBTYPE:BAD") + GAPS,
          TRACK.replace("FRAMES:4", "FRAMES:0") + GAPS,
          TRACK.replace("FRAMES:4", "FRAMES:42949672960") + GAPS,
          TRACK + GAPS.replace("PGTYPE:MODE1", "PGTYPE:BAD"),
          TRACK + GAPS.replace("POSTGAP:0", "POSTGAP:-1")
        }) {
      File file = fixture(false, "CHT2", text(track));
      rejects(file, false, "光盘");
    }
    File nul = fixture(false, "CHT2", text(TRACK + "\0" + GAPS));
    rejects(nul, false, "不是受支持的文本");
  }

  @Test
  public void interruptionRemainsSetAndOriginalFileIsUntouched() throws Exception {
    File file = fixture(false, "CHT2");
    try {
      Thread.currentThread().interrupt();
      rejects(file, false, "取消");
      assertTrue(Thread.currentThread().isInterrupted());
    } finally {
      Thread.interrupted();
    }
  }
}
