package io.retroone.android;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.*;
import org.json.*;

/** Complete content admission; dependency metadata alone never proves a missing parent ZIP. */
final class FbneoRomSet {
  static final String VERSION = "fbneo-romsets-v2";
  private static final long MAX_EXPANDED = 2L * 1024 * 1024 * 1024;
  private static final String BIOS_SOURCE =
      "7e51b413aab7da1965ba2d003d0b76fd7e5dfac5a9f82ea356fc74b0680602b7";

  static final class MissingBiosException extends FbneoArchive.MatchException {
    final List<String> missingNames;

    MissingBiosException(Collection<String> names) {
      super("缺少 Neo Geo 基础 BIOS：" + String.join("、", names) + "。请选择自己的 neogeo.zip；不会下载或改写 BIOS。");
      missingNames = immutable(names);
    }
  }

  static final class Match {
    final String set, archiveSha256, biosSha256;
    final boolean merged, neoGeo;
    final List<String> missingBios, includedBios;

    private Match(
        String set,
        String archiveSha256,
        boolean merged,
        boolean neoGeo,
        Collection<String> missingBios,
        Collection<String> includedBios,
        String biosSha256) {
      this.set = set;
      this.archiveSha256 = archiveSha256;
      this.merged = merged;
      this.neoGeo = neoGeo;
      this.missingBios = immutable(missingBios);
      this.includedBios = immutable(includedBios);
      this.biosSha256 = biosSha256;
    }
  }

  static final class Bios {
    final String sha256;
    final List<String> presentMembers, optionalMembers;

    private Bios(String sha256, Collection<String> present, Collection<String> optional) {
      this.sha256 = sha256;
      presentMembers = immutable(present);
      optionalMembers = immutable(optional);
    }
  }

  private static final class Entry {
    final String path, signature;
    final boolean directory;

    Entry(String path, long crc, long size, boolean directory) {
      this.path = path;
      this.signature = memberSignature(crc, size);
      this.directory = directory;
    }
  }

  private static final class Archive {
    final String sha256;
    final List<Entry> entries;

    Archive(String sha256, List<Entry> entries) {
      this.sha256 = sha256;
      this.entries = entries;
    }
  }

  private static final class BiosMember {
    final String name;
    final boolean required;

    BiosMember(String name, boolean required) {
      this.name = name;
      this.required = required;
    }
  }

  private static final class Index {
    final JSONObject catalog, signatures;
    final JSONArray members;
    final Map<String, BiosMember> bios = new LinkedHashMap<>();
    final List<String> required = new ArrayList<>();

    Index(JSONObject index) throws Exception {
      if (!FbneoArchive.DAT.equals(index.optString("source_sha256"))
          || index.optInt("schema_version") != 2
          || !VERSION.equals(index.optString("matcher_version"))
          || !BIOS_SOURCE.equals(index.optString("bios_source_sha256")))
        throw new IOException("ROM 集索引版本无效");
      catalog = index.getJSONObject("catalog");
      signatures = index.getJSONObject("signatures");
      members = index.getJSONArray("members");
      JSONArray rows = index.getJSONArray("neogeo_bios");
      Set<String> names = new HashSet<>();
      for (int i = 0; i < rows.length(); i++) {
        JSONArray row = rows.getJSONArray(i);
        String name = row.getString(0), signature = indexedSignature(row);
        boolean needed = row.getBoolean(3);
        if (!safeLeaf(name)
            || !names.add(name.toLowerCase(Locale.ROOT))
            || bios.put(signature, new BiosMember(name, needed)) != null)
          throw new IOException("Neo Geo BIOS 索引成员不唯一");
        if (needed) required.add(name);
      }
      if (required.size() != 4 || rows.length() > 64) throw new IOException("Neo Geo 基础 BIOS 索引无效");
    }

    JSONArray set(String name) throws Exception {
      if (!safeSet(name)) throw new IOException("ROM 集标识无效");
      JSONArray row = catalog.optJSONArray(name);
      if (row == null || row.length() != 4) throw new IOException("ROM 集关联索引不完整");
      return row;
    }

