package io.retroone.android;

import android.app.Application;

public final class RetroApplication extends Application {
  DownloadStore downloads;
  Credentials credentials;

  @Override
  public void onCreate() {
    super.onCreate();
    credentials = new Credentials(this);
    downloads = new DownloadStore(this);
    downloads.recover();
  }

  static RetroApplication from(android.content.Context context) {
    return (RetroApplication) context.getApplicationContext();
  }
}
