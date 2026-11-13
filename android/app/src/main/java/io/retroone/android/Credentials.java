package io.retroone.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Credentials are kept out of the queue database, backups, URLs, and logs. */
final class Credentials {
  private final SharedPreferences prefs;

  Credentials(Context context) {
    prefs = context.getSharedPreferences("connections", Context.MODE_PRIVATE);
  }

  private SecretKey key() throws Exception {
    KeyStore store = KeyStore.getInstance("AndroidKeyStore");
    store.load(null);
    if (!store.containsAlias("retroone.connection")) {
      KeyGenerator generator =
          KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
      generator.init(
          new KeyGenParameterSpec.Builder(
                  "retroone.connection",
                  KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
              .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
              .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
              .build());
      generator.generateKey();
    }
    return (SecretKey) store.getKey("retroone.connection", null);
  }

  synchronized void save(ServerConfig config) throws Exception {
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(Cipher.ENCRYPT_MODE, key());
    String secret =
        Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
            + ":"
            + Base64.encodeToString(
                cipher.doFinal(config.authorization.getBytes(StandardCharsets.UTF_8)),
                Base64.NO_WRAP);
    if (!prefs
        .edit()
        .putString("auth." + config.key(), secret)
        .putString("base", config.base)
        .putBoolean("http", config.allowHttp)
        .commit()) throw new Exception("连接设置未保存");
  }

  String base() {
    return prefs.getString("base", "");
  }

  boolean http() {
    return prefs.getBoolean("http", false);
  }

  synchronized ServerConfig load(String base, boolean http) throws Exception {
    ServerConfig empty = new ServerConfig(base, http, "");
    String secret = prefs.getString("auth." + empty.key(), "");
    if (secret.isEmpty()) return empty;
    String[] parts = secret.split(":", 2);
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(
        Cipher.DECRYPT_MODE,
        key(),
        new GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)));
    return new ServerConfig(
        base,
        http,
        new String(
            cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8));
  }

  ServerConfig current() throws Exception {
    return load(base(), http());
  }
}