    List<String> missing(Collection<String> present) {
      List<String> missing = new ArrayList<>(required);
      missing.removeAll(present);
      return missing;
    }
  }

  private FbneoRomSet() {}

  static Match inspect(File archive, JSONObject index) throws Exception {
    Index lookup = new Index(index);
    Archive contents = readArchive(archive);
    List<String> allRoot = new ArrayList<>(), gameRoot = new ArrayList<>();
    Set<String> included = new LinkedHashSet<>();
    for (Entry entry : contents.entries) {
      if (entry.directory || entry.path.contains("/")) continue;
      allRoot.add(entry.signature);
      BiosMember bios = lookup.bios.get(entry.signature);
      if (bios == null) gameRoot.add(entry.signature);
      else if (!included.add(bios.name))
        throw new FbneoArchive.MatchException("同包 BIOS 内容重复，不能唯一核对成员");
    }
    String rawSignature = signature(allRoot), neoSignature = signature(gameRoot);
    Set<String> candidates = new LinkedHashSet<>();
    addCandidates(lookup, rawSignature, false, candidates);
    addCandidates(lookup, neoSignature, true, candidates);
    if (candidates.isEmpty()) {
      if (!included.isEmpty() && gameRoot.isEmpty())
        throw new FbneoArchive.MatchException("此归档只有 BIOS 内容，不能作为游戏启动");
      throw new FbneoArchive.MatchException(
          "归档未匹配当前核心／索引的完整 ROM 集，需核对版本或依赖。可能是 MOD／改版、" + "分离集缺少父集内容，或 ROM 版本不符；已保留原文件。");
    }
    if (candidates.size() != 1)
      throw new FbneoArchive.MatchException("归档根目录对应多个 ROM 集，无法唯一确定启动名称；请手动导入");
    String name = candidates.iterator().next();
    JSONArray definition = lookup.set(name);
    int flags = definition.getInt(2);
    String bios = definition.getString(1);
    if ((flags & 1) != 0) throw new FbneoArchive.MatchException("BIOS 归档不能作为游戏启动");
    if ((flags & 2) != 0) throw new FbneoArchive.MatchException("此 ROM 集的索引成员尚不能完整核对，请手动导入");
    if (!bios.isEmpty() && !bios.equals("neogeo"))
      throw new FbneoArchive.MatchException("此 ROM 集需要尚未接入的 BIOS：" + bios + "；请手动导入");
    boolean neoGeo = bios.equals("neogeo");
    boolean merged = validateMerged(contents.entries, name, bios, lookup);
    return new Match(
        name,
        contents.sha256,
        merged,
        neoGeo,
        neoGeo ? lookup.missing(included) : Collections.emptyList(),
        neoGeo ? included : Collections.emptyList(),
        "");
  }

  static Bios verifyBios(File archive, JSONObject index) throws Exception {
    Index lookup = new Index(index);
    Bios bios = readBios(archive, lookup);
    List<String> missing = lookup.missing(bios.presentMembers);
    if (!missing.isEmpty()) throw new MissingBiosException(missing);
    return bios;
  }

  static Match resolve(File archive, File biosOrNull, JSONObject index) throws Exception {
    return resolve(inspect(archive, index), biosOrNull, index);
  }

  static Match resolve(Match inspected, File biosOrNull, JSONObject index) throws Exception {
    Index lookup = new Index(index);
    if (!inspected.neoGeo || inspected.missingBios.isEmpty()) return inspected;
    if (biosOrNull == null) throw new MissingBiosException(inspected.missingBios);
    Bios external = readBios(biosOrNull, lookup);
    Set<String> union = new LinkedHashSet<>(inspected.includedBios);
    union.addAll(external.presentMembers);
    List<String> missing = lookup.missing(union);
    if (!missing.isEmpty()) throw new MissingBiosException(missing);
    return new Match(
        inspected.set,
        inspected.archiveSha256,
        inspected.merged,
        true,
        Collections.emptyList(),
        inspected.includedBios,
        external.sha256);
  }

  static void requireBios(Match inspected, File biosOrNull, JSONObject index) throws Exception {
    resolve(inspected, biosOrNull, index);
  }

