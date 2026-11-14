package io.retroone.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.widget.*;
import java.io.File;
import java.util.*;
import java.util.concurrent.ExecutorService;

/** Storage management keeps ROM removal and immutable save-backup removal separate. */
final class LocalContentView extends LinearLayout {
  interface Actions {
    void play(DownloadStore.Item item);

    void exportGame(DownloadStore.Item item);

    void exportSave(SaveLibrary.Entry entry);

    void downloads();

    void emulator();
  }

  private final Activity activity;
  private final DownloadStore store;
  private final SaveLibrary saves;
  private final ExecutorService io;
  private final Actions actions;
  private final TextView capacity;
  private final LinearLayout tabs, list;
  private String section = "games", signature = "";
  private long nextCheck;
  private boolean loading;
  private List<DownloadStore.Item> games = new ArrayList<>();
  private List<SaveLibrary.Entry> backups = new ArrayList<>();

  LocalContentView(Activity activity, DownloadStore store, ExecutorService io, Actions actions) {
    super(activity);
    this.activity = activity;
    this.store = store;
    this.io = io;
    this.actions = actions;
    saves = new SaveLibrary(activity.getFilesDir());
    setOrientation(VERTICAL);
    TextView title = text("本地内容", 22, MainActivity.TEXT);
    title.setTypeface(null, Typeface.BOLD);
    addView(title);
    capacity = text("正在统计占用…", 13, MainActivity.MUTED);
    addView(capacity);
    tabs = new LinearLayout(activity);
    addView(tabs);
    renderTabs();
    ScrollView scroll = new ScrollView(activity);
    list = new LinearLayout(activity);
    list.setOrientation(VERTICAL);
    scroll.addView(list);
    addView(scroll, new LayoutParams(-1, 0, 1));
    refresh(true);
  }

  void refresh(boolean force) {
    if (loading || (!force && System.currentTimeMillis() < nextCheck)) return;
    loading = true;
    nextCheck = System.currentTimeMillis() + 2500;
    io.execute(
        () -> {
          try {
            List<DownloadStore.Item> items =
                store.all().stream()
                    .filter(i -> !DownloadStore.CANCELED.equals(i.state))
                    .collect(java.util.stream.Collectors.toList());
            List<SaveLibrary.Entry> entries = saves.all();
            long used = 0, saved = 0;
            StringBuilder stamp = new StringBuilder();
            for (DownloadStore.Item item : items) {
              File file =
                  DownloadStore.READY.equals(item.state)
                      ? store.complete(item)
                      : store.partial(item);
              long size = file.isFile() ? file.length() : 0;
              used += size;
              stamp.append(item.id).append(item.state).append(size).append(item.lastPlayed);
            }
            for (SaveLibrary.Entry entry : entries) {
              saved += entry.file.length();
              stamp.append(entry.file).append(entry.file.length());
            }
            long gameBytes = used, saveBytes = saved;
            activity.runOnUiThread(
                () -> {
                  loading = false;
                  if (!isAttachedToWindow()) return;
                  capacity.setText(
                      "游戏 "
                          + bytes(gameBytes)
                          + "  ·  存档备份 "
                          + bytes(saveBytes)
                          + "  ·  可用 "
                          + bytes(activity.getFilesDir().getUsableSpace()));
                  if (force || !stamp.toString().equals(signature)) {
                    games = items;
                    backups = entries;
                    signature = stamp.toString();
                    render();
                  }
                });
          } catch (Exception error) {
            activity.runOnUiThread(
                () -> {
                  loading = false;
                  if (isAttachedToWindow()) capacity.setText(ApiClient.message(error));
                });
          }
        });
  }

  void showSaves() {
    section = "saves";
    renderTabs();
    render();
  }

  private void renderTabs() {
    tabs.removeAllViews();
    for (String name : new String[] {"games", "saves"}) {
      Button button =
          button(
              name.equals("games") ? "游戏文件" : "存档",
              () -> {
                section = name;
                renderTabs();
                render();
              });
      button.setTextColor(section.equals(name) ? MainActivity.BG : MainActivity.ACCENT);
      button.setBackground(
          surface(section.equals(name) ? MainActivity.ACCENT : MainActivity.PANEL));
      tabs.addView(button);
    }
    tabs.addView(button("下载任务 ↗", actions::downloads));
  }

