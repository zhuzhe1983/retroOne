package io.retroone.android;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import java.io.File;
import java.io.IOException;
import java.io.InterruptedIOException;
import java.io.RandomAccessFile;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONObject;

public final class DownloadService extends Service {
  static final String PAUSE = "pause", RESUME = "resume";
  private final ExecutorService executor = Executors.newSingleThreadExecutor();
  private final Handler main = new Handler(Looper.getMainLooper());
  private final Set<String> paused = ConcurrentHashMap.newKeySet();
  private volatile boolean stopping;
  private boolean working;
  private String active;
  private DownloadStore store;
  private PowerManager.WakeLock wake;
  private long renewWakeAt;

  static void command(android.content.Context context, String command, String id) {
    Intent intent = new Intent(context, DownloadService.class).setAction(command);
    if (id != null) intent.putExtra("id", id);
    context.startForegroundService(intent);
  }

  @Override
  public void onCreate() {
    super.onCreate();
    store = RetroApplication.from(this).downloads;
    NotificationManager nm = getSystemService(NotificationManager.class);
    nm.createNotificationChannel(
        new NotificationChannel("downloads", "游戏文件下载", NotificationManager.IMPORTANCE_LOW));
    wake =
        getSystemService(PowerManager.class)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "RetroOne:download");
    wake.setReferenceCounted(false);
    if (Build.VERSION.SDK_INT >= 29)
      startForeground(1, notification("正在准备下载"), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
    else startForeground(1, notification("正在准备下载"));
  }