  private static void addCandidates(
      Index index, String signature, boolean neo, Set<String> candidates) throws Exception {
    JSONArray rows = index.signatures.optJSONArray(signature);
    if (rows == null) return;
    for (int i = 0; i < rows.length(); i++) {
      String name = rows.getString(i);
      JSONArray definition = index.set(name);
      if (definition.getString(1).equals("neogeo") == neo) candidates.add(name);
    }
  }

  private static boolean validateMerged(List<Entry> entries, String base, String bios, Index index)
      throws Exception {
    boolean merged = false;
    Map<String, Map<String, String>> allowed = new HashMap<>();
    for (Entry entry : entries) {
      int slash = entry.path.indexOf('/');
      if (slash < 0) continue;
      String child = entry.path.substring(0, slash);
      if (!allowed.containsKey(child)) {
        JSONArray definition = index.set(child);
        if ((definition.getInt(2) & 3) != 0
            || !bios.equals(definition.getString(1))
            || !descendsFrom(child, base, index))
          throw new FbneoArchive.MatchException("合并归档包含无关或未支持的子集：" + child);
        Map<String, String> members = new HashMap<>();
        JSONArray ids = definition.getJSONArray(3);
        for (int i = 0; i < ids.length(); i++) {
          JSONArray member = index.members.getJSONArray(ids.getInt(i));
          String name = member.getString(0);
          if (!safeLeaf(name) || members.put(name, indexedSignature(member)) != null)
            throw new IOException("合并 ROM 集索引成员不唯一");
        }
        allowed.put(child, members);
      }
      if (entry.directory) continue;
      String leaf = entry.path.substring(slash + 1);
      if (!entry.signature.equals(allowed.get(child).get(leaf)))
        throw new FbneoArchive.MatchException("合并子集成员的名称或版本不匹配：" + entry.path);
      merged = true;
    }
    return merged;
  }

  private static boolean descendsFrom(String child, String base, Index index) throws Exception {
    Set<String> seen = new HashSet<>();
    String next = child;
    while (!next.isEmpty() && seen.size() < 64) {
      if (!seen.add(next)) throw new IOException("ROM 集索引存在循环关联");
      String parent = index.set(next).getString(0);
      if (base.equals(parent)) return true;
      next = parent;
    }
    return false;
  }

  private static Bios readBios(File archive, Index index) throws Exception {
    Archive data = readArchive(archive);
    Set<String> present = new LinkedHashSet<>(), optional = new LinkedHashSet<>();
    for (Entry entry : data.entries) {
      if (entry.directory || entry.path.contains("/"))
        throw new FbneoArchive.MatchException("Neo Geo BIOS 归档仅接入根目录成员，不接入嵌套目录");
      BiosMember known = index.bios.get(entry.signature);
      if (known == null || !present.add(known.name))
        throw new FbneoArchive.MatchException("Neo Geo BIOS 含未知、版本不符或重复的成员：" + entry.path);
      if (!known.required) optional.add(known.name);
    }
    return new Bios(data.sha256, present, optional);
  }

