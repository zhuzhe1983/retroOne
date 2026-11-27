package io.retroone.android;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.*;
import org.json.*;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

public class FbneoRomSetTest {
  @Rule public TemporaryFolder files = new TemporaryFolder();
  private static final String BIOS_SOURCE =
      "7e51b413aab7da1965ba2d003d0b76fd7e5dfac5a9f82ea356fc74b0680602b7";

  private static byte[] bytes(String label) {
    return ("RetroOne original test bytes: " + label).getBytes(StandardCharsets.UTF_8);
  }

  private static String digest(byte[] bytes) throws Exception {
    return FbneoArchive.hex(MessageDigest.getInstance("SHA-256").digest(bytes));
  }

  private static String crc(byte[] bytes) {
    CRC32 crc = new CRC32();
    crc.update(bytes);
    return String.format(Locale.ROOT, "%08x", crc.getValue());
  }

  private static Map<String, byte[]> map(String... labels) {
    Map<String, byte[]> result = new LinkedHashMap<>();
    for (String label : labels) result.put(label + ".bin", bytes(label));
    return result;
  }

  private static String signature(Map<String, byte[]> members) throws Exception {
    List<String> signatures = new ArrayList<>();
    for (byte[] content : members.values()) signatures.add(crc(content) + ":" + content.length);
    Collections.sort(signatures);
    return digest((String.join("\n", signatures) + "\n").getBytes(StandardCharsets.UTF_8));
  }