  private void render() {
    list.removeAllViews();
    if (section.equals("games")) {
      if (games.isEmpty()) list.addView(text("还没有本机游戏。下载完成后，可在「已下载」中游玩。", 16, MainActivity.MUTED));
      for (DownloadStore.Item item : games) {
        LinearLayout card = card(item.title);
        card.addView(
            text(
                item.platform.toUpperCase(Locale.ROOT)
                    + " · "
                    + bytes(item.bytes)
                    + " / "
                    + bytes(item.size),
                13,
                MainActivity.MUTED));
        LinearLayout controls = new LinearLayout(activity);
        card.addView(controls);
        if (DownloadStore.READY.equals(item.state)) {
          LaunchPolicy.State state =
              Emulators.status(activity, item, !Emulators.installed(activity).isEmpty());
          controls.addView(button(state.label, () -> actions.play(item)));
          controls.addView(button("导出", () -> actions.exportGame(item)));
        } else controls.addView(button("查看下载", actions::downloads));
        boolean active =
            List.of(DownloadStore.RUNNING, DownloadStore.QUEUED, DownloadStore.PREPARING)
                .contains(item.state);
        if (!active) {
          Button remove =
              button(
                  "移除",
                  () ->
                      new AlertDialog.Builder(activity)
                          .setTitle("移除「" + item.title + "」的游戏文件？")
                          .setMessage("只移除这份本机下载，保留所有存档与备份。请先退出正在使用该文件的模拟器。")
                          .setNegativeButton("保留", null)
                          .setPositiveButton("移除游戏文件", (d, w) -> work(() -> store.cancel(item.id)))
                          .show());
          remove.setContentDescription("移除游戏 " + item.title);
          controls.addView(remove);
        }
      }
    } else {
      LinearLayout help = new LinearLayout(activity);
      help.addView(
          button(
              "存档说明 ⓘ",
              () ->
                  new AlertDialog.Builder(activity)
                      .setTitle("存档与云端备份")
                      .setMessage(
                          "这里管理 RetroOne 保留的本机与云端存档备份。PSP 可在游戏详情的「云存档」中比较并同步；请先退出"
                              + " PPSSPP。尚未在游玩期间自动同步。\n\n"
                              + "每份备份独立保留所属玩家、游戏与云端版本；移除备份不会删除模拟器进度或云端存档。旧版备份若缺少身份信息会明确标注，不猜测归属。")
                      .setPositiveButton("知道了", null)
                      .show()));
      help.addView(button("打开 PPSSPP", actions::emulator));
      list.addView(help);
      if (backups.isEmpty())
        list.addView(text("暂无存档备份。可在游戏详情的「云存档」中保存一份。", 16, MainActivity.MUTED));
      for (SaveLibrary.Entry entry : backups) {
        LinearLayout card = card(entry.title());
        String version =
            "local".equals(entry.info.optString("origin"))
                ? "本机替换前备份"
                : entry.info.has("revision") ? "云端备份 v" + entry.info.optLong("revision") : "旧版备份";
        card.addView(
            text(
                entry.player() + " · " + version + " · " + bytes(entry.file.length()),
                13,
                MainActivity.ACCENT));
        card.addView(
            text(
                java.text.DateFormat.getDateTimeInstance().format(new Date(entry.created())),
                12,
                MainActivity.MUTED));
        LinearLayout controls = new LinearLayout(activity);
        card.addView(controls);
        Button export = button("导出", () -> actions.exportSave(entry));
        export.setContentDescription("导出存档 " + entry.title());
        controls.addView(export);
        Button remove =
            button(
                "移除备份",
                () ->
                    new AlertDialog.Builder(activity)
                        .setTitle("移除这份本机存档备份？")
                        .setMessage(
                            entry.title() + " · " + entry.player() + "\n模拟器正在使用的存档和云端进度不会被删除。")
                        .setNegativeButton("保留", null)
                        .setPositiveButton("仅移除这份备份", (d, w) -> work(() -> saves.delete(entry)))
                        .show());
        controls.addView(remove);
        remove.setContentDescription("移除存档备份 " + entry.title() + " " + entry.player());
      }
    }
  }

  private interface Work {
    void run() throws Exception;
  }

  private void work(Work work) {
    io.execute(
        () -> {
          try {
            work.run();
            activity.runOnUiThread(() -> refresh(true));
          } catch (Exception e) {
            activity.runOnUiThread(
                () ->
                    new AlertDialog.Builder(activity)
                        .setMessage(ApiClient.message(e))
                        .setPositiveButton("知道了", null)
                        .show());
          }
        });
  }

  private LinearLayout card(String title) {
    LinearLayout card = new LinearLayout(activity);
    card.setOrientation(VERTICAL);
    card.setPadding(dp(16), dp(12), dp(16), dp(12));
    card.setBackground(surface(MainActivity.PANEL));
    LayoutParams p = new LayoutParams(-1, -2);
    p.setMargins(0, dp(7), 0, dp(7));
    list.addView(card, p);
    card.addView(text(title, 18, MainActivity.TEXT));
    return card;
  }

  private TextView text(String value, int size, int color) {
    TextView v = new TextView(activity);
    v.setText(value);
    v.setTextSize(size);
    v.setTextColor(color);
    v.setPadding(0, dp(4), 0, dp(4));
    return v;
  }

  private Button button(String label, Runnable action) {
    Button b = new Button(activity);
    b.setText(label);
    b.setTextSize(14);
    b.setAllCaps(false);
    b.setTextColor(MainActivity.ACCENT);
    b.setMinHeight(dp(44));
    b.setPadding(dp(12), dp(4), dp(12), dp(4));
    android.graphics.drawable.StateListDrawable states =
        new android.graphics.drawable.StateListDrawable();
    GradientDrawable focus = surface(0xff29445c);
    focus.setStroke(dp(2), MainActivity.ACCENT);
    states.addState(new int[] {android.R.attr.state_focused}, focus);
    states.addState(new int[] {android.R.attr.state_pressed}, surface(0xff29445c));
    GradientDrawable normal = surface(0xff20354a);
    normal.setStroke(dp(1), 0xff354d63);
    states.addState(new int[] {}, normal);
    b.setBackground(states);
    LayoutParams params = new LayoutParams(-2, dp(44));
    params.setMargins(0, dp(6), dp(8), 0);
    b.setLayoutParams(params);
    b.setOnClickListener(v -> action.run());
    return b;
  }

  private GradientDrawable surface(int color) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(12));
    return d;
  }

  private int dp(int value) {
    return (int) (getResources().getDisplayMetrics().density * value + .5f);
  }

  private static String bytes(long n) {
    return n >= 1073741824
        ? String.format(Locale.ROOT, "%.1f GiB", n / 1073741824d)
        : n >= 1048576
            ? String.format(Locale.ROOT, "%.1f MiB", n / 1048576d)
            : String.format(Locale.ROOT, "%.1f KiB", n / 1024d);
  }
}
