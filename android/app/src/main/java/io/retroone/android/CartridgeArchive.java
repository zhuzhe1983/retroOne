package io.retroone.android;

import java.io.DataInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.nio.charset.Charset;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/** Single-cartridge ZIP preparation; extraction never uses an archive member as an output path. */
final class CartridgeArchive {
  static final long MAX_ARCHIVE_BYTES = 128L * 1024 * 1024;
  static final long MAX_NOTE_BYTES = 64L * 1024;
  static final long MAX_NOTES_BYTES = 1024L * 1024;
  static final int MAX_NOTES = 32;

  static final class Extracted {
    final File file;
    final String name;
    final boolean fromArchive;

    private Extracted(File file, String name, boolean fromArchive) {
      this.file = file;
      this.name = name;
      this.fromArchive = fromArchive;
    }
  }

  static boolean isZipName(String name) {
    return safeBasename(name)
        && name.length() > 4
        && name.toLowerCase(Locale.ROOT).endsWith(".zip");
  }

  static Extracted prepare(File source, String originalName, String platform, File cacheDir)
      throws Exception {
    return prepare(source, originalName, new Format(platform), cacheDir);
  }

  static Extracted prepareRuntime(
      File source, String originalName, RuntimePolicy.Spec spec, File cacheDir) throws Exception {
    if (spec == null || spec.maxArchiveBytes == 0) throw new IOException("此平台不支持单卡带 ZIP");
    return prepare(source, originalName, new Format(spec), cacheDir);
  }

  private static final class Format {
    final boolean nes;
    final CartridgePolicy.Spec cartridge;
    final RuntimePolicy.Spec runtime;
    final String id;
    final long maximum, archiveMaximum;

    Format(String platform) throws IOException {
      nes = "nes".equals(platform);
      cartridge = CartridgePolicy.spec(platform);
      runtime = null;
      if (!nes && cartridge == null) throw new IOException("此平台尚未接入单卡带解包");
      id = nes ? "nes" : cartridge.id;
      maximum = nes ? 64L * 1024 * 1024 : cartridge.maxBytes;
      archiveMaximum = MAX_ARCHIVE_BYTES;
    }

    Format(RuntimePolicy.Spec spec) {
      nes = false;
      cartridge = null;
      runtime = spec;
      id = spec.id;
      maximum = spec.maxBytes;
      archiveMaximum = spec.maxArchiveBytes;
    }

    boolean matches(String name) {
      return runtime == null
          ? romName(name, nes, cartridge)
          : RuntimePolicy.isRomName(runtime, name);
    }

    void validate(File file) throws IOException {
      if (runtime == null) CartridgeArchive.validate(file, nes, cartridge);
      else RuntimePolicy.validateFile(runtime, file);
    }
  }