  private Notification notification(String text) {
    PendingIntent open =
        PendingIntent.getActivity(
            this,
            0,
            new Intent(this, MainActivity.class).putExtra("downloads", true),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    PendingIntent pause =
        PendingIntent.getService(
            this,
            1,
            new Intent(this, DownloadService.class).setAction("pauseAll"),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    return new Notification.Builder(this, "downloads")
        .setSmallIcon(io.retroone.android.R.drawable.ic_retroone)
        .setContentTitle("RetroOne 下载管理")
        .setContentText(text)
        .setContentIntent(open)
        .setOngoing(true)
        .addAction(new Notification.Action.Builder(null, "暂停全部", pause).build())
        .build();
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    if (intent != null) {
      String id = intent.getStringExtra("id"), action = intent.getAction();
      if (PAUSE.equals(action) && id != null) pause(id);
      if ("pauseAll".equals(action)) for (DownloadStore.Item item : store.all()) pause(item.id);
      if (RESUME.equals(action) && id != null) {
        DownloadStore.Item item = store.get(id);
        if (item != null
            && (DownloadStore.PAUSED.equals(item.state)
                || DownloadStore.FAILED.equals(item.state))) {
          paused.remove(id);
          store.state(id, DownloadStore.QUEUED, "");
        }
      }
    }
    schedule();
    return START_NOT_STICKY;
  }

  private void pause(String id) {
    DownloadStore.Item item = store.get(id);
    if (item == null) return;
    if (id.equals(active)) paused.add(id);
    else if (DownloadStore.QUEUED.equals(item.state)) store.state(id, DownloadStore.PAUSED, "已暂停");
  }

  private void schedule() {
    if (working || stopping) return;
    DownloadStore.Item next = null;
    // Oldest first, stable queue. New work cannot starve an older request.
    for (DownloadStore.Item item : store.all())
      if (DownloadStore.QUEUED.equals(item.state)) next = item;
    if (next == null) {
      stopForeground(STOP_FOREGROUND_REMOVE);
      stopSelf();
      return;
    }
    working = true;
    active = next.id;
    DownloadStore.Item item = next;
    executor.execute(
        () -> {
          wake.acquire(30 * 60 * 1000L);
          renewWakeAt = android.os.SystemClock.elapsedRealtime() + 10 * 60 * 1000L;
          try {
            transfer(item);
          } catch (InterruptedIOException e) {
            store.state(
                item.id,
                canceled(item.id) ? DownloadStore.PAUSED : DownloadStore.FAILED,
                canceled(item.id) ? "已暂停，继续时会校验本机分片" : ApiClient.message(e));
          } catch (Exception e) {
            store.state(item.id, DownloadStore.FAILED, ApiClient.message(e));
          } finally {
            if (wake.isHeld()) wake.release();
            main.post(
                () -> {
                  working = false;
                  active = null;
                  if (!stopping) schedule();
                });
          }
        });
  }

  private boolean canceled(String id) {
    if (stopping || paused.contains(id)) return true;
    long now = android.os.SystemClock.elapsedRealtime();
    if (now >= renewWakeAt) {
      // Bound every acquisition, but keep long, actively progressing transfers awake.
      wake.acquire(30 * 60 * 1000L);
      renewWakeAt = now + 10 * 60 * 1000L;
    }
    return false;
  }

  private void check(String id) throws InterruptedIOException {
    if (canceled(id)) throw new InterruptedIOException();
  }

  private void transfer(DownloadStore.Item item) throws Exception {
    store.state(item.id, DownloadStore.PREPARING, "检查下载资格与版本");
    ServerConfig config = RetroApplication.from(this).credentials.load(item.base, item.http);
    ApiClient api = new ApiClient(config);
    String endpoint = "games/" + ServerConfig.encode(item.game);
    DownloadCapability options =
        new DownloadCapability(api.json(config.api(endpoint + "/download-options")));
    if (!options.available) throw new IOException(options.explanation());
    long deadline = System.currentTimeMillis() + 30 * 60 * 1000L;
    JSONObject json;
    while (true) {
      check(item.id);
      json = api.json(config.api(endpoint + "/download-manifest?transport=http"));
      if (!"preparing".equals(json.optString("status"))) break;
      store.state(
          item.id,
          DownloadStore.PREPARING,
          "服务器正在生成分片校验：" + MainActivity.bytes(json.optLong("bytes_hashed")));
      if (System.currentTimeMillis() > deadline) throw new IOException("服务器准备时间较长，请稍后继续");
      for (int i = 0; i < 20; i++) {
        check(item.id);
        Thread.sleep(100);
      }
    }
    DownloadManifest manifest = new DownloadManifest(json, config);
    if (!item.manifest.isEmpty()) {
      DownloadManifest old = new DownloadManifest(new JSONObject(item.manifest), config);
      if (!old.revision.equals(manifest.revision)
          || !old.name.equals(manifest.name)
          || old.size != manifest.size) throw new IOException("服务器文件版本已变化；保留旧任务，请移除后重新下载");
    }
    store.manifest(item.id, json, manifest);
    item = store.get(item.id);
    final String id = item.id;
    store.state(id, DownloadStore.RUNNING, "校验本机分片并续传");
    File partial = store.partial(item), complete = store.complete(item);
    if (complete.exists()) {
      try (RandomAccessFile file = new RandomAccessFile(complete, "r")) {
        if (file.length() != manifest.size
            || Transfer.verifiedPrefix(file, manifest, () -> canceled(id)) != manifest.size)
          throw new IOException("本机文件不完整或校验失败；请移除此副本后重新下载");
      }
    } else {
      Transfer.download(
          api,
          manifest,
          partial,
          () -> canceled(id),
          done -> {
            store.progress(id, done);
            getSystemService(NotificationManager.class)
                .notify(
                    1,
                    notification(
                        MainActivity.bytes(done) + " / " + MainActivity.bytes(manifest.size)));
          });
      check(id);
      if (complete.exists() || !partial.renameTo(complete))
        throw new IOException("下载完成，但本机文件无法安全落盘，请重试");
    }
    store.progress(id, manifest.size);
    store.state(id, DownloadStore.READY, "分片校验通过 · 可离线使用");
  }

  @Override
  public void onTimeout(int startId, int type) {
    stopping = true;
    stopSelf();
  }

  @Override
  public void onDestroy() {
    stopping = true;
    executor.shutdown();
    super.onDestroy();
  }

  @Override
  public IBinder onBind(Intent intent) {
    return null;
  }
}
