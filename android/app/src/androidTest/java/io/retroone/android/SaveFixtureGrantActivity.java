package io.retroone.android;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.provider.DocumentsContract;

/** Grants only the test provider's private, synthetic tree to the target APK. */
public class SaveFixtureGrantActivity extends Activity {
  public void onCreate(Bundle state) {
    super.onCreate(state);
    grantUriPermission(
        "io.retroone.android",
        DocumentsContract.buildTreeDocumentUri("io.retroone.android.test.saves", "root"),
        Intent.FLAG_GRANT_READ_URI_PERMISSION
            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
    finish();
  }
}
