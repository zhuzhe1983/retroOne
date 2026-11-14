package io.retroone.android;

import android.database.*;
import android.os.*;
import android.provider.DocumentsContract.Document;
import android.provider.DocumentsProvider;
import java.io.*;

/**
 * A real DocumentsProvider in the test APK; all data lives in its own private fixture directory.
 */
public class SaveFixtureProvider extends DocumentsProvider {
  private File root;
  private boolean failRename;

  public boolean onCreate() {
    root = new File(getContext().getFilesDir(), "SAVEDATA");
    root.mkdirs();
    return true;
  }

  private File file(String id) throws FileNotFoundException {
    try {
      File f = id.equals("root") ? root : new File(root, id);
      if (!f.getCanonicalPath().equals(root.getCanonicalPath())
          && !f.getCanonicalPath().startsWith(root.getCanonicalPath() + "/"))
        throw new IOException();
      return f;
    } catch (Exception e) {
      throw new FileNotFoundException("Invalid fixture path");
    }
  }

  private String id(File f) {
    return f.equals(root) ? "root" : root.toPath().relativize(f.toPath()).toString();
  }

  private MatrixCursor cursor(String[] projection) {
    return new MatrixCursor(
        projection == null
            ? new String[] {
              Document.COLUMN_DOCUMENT_ID,
              Document.COLUMN_DISPLAY_NAME,
              Document.COLUMN_MIME_TYPE,
              Document.COLUMN_FLAGS,
              Document.COLUMN_SIZE,
              Document.COLUMN_LAST_MODIFIED
            }
            : projection);
  }

  private void row(MatrixCursor c, File f) {
    MatrixCursor.RowBuilder r = c.newRow();
    for (String key : c.getColumnNames())
      r.add(
          key,
          switch (key) {
            case Document.COLUMN_DOCUMENT_ID -> id(f);
            case Document.COLUMN_DISPLAY_NAME -> f.getName();
            case Document.COLUMN_MIME_TYPE ->
                f.isDirectory() ? Document.MIME_TYPE_DIR : "application/octet-stream";
            case Document.COLUMN_FLAGS ->
                Document.FLAG_SUPPORTS_WRITE
                    | Document.FLAG_SUPPORTS_DELETE
                    | Document.FLAG_SUPPORTS_RENAME
                    | (f.isDirectory() ? Document.FLAG_DIR_SUPPORTS_CREATE : 0);
            case Document.COLUMN_SIZE -> f.length();
            case Document.COLUMN_LAST_MODIFIED -> f.lastModified();
            default -> null;
          });
  }

  public Cursor queryRoots(String[] projection) {
    return new MatrixCursor(projection == null ? new String[] {"root_id"} : projection);
  }

  public Cursor queryDocument(String id, String[] projection) throws FileNotFoundException {
    MatrixCursor c = cursor(projection);
    File f = file(id);
    if (f.exists()) row(c, f);
    return c;
  }

  public Cursor queryChildDocuments(String parent, String[] projection, String sort)
      throws FileNotFoundException {
    MatrixCursor c = cursor(projection);
    File[] children = file(parent).listFiles();
    if (children != null) for (File f : children) row(c, f);
    return c;
  }

  public ParcelFileDescriptor openDocument(String id, String mode, CancellationSignal signal)
      throws FileNotFoundException {
    return ParcelFileDescriptor.open(file(id), ParcelFileDescriptor.parseMode(mode));
  }

  public String createDocument(String parent, String mime, String name)
      throws FileNotFoundException {
    if (name.contains("/") || name.equals("..")) throw new FileNotFoundException();
    File f = new File(file(parent), name);
    try {
      if (f.exists() || !(mime.equals(Document.MIME_TYPE_DIR) ? f.mkdir() : f.createNewFile()))
        throw new IOException();
      return id(f);
    } catch (IOException e) {
      throw new FileNotFoundException();
    }
  }

  public String renameDocument(String id, String name) throws FileNotFoundException {
    if (failRename && name.equals("TEST00001_GameData")) {
      failRename = false;
      throw new FileNotFoundException("Injected commit failure");
    }
    File old = file(id), next = new File(old.getParentFile(), name);
    if (name.contains("/") || next.exists() || !old.renameTo(next))
      throw new FileNotFoundException();
    return id(next);
  }

  public void deleteDocument(String id) throws FileNotFoundException {
    erase(file(id));
  }

  private void erase(File f) throws FileNotFoundException {
    File[] kids = f.listFiles();
    if (kids != null) for (File kid : kids) erase(kid);
    if (!f.delete()) throw new FileNotFoundException();
  }

  public boolean isChildDocument(String parent, String child) {
    try {
      return file(child).toPath().startsWith(file(parent).toPath());
    } catch (FileNotFoundException e) {
      return false;
    }
  }

  @Override
  public Bundle call(String method, String arg, Bundle extras) {
    if ("testFailRename".equals(method)) {
      failRename = true;
      return new Bundle();
    }
    return super.call(method, arg, extras);
  }
}