  private File zip(Map<String, byte[]> data) throws Exception {
    File file = files.newFile();
    try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(file))) {
      for (Map.Entry<String, byte[]> member : data.entrySet()) {
        ZipEntry entry = new ZipEntry(member.getKey());
        entry.setMethod(ZipEntry.STORED);
        entry.setSize(member.getValue().length);
        entry.setCrc(Long.parseLong(crc(member.getValue()), 16));
        out.putNextEntry(entry);
        out.write(member.getValue());
        out.closeEntry();
      }
    }
    return file;
  }

  private static class Fixture {
    final JSONObject index = new JSONObject();
    final JSONObject catalog = new JSONObject(), signatures = new JSONObject();
    final JSONArray pool = new JSONArray(), bios = new JSONArray();
    final Map<String, byte[]> required = map("base0", "base1", "base2", "base3");
    final Map<String, byte[]> optional = map("alternate0", "alternate1");
    final Map<String, byte[]> base = map("program", "graphics");
    final Map<String, byte[]> clone = map("newprogram", "graphics");

    Fixture() throws Exception {
      index
          .put("schema_version", 2)
          .put("matcher_version", FbneoRomSet.VERSION)
          .put("source_sha256", FbneoArchive.DAT)
          .put("bios_source_sha256", BIOS_SOURCE)
          .put("catalog", catalog)
          .put("signatures", signatures)
          .put("members", pool)
          .put("neogeo_bios", bios);
      for (Map.Entry<String, byte[]> member : required.entrySet()) bios.put(row(member).put(true));
      for (Map.Entry<String, byte[]> member : optional.entrySet()) bios.put(row(member).put(false));
      Map<String, byte[]> allBios = new LinkedHashMap<>(required);
      allBios.putAll(optional);
      set("neogeo", "", "neogeo", 1, allBios);
      set("base", "", "", 0, base);
      set("clone", "base", "", 0, clone);
      set("unrelated", "", "", 0, map("unrelatedprogram"));
      set("neo", "", "neogeo", 0, map("neoprogram", "neographics"));
      set("neoclone", "neo", "neogeo", 0, map("newneoprogram", "neographics"));
    }

    JSONArray row(Map.Entry<String, byte[]> member) {
      return new JSONArray()
          .put(member.getKey())
          .put(crc(member.getValue()))
          .put(member.getValue().length);
    }

    void set(String name, String parent, String needs, int flags, Map<String, byte[]> members)
        throws Exception {
      JSONArray ids = new JSONArray();
      for (Map.Entry<String, byte[]> member : members.entrySet()) {
        ids.put(pool.length());
        pool.put(row(member));
      }
      catalog.put(name, new JSONArray().put(parent).put(needs).put(flags).put(ids));
      String signature = signature(members);
      if (!signatures.has(signature)) signatures.put(signature, new JSONArray());
      signatures.getJSONArray(signature).put(name);
    }
  }

  private interface ThrowingCall {
    void run() throws Exception;
  }

  private IOException reject(ThrowingCall call) throws Exception {
    try {
      call.run();
      fail("Expected rejection");
      return null;
    } catch (IOException expected) {
      return expected;
    }
  }

  @Test
  public void completeCloneNeedsNoSeparateParentButSplitContentIsRejected() throws Exception {
    Fixture fixture = new Fixture();
    File clone = zip(fixture.clone);
    byte[] before = Files.readAllBytes(clone.toPath());
    FbneoRomSet.Match match = FbneoRomSet.inspect(clone, fixture.index);
    assertEquals("clone", match.set);
    assertFalse(match.neoGeo);
    assertFalse(match.merged);
    assertTrue(match.missingBios.isEmpty());
    assertEquals("clone", FbneoArchive.identify(clone, fixture.index));
    assertArrayEquals(before, Files.readAllBytes(clone.toPath()));
    File split = zip(map("newprogram"));
    assertTrue(
        reject(() -> FbneoRomSet.inspect(split, fixture.index)).getMessage().contains("完整 ROM 集"));
  }

  @Test
  public void neoGeoReportsFourMissingAndAcceptsRequiredPlusOptionalSubset() throws Exception {
    Fixture fixture = new Fixture();
    File game = zip(map("neoprogram", "neographics"));
    FbneoRomSet.Match match = FbneoRomSet.inspect(game, fixture.index);
    assertEquals("neo", match.set);
    assertTrue(match.neoGeo);
    assertEquals(4, match.missingBios.size());
    assertTrue(
        reject(() -> FbneoRomSet.resolve(match, null, fixture.index))
            instanceof FbneoRomSet.MissingBiosException);
    Map<String, byte[]> biosData = new LinkedHashMap<>(fixture.required);
    biosData.put("renamed-alternative.bin", bytes("alternate0"));
    File bios = zip(biosData);
    byte[] before = Files.readAllBytes(bios.toPath());
    FbneoRomSet.Bios verified = FbneoRomSet.verifyBios(bios, fixture.index);
    assertEquals(5, verified.presentMembers.size());
    assertEquals(Collections.singletonList("alternate0.bin"), verified.optionalMembers);
    FbneoRomSet.Match resolved = FbneoRomSet.resolve(match, bios, fixture.index);
    assertTrue(resolved.missingBios.isEmpty());
    assertEquals(verified.sha256, resolved.biosSha256);
    assertEquals(digest(before), resolved.biosSha256);
    assertEquals(match.archiveSha256, resolved.archiveSha256);
    assertEquals(4, match.missingBios.size());
    assertArrayEquals(before, Files.readAllBytes(bios.toPath()));
  }

  @Test
  public void eachRequiredBiosIsNecessaryButTheOther34AlternativesAreNot() throws Exception {
    Fixture fixture = new Fixture();
    for (String missing : fixture.required.keySet()) {
      Map<String, byte[]> contents = new LinkedHashMap<>(fixture.required);
      contents.remove(missing);
      contents.putAll(fixture.optional);
      File partial = zip(contents);
      IOException failure = reject(() -> FbneoRomSet.verifyBios(partial, fixture.index));
      assertTrue(failure instanceof FbneoRomSet.MissingBiosException);
      assertEquals(
          Collections.singletonList(missing),
          ((FbneoRomSet.MissingBiosException) failure).missingNames);
    }
    assertEquals(
        0, FbneoRomSet.verifyBios(zip(fixture.required), fixture.index).optionalMembers.size());
  }

  @Test
  public void sameArchiveAndExternalBiosAreCombinedWithoutReadingUnneededFile() throws Exception {
    Fixture fixture = new Fixture();
    Map<String, byte[]> game = map("neoprogram", "neographics");
    game.putAll(fixture.required);
    File complete = zip(game);
    FbneoRomSet.Match match = FbneoRomSet.inspect(complete, fixture.index);
    assertTrue(match.missingBios.isEmpty());
    assertEquals(4, match.includedBios.size());
    File missingFile = new File(files.getRoot(), "does-not-exist.zip");
    assertSame(match, FbneoRomSet.resolve(match, missingFile, fixture.index));
    assertEquals("", match.biosSha256);
    Map<String, byte[]> halfGame = map("neoprogram", "neographics");
    halfGame.put("base0.bin", bytes("base0"));
    halfGame.put("base1.bin", bytes("base1"));
    FbneoRomSet.Match half = FbneoRomSet.inspect(zip(halfGame), fixture.index);
    assertEquals(2, half.missingBios.size());
    File otherHalf = zip(map("base2", "base3"));
    assertTrue(FbneoRomSet.resolve(half, otherHalf, fixture.index).missingBios.isEmpty());
    assertTrue(
        reject(() -> FbneoRomSet.verifyBios(otherHalf, fixture.index))
            instanceof FbneoRomSet.MissingBiosException);
  }

  @Test
  public void mergedRootMustBeCompleteAndEveryPartialChildMemberMustBeRelated() throws Exception {
    Fixture fixture = new Fixture();
    Map<String, byte[]> merged = new LinkedHashMap<>(fixture.base);
    merged.put("clone/", new byte[0]);
    merged.put("clone/newprogram.bin", bytes("newprogram"));
    File file = zip(merged);
    byte[] before = Files.readAllBytes(file.toPath());
    FbneoRomSet.Match match = FbneoRomSet.inspect(file, fixture.index);
    assertEquals("base", match.set);
    assertTrue(match.merged);
    assertArrayEquals(before, Files.readAllBytes(file.toPath()));
    for (String path :
        new String[] {
          "unrelated/unrelatedprogram.bin", "clone/unknown.bin", "clone/newprogram.bin"
        }) {
      Map<String, byte[]> invalid = new LinkedHashMap<>(fixture.base);
      invalid.put(path, bytes("unrelatedprogram"));
      File rejected = zip(invalid);
      reject(() -> FbneoRomSet.inspect(rejected, fixture.index));
    }
    Map<String, byte[]> incompleteRoot = map("program");
    incompleteRoot.put("clone/newprogram.bin", bytes("newprogram"));
    incompleteRoot.put("clone/graphics.bin", bytes("graphics"));
    File incomplete = zip(incompleteRoot);
    reject(() -> FbneoRomSet.inspect(incomplete, fixture.index));
  }

  @Test
  public void mergedNeoGeoClonesAndGrandchildrenKeepBaseSelection() throws Exception {
    Fixture fixture = new Fixture();
    fixture.set("neograndchild", "neoclone", "neogeo", 0, map("grandprogram", "neographics"));
    Map<String, byte[]> merged = map("neoprogram", "neographics");
    merged.put("neoclone/newneoprogram.bin", bytes("newneoprogram"));
    merged.put("neograndchild/grandprogram.bin", bytes("grandprogram"));
    FbneoRomSet.Match match = FbneoRomSet.inspect(zip(merged), fixture.index);
    assertEquals("neo", match.set);
    assertTrue(match.merged);
    assertEquals(4, match.missingBios.size());
    assertTrue(
        FbneoRomSet.resolve(match, zip(fixture.required), fixture.index).missingBios.isEmpty());
  }

  @Test
  public void unknownExtrasWrongBiosAndDuplicateContentAreNeverIgnored() throws Exception {
    Fixture fixture = new Fixture();
    Map<String, byte[]> badGame = map("neoprogram", "neographics", "unknown-readme");
    File file = zip(badGame);
    reject(() -> FbneoRomSet.inspect(file, fixture.index));
    Map<String, byte[]> badBios = new LinkedHashMap<>(fixture.required);
    badBios.put("alternate0.bin", bytes("unreviewed-bios"));
    File bios = zip(badBios);
    reject(() -> FbneoRomSet.verifyBios(bios, fixture.index));
    badBios = new LinkedHashMap<>(fixture.required);
    badBios.put("duplicate.bin", bytes("base0"));
    File duplicates = zip(badBios);
    reject(() -> FbneoRomSet.verifyBios(duplicates, fixture.index));
    File biosOnly = zip(fixture.required);
    assertTrue(
        reject(() -> FbneoRomSet.inspect(biosOnly, fixture.index)).getMessage().contains("BIOS"));
  }

  @Test
  public void actualPayloadCrcIsReadAndNotTrustedFromCentralDirectory() throws Exception {
    Fixture fixture = new Fixture();
    File file = zip(fixture.base);
    try (RandomAccessFile edit = new RandomAccessFile(file, "rw")) {
      edit.seek(30 + "program.bin".length());
      int value = edit.read();
      edit.seek(30 + "program.bin".length());
      edit.write(value ^ 1);
    }
    byte[] corrupt = Files.readAllBytes(file.toPath());
    assertTrue(reject(() -> FbneoRomSet.inspect(file, fixture.index)).getMessage().contains("CRC"));
    assertArrayEquals(corrupt, Files.readAllBytes(file.toPath()));
  }

  @Test
  public void pathsAndCaseCollisionsAndUnrelatedEmptyDirectoriesAreRejected() throws Exception {
    Fixture fixture = new Fixture();
    for (String path :
        new String[] {
          "../escape.bin",
          "clone/../../escape.bin",
          "clone\\file.bin",
          "clone/deep/program.bin",
          "C:escape.bin",
          "/absolute.bin",
          "unknown/"
        }) {
      Map<String, byte[]> data = new LinkedHashMap<>(fixture.base);
      data.put(path, path.endsWith("/") ? new byte[0] : bytes("newprogram"));
      File bad = zip(data);
      reject(() -> FbneoRomSet.inspect(bad, fixture.index));
    }
    Map<String, byte[]> collision = new LinkedHashMap<>(fixture.base);
    collision.put("PROGRAM.BIN", bytes("program"));
    File bad = zip(collision);
    assertTrue(reject(() -> FbneoRomSet.inspect(bad, fixture.index)).getMessage().contains("大小写"));
    Map<String, byte[]> fileDirectory = map("graphics");
    fileDirectory.put("clone", bytes("program"));
    fileDirectory.put("clone/newprogram.bin", bytes("newprogram"));
    File overlapping = zip(fileDirectory);
    assertTrue(
        reject(() -> FbneoRomSet.inspect(overlapping, fixture.index))
            .getMessage()
            .contains("路径冲突"));
  }

  @Test
  public void unsupportedBiosAmbiguityAndWrongIndexStayClosed() throws Exception {
    Fixture fixture = new Fixture();
    fixture.set("otherplatform", "", "otherbios", 0, map("otherplatform"));
    File file = zip(map("otherplatform"));
    assertTrue(
        reject(() -> FbneoRomSet.inspect(file, fixture.index)).getMessage().contains("尚未接入"));
    fixture.set("basealias", "", "", 0, fixture.base);
    File base = zip(fixture.base);
    assertTrue(reject(() -> FbneoRomSet.inspect(base, fixture.index)).getMessage().contains("多个"));
    fixture.index.put("matcher_version", "unreviewed");
    assertTrue(
        reject(() -> FbneoRomSet.inspect(base, fixture.index)).getMessage().contains("索引版本"));
  }

  @Test
  public void physicalAndDirectoryEntryLimitsRunBeforeParsingAndCancellationPreservesSource()
      throws Exception {
    Fixture fixture = new Fixture();
    File sparse = files.newFile();
    try (RandomAccessFile out = new RandomAccessFile(sparse, "rw")) {
      out.setLength(FbneoArchive.MAX_ARCHIVE_BYTES + 1);
    }
    assertTrue(
        reject(() -> FbneoRomSet.inspect(sparse, fixture.index)).getMessage().contains("512 MiB"));
    Map<String, byte[]> dirs = new LinkedHashMap<>();
    for (int i = 0; i <= FbneoArchive.MAX_ENTRIES; i++) dirs.put("dir" + i + "/", new byte[0]);
    File tooMany = zip(dirs);
    assertTrue(
        reject(() -> FbneoRomSet.inspect(tooMany, fixture.index)).getMessage().contains("条目数量"));
    File source = zip(fixture.base);
    byte[] before = Files.readAllBytes(source.toPath());
    Thread.currentThread().interrupt();
    try {
      assertTrue(
          reject(() -> FbneoRomSet.inspect(source, fixture.index)).getMessage().contains("取消"));
    } finally {
      Thread.interrupted();
    }
    assertArrayEquals(before, Files.readAllBytes(source.toPath()));
  }

  @Test
  public void contentMultisetCannotBeSatisfiedByOneOfTwoIdenticalMembers() throws Exception {
    Fixture fixture = new Fixture();
    Map<String, byte[]> duplicateRequired = new LinkedHashMap<>();
    duplicateRequired.put("one.bin", bytes("same-content"));
    duplicateRequired.put("two.bin", bytes("same-content"));
    fixture.set("twocopies", "", "", 0, duplicateRequired);
    File complete = zip(duplicateRequired);
    assertEquals("twocopies", FbneoRomSet.inspect(complete, fixture.index).set);
    File partial = zip(Collections.singletonMap("one.bin", bytes("same-content")));
    reject(() -> FbneoRomSet.inspect(partial, fixture.index));
  }
}
