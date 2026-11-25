package io.retroone.android;

import java.io.*;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.*;
import java.util.zip.*;

/** A byte-exact compatibility exception, separate from official FBNeo DAT admission. */
final class ArcadeCompatProfile {
  static final String VERSION = "knights-mame-v1";
  static final String SOURCE_SHA256 =
      "e7920e35a0a2528703361174349ad8bd39c240fce9e80bf81c6f0cca1094167f";

  static final class Profile {
    final String id, core, set, label;
    final String sourceSha256;
    final List<Member> members;

    Profile(
        String id, String core, String set, String label, String sourceSha256, Member... members) {
      if (!safeName(id)
          || !safeName(set)
          || !safeName(core)
          || !sourceSha256.matches("[a-f0-9]{64}")
          || members.length == 0
          || members.length > FbneoArchive.MAX_ENTRIES)
        throw new IllegalArgumentException("Invalid compatibility profile");
      Set<String> original = new HashSet<>(), renamed = new HashSet<>();
      for (Member member : members) {
        if (!original.add(member.sourceName) || !renamed.add(member.stageName))
          throw new IllegalArgumentException("Duplicate compatibility member");
      }
      this.id = id;
      this.core = core;
      this.set = set;
      this.label = label;
      this.sourceSha256 = sourceSha256;
      this.members = Collections.unmodifiableList(Arrays.asList(members.clone()));
    }
  }

  static final class Member {
    final String sourceName, stageName, sha256;
    final long size, crc;

    Member(String sourceName, String stageName, long size, long crc, String sha256) {
      if (!safeName(sourceName)
          || !safeName(stageName)
          || size < 0
          || size > 512L * 1024 * 1024
          || crc < 0
          || crc > 0xffffffffL
          || !sha256.matches("[a-f0-9]{64}"))
        throw new IllegalArgumentException("Invalid compatibility member");
      this.sourceName = sourceName;
      this.stageName = stageName;
      this.size = size;
      this.crc = crc;
      this.sha256 = sha256;
    }
  }