  private static Extracted prepare(File source, String originalName, Format format, File cacheDir)
      throws Exception {
    interrupted();
    if (!safeBasename(originalName)) throw new IOException("原文件名无效");
    if (source == null || !source.isFile() || !source.canRead())
      throw new IOException("本地卡带文件不存在或不可读");
    if (!isZipName(originalName)) {
      if (!format.matches(originalName)) throw new IOException("仅支持独立卡带文件或只含一份卡带的 ZIP；其他格式请手动导入");
      format.validate(source);
      return new Extracted(source, originalName, false);
    }
    long physicalSize = source.length(), modified = source.lastModified();
    if (physicalSize > format.archiveMaximum)
      throw new IOException(
          "卡带 ZIP 超过 " + format.archiveMaximum / (1024 * 1024) + " MiB 物理大小上限，请手动导入");
    int expectedEntries = FbneoArchive.checkLayout(source, format.archiveMaximum);
    checkRegularEntries(source, expectedEntries);
    long maxRomBytes = format.maximum;
    // Old non-UTF8 archives often contain GBK bytes. CP437 is lossless for names and suffixes;
    // ZipFile still honors each member's UTF-8 flag. We never infer content from the title.
    try (ZipFile zip = new ZipFile(source, Charset.forName("CP437"))) {
      if (zip.size() != expectedEntries) throw new IOException("ZIP 条目数量已变化");
      ZipEntry rom = null;
      String romBasename = null;
      List<ZipEntry> notes = new ArrayList<>();
      Set<String> names = new HashSet<>();
      long notesBytes = 0;
      int entryCount = 0;
      Enumeration<? extends ZipEntry> entries = zip.entries();
      while (entries.hasMoreElements()) {
        interrupted();
        ZipEntry entry = entries.nextElement();
        String name = entry.getName();
        if (++entryCount > FbneoArchive.MAX_ENTRIES || !names.add(name.toLowerCase(Locale.ROOT)))
          throw new IOException("ZIP 条目过多或重名（包括目录）");
        String basename = memberBasename(name, entry.isDirectory());
        if (entry.getMethod() != ZipEntry.STORED && entry.getMethod() != ZipEntry.DEFLATED)
          throw new IOException("ZIP 压缩方法尚未支持，请手动导入");
        if (entry.getSize() < 0 || entry.getCompressedSize() < 0 || entry.getCrc() < 0)
          throw new IOException("ZIP 条目缺少大小或 CRC");
        if (entry.isDirectory()) {
          if (entry.getSize() != 0 || entry.getCrc() != 0) throw new IOException("ZIP 目录包含异常数据");
          continue;
        }
        if (format.matches(basename)) {
          if (rom != null) throw new IOException("ZIP 含多份可启动卡带，无法自动选择；请手动导入");
          if (entry.getSize() == 0 || entry.getSize() > maxRomBytes)
            throw new IOException("ZIP 内卡带声明大小超出平台限制");
          rom = entry;
          romBasename = basename;
        } else if (noteName(basename)) {
          notesBytes += entry.getSize();
          if (entry.getSize() > MAX_NOTE_BYTES
              || notesBytes > MAX_NOTES_BYTES
              || notes.size() >= MAX_NOTES) throw new IOException("ZIP 附带说明文件超出大小或数量限制");
          notes.add(entry);
        } else {
          throw new IOException(
              "ZIP 含其他文件或多文件依赖（" + basename + "）；仅自动提取唯一卡带及少量 README / LICENSE 文字说明，请手动核对");
        }
      }
      if (entryCount != expectedEntries) throw new IOException("ZIP 条目数量不一致");
      if (rom == null) throw new IOException("ZIP 中没有匹配此平台的独立卡带");
      // Validate companion files too, but never write them to disk or display their contents.
      for (ZipEntry note : notes) readEntry(zip, note, null, null, MAX_NOTE_BYTES);
      File directory = cacheDirectory(cacheDir);
      File pending = File.createTempFile("pending-", ".tmp", directory);
      try {
        MessageDigest sha = MessageDigest.getInstance("SHA-256");
        try (OutputStream out = new FileOutputStream(pending)) {
          readEntry(zip, rom, out, sha, maxRomBytes);
        }
        format.validate(pending);
        if (source.length() != physicalSize || source.lastModified() != modified)
          throw new IOException("解包期间源 ZIP 已变化，请重新检查下载");
        String hash = FbneoArchive.hex(sha.digest());
        String suffix =
            romBasename.substring(romBasename.lastIndexOf('.')).toLowerCase(Locale.ROOT);
        String id = format.id;
        File ready = new File(directory, "cart-" + id + "-" + hash + suffix);
        interrupted();
        publish(pending, ready, hash, rom.getSize(), maxRomBytes, format);
        return new Extracted(ready, romBasename, true);
      } finally {
        // No pending file survives cancellation, invalid CRC/header, or a cache conflict.
        Files.deleteIfExists(pending.toPath());
      }
    }
  }

  private static boolean romName(String name, boolean nes, CartridgePolicy.Spec spec) {
    if (!safeBasename(name)) return false;
    if (nes) return name.length() > 4 && name.toLowerCase(Locale.ROOT).endsWith(".nes");
    return CartridgePolicy.formatReason(spec.id, name).isEmpty();
  }

  private static boolean noteName(String basename) {
    String name = basename.toLowerCase(Locale.ROOT);
    return name.matches(
        "(readme|read me|license|licence|copying|authors|credits|说明|使用说明)"
            + "([ ._-][^/\\\\:]*)?\\.(txt|nfo)");
  }

  private static boolean safeBasename(String name) {
    return name != null
        && !name.isEmpty()
        && name.length() <= 1024
        && !name.equals(".")
        && !name.equals("..")
        && name.codePoints()
            .noneMatch(c -> c == '/' || c == '\\' || c == ':' || Character.isISOControl(c));
  }

  private static String memberBasename(String name, boolean directory) throws IOException {
    if (name == null
        || name.length() > 4096
        || name.isEmpty()
        || name.startsWith("/")
        || name.indexOf('\\') >= 0
        || name.indexOf(':') >= 0
        || name.codePoints().anyMatch(Character::isISOControl)) throw new IOException("ZIP 条目路径无效");
    String path = directory ? name.substring(0, name.length() - 1) : name;
    String[] segments = path.split("/", -1);
    for (String segment : segments)
      if (!safeBasename(segment)) throw new IOException("ZIP 含绝对路径、空路径或上级目录");
    return segments[segments.length - 1];
  }

  private static void readEntry(
      ZipFile zip, ZipEntry entry, OutputStream out, MessageDigest sha, long maximum)
      throws IOException {
    CRC32 crc = new CRC32();
    long count = 0;
    byte[] buffer = new byte[32768];
    try (InputStream in = zip.getInputStream(entry)) {
      int length;
      while ((length = in.read(buffer)) != -1) {
        interrupted();
        count += length;
        if (count > maximum || count > entry.getSize())
          throw new IOException("ZIP 实际展开大小超出限制或与声明不符");
        if (out == null && sha == null) {
          for (int i = 0; i < length; i++) {
            int c = buffer[i] & 255;
            if (c < 32 && c != '\t' && c != '\n' && c != '\r')
              throw new IOException("ZIP 附带说明不是普通文本，需手动核对");
          }
        }
        crc.update(buffer, 0, length);
        if (sha != null) sha.update(buffer, 0, length);
        if (out != null) out.write(buffer, 0, length);
      }
    }
    if (count != entry.getSize() || crc.getValue() != entry.getCrc())
      throw new IOException("ZIP 实际大小或 CRC 不符，已保留原压缩包");
  }