  private static Archive readArchive(File archive) throws Exception {
    checkCancelled();
    int expected = FbneoArchive.checkLayout(archive);
    String before = sha256(archive);
    List<Entry> contents = new ArrayList<>();
    Set<String> paths = new HashSet<>();
    Set<String> rootFiles = new HashSet<>();
    long total = 0;
    byte[] buffer = new byte[65536];
    try (ZipFile zip = new ZipFile(archive)) {
      if (zip.size() != expected) throw new IOException("ZIP 条目数量已变化");
      Enumeration<? extends ZipEntry> entries = zip.entries();
      while (entries.hasMoreElements()) {
        checkCancelled();
        ZipEntry entry = entries.nextElement();
        String path = entry.getName();
        validatePath(path, entry.isDirectory());
        String normalized = entry.isDirectory() ? path.substring(0, path.length() - 1) : path;
        if (!paths.add(normalized.toLowerCase(Locale.ROOT)))
          throw new IOException("ZIP 成员路径重复或大小写冲突");
        if (!entry.isDirectory() && !path.contains("/"))
          rootFiles.add(path.toLowerCase(Locale.ROOT));
        long size = entry.getSize();
        if (size < 0
            || size > FbneoArchive.MAX_ARCHIVE_BYTES
            || entry.getCrc() < 0
            || (entry.isDirectory() && size != 0)) throw new IOException("ZIP 成员大小或 CRC 无效");
        total += size;
        if (total > MAX_EXPANDED) throw new IOException("归档展开量超出 2 GiB 校验范围");
        CRC32 crc = new CRC32();
        long count = 0;
        try (InputStream input = zip.getInputStream(entry)) {
          int n;
          while ((n = input.read(buffer)) != -1) {
            checkCancelled();
            count += n;
            if (count > size) throw new IOException("ZIP 成员实际大小超出声明");
            crc.update(buffer, 0, n);
          }
        }
        if (count != size || crc.getValue() != entry.getCrc())
          throw new IOException("ZIP 成员实际内容的大小或 CRC 不符");
        contents.add(new Entry(path, crc.getValue(), count, entry.isDirectory()));
      }
    }
    for (Entry entry : contents) {
      int slash = entry.path.indexOf('/');
      if (slash >= 0 && rootFiles.contains(entry.path.substring(0, slash).toLowerCase(Locale.ROOT)))
        throw new IOException("ZIP 文件与子集目录路径冲突");
    }
    if (!before.equals(sha256(archive))) throw new IOException("ROM 归档在校验期间发生变化，请重新检查");
    return new Archive(before, contents);
  }

  private static void validatePath(String path, boolean directory) throws IOException {
    String normalized = directory ? path.substring(0, path.length() - 1) : path;
    String[] parts = normalized.split("/", -1);
    if (parts.length < 1 || parts.length > 2 || (directory && parts.length != 1))
      throw new IOException("ZIP 路径不是根 ROM 或单层关联子集，不自动解包嵌套目录");
    for (String part : parts) if (!safeLeaf(part)) throw new IOException("ZIP 成员路径无效");
    if (parts.length == 2 && !safeSet(parts[0])) throw new IOException("ZIP 子集目录标识无效");
  }

  private static boolean safeLeaf(String name) {
    if (name.isEmpty() || name.length() > 255 || name.equals(".") || name.equals(".."))
      return false;
    for (int i = 0; i < name.length(); i++) {
      char c = name.charAt(i);
      if (c < 32 || c > 126 || c == '/' || c == '\\' || c == ':') return false;
    }
    return true;
  }

  private static boolean safeSet(String name) {
    return name.matches("[A-Za-z0-9_-]{1,64}");
  }

  private static String indexedSignature(JSONArray row) throws Exception {
    String crc = row.getString(1);
    long size = row.getLong(2);
    if (!crc.matches("[0-9a-f]{8}") || size < 0 || size > FbneoArchive.MAX_ARCHIVE_BYTES)
      throw new IOException("ROM 成员索引无效");
    return crc + ":" + size;
  }

  private static String memberSignature(long crc, long size) {
    return String.format(Locale.ROOT, "%08x:%d", crc, size);
  }

  private static String signature(Collection<String> members) throws Exception {
    List<String> sorted = new ArrayList<>(members);
    Collections.sort(sorted);
    return FbneoArchive.hex(
        MessageDigest.getInstance("SHA-256")
            .digest((String.join("\n", sorted) + "\n").getBytes(StandardCharsets.UTF_8)));
  }

  private static String sha256(File file) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    byte[] buffer = new byte[65536];
    long total = 0;
    try (InputStream input = new FileInputStream(file)) {
      int n;
      while ((n = input.read(buffer)) != -1) {
        checkCancelled();
        total += n;
        if (total > FbneoArchive.MAX_ARCHIVE_BYTES) throw new IOException("ZIP 超过物理大小上限");
        digest.update(buffer, 0, n);
      }
    }
    return FbneoArchive.hex(digest.digest());
  }

  private static List<String> immutable(Collection<String> values) {
    return Collections.unmodifiableList(new ArrayList<>(values));
  }

  private static void checkCancelled() throws IOException {
    if (Thread.currentThread().isInterrupted()) throw new IOException("ROM 集校验已取消");
  }
}