  // Native evidence: knights-native-result.json, 2026-11-25, MAME2003-Plus e58aaec.
  // The 13 aliases below follow that core's CPS1 names. No ROM bytes are patched.
  private static final Profile KNIGHTS =
      new Profile(
          VERSION,
          "mame2003_plus_libretro_android.so",
          "knights",
          "圆桌骑士 MOD · MAME2003-Plus 兼容模式",
          SOURCE_SHA256,
          new Member(
              "bprg1.11d",
              "bprg1.11d",
              279L,
              0x31793da7L,
              "5e0b72dcc776259ee9402a7c930a5c39d6f49ed645a519ff46167c8c82b2eb0e"),
          new Member(
              "buf1",
              "buf1",
              279L,
              0xeb122de7L,
              "f313dbf1d435ba58cc5014db6dea1770abdd01ba54c1bc510751b950dc79696a"),
          new Member(
              "c632.ic1",
              "c632.ic1",
              279L,
              0x0fbd9270L,
              "23d2d1d495e0fda0a04e0a24962f8025c72ecf2e0f212b302dfec4fa19d08747"),
          new Member(
              "ioa1",
              "ioa1",
              279L,
              0x59c7ee3bL,
              "8073941038aab9017d09df03d45f191688e7becaeb2c2ee6ac64bd6c57060372"),
          new Member(
              "iob1.12d",
              "iob1.12d",
              279L,
              0x3abc0700L,
              "9771ac95f6b470fe5f2046f6e8eafa4940be369d3bda1be0093b9b56ffe4491a"),
          new Member(
              "ioc1.ic7",
              "ioc1.ic7",
              279L,
              0x0d182081L,
              "66c77432770f751f6e8fbd4969bdee7d5ad2f8bf2695fbe91ed275f47a9c4711"),
          new Member(
              "kr-1m.4a",
              "kr_gfx2.rom",
              524288L,
              0xf095be2dL,
              "013ab2f161cbc5262ddad94c6a29169f47d194f25f98842875a9266de4652eeb"),
          new Member(
              "kr-2m.8a",
              "kr_gfx6.rom",
              524288L,
              0x0200bc3dL,
              "742db4cfd6591a8abed260540d64f1a02e8873fd32cd93623bf0ea165542c8d7"),
          new Member(
              "kr-3m.6a",
              "kr_gfx4.rom",
              524288L,
              0x179dfd96L,
              "751607fd6121a402a46fbc9b2f54dd80abd9392f77f0fa3aee0677a53d7527a7"),
          new Member(
              "kr-4m.10a",
              "kr_gfx8.rom",
              524288L,
              0x0bb2b4e7L,
              "06066cba099441de53a38f34b075042ad668b82a7fe13b13ad3e1adf70655c3c"),
          new Member(
              "kr-5m.3a",
              "kr_gfx1.rom",
              524288L,
              0x9e36c1a4L,
              "ed850e09d88215290ce252cca32d4412162ac2df2235c43509f361b1ec2e5a5c"),
          new Member(
              "kr-6m.7a",
              "kr_gfx5.rom",
              524288L,
              0x1f4298d2L,
              "b2c0776175b1bf8cd67d4931ff9f68e0af454feaeafee87483eda5872eed2bd7"),
          new Member(
              "kr-7m.5a",
              "kr_gfx3.rom",
              524288L,
              0xc5832caeL,
              "93e49175e6720bd2fca8beecdb66f66b8792c7b3875658c03aa450d4abc97b13"),
          new Member(
              "kr-8m.9a",
              "kr_gfx7.rom",
              524288L,
              0x37fa8751L,
              "af4c0bad759d63818f1905f79f71cba3dec32b9a44982778c2f795ea0459e4d1"),
          new Member(
              "kr63b.1a",
              "kr63b.1a",
              279L,
              0xfd5b6522L,
              "bff16ee50b6962fbcc7cdac900a3e57863e979a8a6fc22d70cae35f4b2ea8e8d"),
          new Member(
              "kr_09.11a",
              "kr_09.rom",
              65536L,
              0x5e44d9eeL,
              "9f2107abc667ec7ff671e29dea638f3e6d79d47bedde4ae0bf0f8ed0f685b94c"),
          new Member(
              "kr_18.11c",
              "kr_18.rom",
              131072L,
              0xda69d15fL,
              "d2ae6759232eb953ac7184157bab3a8861122883f2c21f8524dd992aea867861"),
          new Member(
              "kr_19.12c",
              "kr_19.rom",
              131072L,
              0xbfc654e9L,
              "7d1ffe4830fa9b2fe4098a902c9801fdebd711763be5738e1c67b99987c42b18"),
          new Member(
              "kr_22.7f",
              "kr_22.rom",
              524288L,
              0xd0b671a9L,
              "4866b2a4af981e9682b09e16f2b76d1a461c2fd6a608a167a9f06a8279eb6a19"),
          new Member(
              "kr_23e.8f",
              "kr_23e.rom",
              524288L,
              0xe0d06498L,
              "ce2389e42a2a8ed49a8093aaa7060ee12f1d1ac1bead03dfdf0be07bcdbb7138"),
          new Member(
              "prg1",
              "prg1",
              279L,
              0xf1129744L,
              "85412e1859ee11f006661bf725fa54911bc84140c92899698209ed0d863e6c5c"),
          new Member(
              "rom1",
              "rom1",
              279L,
              0x41dc73b9L,
              "dff9c541fce76f6ed2c89112381ebf008cda1c7e3adde3047396d1aa2fde7d1a"),
          new Member(
              "sou1",
              "sou1",
              279L,
              0x84f4b2feL,
              "3d7ddc1c0ec5e1409ed044d79275c831578825521fb216ab3c0f082bcb147f37"));

  private ArcadeCompatProfile() {}

  /** Resolve saved UI state only through this fixed whitelist, never a caller-provided core. */
  static Profile profile(String id) {
    return VERSION.equals(id) ? KNIGHTS : null;
  }

  /** Unknown archive hashes return null; malformed or changed recognized input fails closed. */
  static Profile match(File source) throws IOException {
    checkCancelled();
    FbneoArchive.checkLayout(source);
    if (!SOURCE_SHA256.equals(sha256(source))) return null;
    verifyArchive(source, KNIGHTS, false);
    return KNIGHTS;
  }

  /** Create a separate, verified ZIP. The downloaded source is opened read-only. */
  static File prepare(File source, File cacheDir) throws IOException {
    Profile matched = match(source);
    if (matched == null) throw new IOException("该归档未匹配已验证的街机兼容模式；已保留原文件");
    return prepareProfile(source, cacheDir, matched);
  }