  private static void validate(File file, boolean nes, CartridgePolicy.Spec spec)
      throws IOException {
    interrupted();
    if (!nes) {
      CartridgePolicy.validateFile(spec, file);
      return;
    }
    byte[] header = new byte[16];
    try (DataInputStream in = new DataInputStream(new FileInputStream(file))) {
      in.readFully(header);
    }
    RetroArchPolicy.validateHeader(header, file.length());
  }

  private static File cacheDirectory(File cacheDir) throws IOException {
    if (cacheDir == null || !cacheDir.isDirectory()) throw new IOException("应用缓存目录不可用");
    File directory = new File(cacheDir, "cartridge-roms");
    if (Files.isSymbolicLink(directory.toPath())) throw new IOException("卡带缓存目录不能是符号链接");
    if (!directory.isDirectory() && !directory.mkdir() && !directory.isDirectory())
      throw new IOException("无法创建卡带缓存目录");
    return directory;
  }

  private static synchronized void publish(
      File pending, File ready, String hash, long size, long maximum, Format format)
      throws Exception {
    // Cancellation may arrive while waiting to enter this monitor.
    interrupted();
    try {
      // Android SELinux forbids app-data hard links. Same-directory move publishes the closed
      // file; no REPLACE_EXISTING/ATOMIC_MOVE (the latter may overwrite on Unix). This monitor
      // serializes all publishers in our single-process, app-private cache.
      interrupted();
      Files.move(pending.toPath(), ready.toPath());
    } catch (FileAlreadyExistsException exists) {
      if (!Files.isRegularFile(ready.toPath(), LinkOption.NOFOLLOW_LINKS) || ready.length() != size)
        throw new IOException("已有卡带缓存异常；请清理该副本后重试，不会覆盖", exists);
      MessageDigest sha = MessageDigest.getInstance("SHA-256");
      byte[] buffer = new byte[32768];
      long count = 0;
      try (InputStream in = new FileInputStream(ready)) {
        int length;
        while ((length = in.read(buffer)) != -1) {
          interrupted();
          count += length;
          if (count > size || count > maximum) throw new IOException("已有卡带缓存大小异常，不会覆盖");
          sha.update(buffer, 0, length);
        }
      }
      if (count != size || !hash.equals(FbneoArchive.hex(sha.digest())))
        throw new IOException("已有卡带缓存内容已变化；请清理该副本后重试，不会覆盖");
      format.validate(ready);
    } catch (UnsupportedOperationException unsupported) {
      throw new IOException("缓存文件系统不支持安全发布卡带副本", unsupported);
    }
    interrupted();
  }

  /**
   * The shared preflight bounds these records first; additionally reject symlinks/device entries.
   */
  private static void checkRegularEntries(File archive, int count) throws IOException {
    try (RandomAccessFile file = new RandomAccessFile(archive, "r")) {
      byte[] tail = new byte[(int) Math.min(file.length(), 65557)];
      file.seek(file.length() - tail.length);
      file.readFully(tail);
      int end = -1;
      for (int i = tail.length - 22; i >= 0; i--) {
        if (u32(tail, i) == 0x06054b50L && i + 22 + u16(tail, i + 20) == tail.length) {
          end = i;
          break;
        }
      }
      if (end < 0) throw new IOException("ZIP 结束记录已变化");
      file.seek(u32(tail, end + 16));
      byte[] header = new byte[46];
      for (int i = 0; i < count; i++) {
        interrupted();
        file.readFully(header);
        if (u32(header, 0) != 0x02014b50L) throw new IOException("ZIP 中央目录已变化");
        int type = (int) ((u32(header, 38) >>> 16) & 0xf000);
        if (type != 0 && type != 0x8000 && type != 0x4000)
          throw new IOException("ZIP 含符号链接或特殊文件，不能自动提取");
        int nameLength = u16(header, 28);
        if (nameLength == 0) throw new IOException("ZIP 条目名为空");
        file.skipBytes(nameLength - 1);
        boolean directory = file.readUnsignedByte() == '/';
        if ((type == 0x4000 || (u32(header, 38) & 0x10) != 0) && !directory
            || type == 0x8000 && directory) throw new IOException("ZIP 条目类型与路径不符");
        file.seek(file.getFilePointer() + u16(header, 30) + u16(header, 32));
      }
    }
  }

  private static int u16(byte[] bytes, int at) {
    return (bytes[at] & 255) | (bytes[at + 1] & 255) << 8;
  }

  private static long u32(byte[] bytes, int at) {
    return u16(bytes, at) | (long) u16(bytes, at + 2) << 16;
  }

  private static void interrupted() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw new IOException("卡带解包已取消");
  }
}
