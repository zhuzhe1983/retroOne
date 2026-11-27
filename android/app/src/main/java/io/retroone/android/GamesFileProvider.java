package io.retroone.android;

import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import androidx.core.content.FileProvider;
import java.util.ArrayList;
import java.util.List;

/** FileProvider metadata with the MIME column requested by Dolphin's ContentHandler. */
public final class GamesFileProvider extends FileProvider {
  @Override
  public Cursor query(
      Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
    // Always let FileProvider resolve and validate the URI first, including MIME-only queries.
    Cursor original = super.query(uri, projection, selection, selectionArgs, sortOrder);
    boolean wantsMime = false;
    if (projection != null)
      for (String column : projection)
        if (DocumentsContract.Document.COLUMN_MIME_TYPE.equals(column)) wantsMime = true;
    // Preserve the default projection and all existing PSP/Eden query behavior exactly.
    if (!wantsMime) return original;

    try (Cursor cursor = original) {
      List<String> columns = new ArrayList<>();
      for (String column : projection) {
        if (DocumentsContract.Document.COLUMN_MIME_TYPE.equals(column)
            || OpenableColumns.DISPLAY_NAME.equals(column)
            || OpenableColumns.SIZE.equals(column)) columns.add(column);
      }
      MatrixCursor result = new MatrixCursor(columns.toArray(new String[0]), 1);
      if (cursor.moveToFirst()) {
        Object[] values = new Object[columns.size()];
        String mimeType = super.getType(uri);
        for (int i = 0; i < columns.size(); i++) {
          String column = columns.get(i);
          if (DocumentsContract.Document.COLUMN_MIME_TYPE.equals(column)) values[i] = mimeType;
          else {
            int index = cursor.getColumnIndexOrThrow(column);
            values[i] =
                cursor.isNull(index)
                    ? null
                    : OpenableColumns.SIZE.equals(column)
                        ? cursor.getLong(index)
                        : cursor.getString(index);
          }
        }
        result.addRow(values);
      }
      // Unknown columns remain omitted, just as in FileProvider; no file paths are exposed.
      return result;
    }
  }
}