  // Only the fixed-hash production entry point can reach this transformation engine.
  private static synchronized File prepareProfile(File source, File cacheDir, Profile profile)
      throws IOException {
    checkCancelled();
    FbneoArchive.checkLayout(source);
    requireSourceHash(source, profile);
    verifyArchive(source, profile, false);
    File root = cacheDir.getCanonicalFile();
    if (!root.isDirectory() && !root.mkdirs()) throw new IOException("无法创建兼容缓存目录");
    File namespace = child(root, "arcade-compat");
    makeDirectory(namespace);
    File directory = child(namespace, profile.id);
    makeDirectory(directory);
    File output = child(directory, profile.set + ".zip");
    if (output.exists()) {
      // A corrupt or foreign cache is not silently overwritten or deleted.
      verifyArchive(output, profile, true);
      requireSourceHash(source, profile);
      checkCancelled();
      return output;
    }

    File pending = File.createTempFile(".pending-", ".zip", directory);
    boolean published = false, success = false;
    try {
      try (ZipFile zip = new ZipFile(source);
          FileOutputStream file = new FileOutputStream(pending);
          ZipOutputStream out = new ZipOutputStream(file)) {
        for (Member member : profile.members) {
          checkCancelled();
          ZipEntry original = zip.getEntry(member.sourceName);
          if (original == null) throw new IOException("兼容源文件成员已变化");
          ZipEntry renamed = new ZipEntry(member.stageName);
          renamed.setTime(0);
          renamed.setMethod(ZipEntry.STORED);
          renamed.setSize(member.size);
          renamed.setCompressedSize(member.size);
          renamed.setCrc(member.crc);
          out.putNextEntry(renamed);
          copyVerified(zip, original, member, out);
          out.closeEntry();
        }
        out.finish();
        file.getFD().sync();
      }
      verifyArchive(pending, profile, true);
      requireSourceHash(source, profile);
      checkCancelled();
      // Same-directory move; no REPLACE_EXISTING, so an unexpected target is preserved.
      Files.move(pending.toPath(), output.toPath());
      published = true;
      checkCancelled();
      success = true;
      return output;
    } finally {
      if (!success) {
        Files.deleteIfExists(pending.toPath());
        if (published) Files.deleteIfExists(output.toPath());
      }
    }
  }

  private static void verifyArchive(File archive, Profile profile, boolean staged)
      throws IOException {
    int count = FbneoArchive.checkLayout(archive);
    if (count != profile.members.size()) throw new IOException("兼容归档的成员数量不符");
    Map<String, Member> expected = new HashMap<>();
    for (Member member : profile.members)
      expected.put(staged ? member.stageName : member.sourceName, member);
    try (ZipFile zip = new ZipFile(archive)) {
      if (zip.size() != count) throw new IOException("兼容归档条目已变化");
      Enumeration<? extends ZipEntry> entries = zip.entries();
      while (entries.hasMoreElements()) {
        checkCancelled();
        ZipEntry entry = entries.nextElement();
        Member member = expected.remove(entry.getName());
        if (entry.isDirectory()
            || member == null
            || entry.getSize() != member.size
            || entry.getCrc() != member.crc) throw new IOException("兼容归档的成员名称、大小或 CRC 不符");
        copyVerified(zip, entry, member, null);
      }
    }
    if (!expected.isEmpty()) throw new IOException("兼容归档缺少预期成员");
  }

  private static void copyVerified(ZipFile zip, ZipEntry entry, Member member, OutputStream out)
      throws IOException {
    MessageDigest digest = digest();
    CRC32 crc = new CRC32();
    byte[] buffer = new byte[65536];
    long count = 0;
    try (InputStream input = zip.getInputStream(entry)) {
      int n;
      while ((n = input.read(buffer)) != -1) {
        checkCancelled();
        count += n;
        if (count > member.size) throw new IOException("兼容归档成员展开量超出预期");
        digest.update(buffer, 0, n);
        crc.update(buffer, 0, n);
        if (out != null) out.write(buffer, 0, n);
      }
    }
    if (count != member.size
        || crc.getValue() != member.crc
        || !member.sha256.equals(FbneoArchive.hex(digest.digest())))
      throw new IOException("兼容归档成员内容校验不符；未修改原文件");
  }

  private static void requireSourceHash(File source, Profile profile) throws IOException {
    if (!profile.sourceSha256.equals(sha256(source)))
      throw new IOException("兼容源归档摘要不符或内容已变化；未修改原文件");
  }

  private static String sha256(File file) throws IOException {
    MessageDigest digest = digest();
    byte[] buffer = new byte[65536];
    try (InputStream input = new FileInputStream(file)) {
      int n;
      long total = 0;
      while ((n = input.read(buffer)) != -1) {
        checkCancelled();
        total += n;
        if (total > FbneoArchive.MAX_ARCHIVE_BYTES) throw new IOException("兼容归档超过物理大小上限");
        digest.update(buffer, 0, n);
      }
    }
    return FbneoArchive.hex(digest.digest());
  }

  private static MessageDigest digest() {
    try {
      return MessageDigest.getInstance("SHA-256");
    } catch (NoSuchAlgorithmException impossible) {
      throw new AssertionError(impossible);
    }
  }

  private static boolean safeName(String name) {
    return name != null
        && name.matches("[A-Za-z0-9_-][A-Za-z0-9_.-]{0,95}")
        && !name.equals(".")
        && !name.equals("..");
  }

  private static File child(File parent, String name) throws IOException {
    File child = new File(parent, name);
    if (!child.getCanonicalFile().equals(child.getAbsoluteFile()))
      throw new IOException("兼容缓存路径不能指向其他位置");
    return child;
  }

  private static void makeDirectory(File directory) throws IOException {
    if (!directory.isDirectory() && !directory.mkdir()) throw new IOException("兼容缓存目录无效或无法创建");
  }

  private static void checkCancelled() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw new IOException("街机兼容准备已取消");
  }
}
