package io.retroone.android;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.LruCache;
import android.view.Gravity;
import android.view.View;
import android.widget.*;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import org.json.JSONArray;
import org.json.JSONObject;

/** Native, keyboard-focusable UI. Remote catalog and on-device downloads have separate states. */
public final class MainActivity extends Activity {
  static final int BG = 0xff0d192b,
      PANEL = 0xff192b40,
      TEXT = 0xffedf4fa,
      MUTED = 0xff9eb1c6,
      ACCENT = 0xffa3dfff;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final ExecutorService io = Executors.newFixedThreadPool(2),
      preferencesIo = Executors.newSingleThreadExecutor();
  private final ThreadPoolExecutor images = (ThreadPoolExecutor) Executors.newFixedThreadPool(3);
  private final LruCache<String, Bitmap> artwork = new LruCache<>(32);
  private final List<JSONObject> games = new ArrayList<>();
  private final List<JSONObject> platforms = new ArrayList<>();
  private final List<String> pinnedPlatforms = new ArrayList<>();
  private LinearLayout root, body, grid, platformStrip, headerScopes;
  private TextView status;
  private ScrollView scroll;
  private ServerConfig config;
  private DownloadStore store;
  private String tab = "library", query = "", platformId = "", downloadSignature = "", exportId;
  private String scope = "all", sort = "popularity", direction = "desc", profileName = "选择玩家";
  private String saveExportPath, profileRequestedBase;
  private Button profileButton, sortButton;
  private int requestVersion, total, pageOffset;
  private boolean destroyed, settingsBusy;
  private String settingsBase;
  private int settingsVersion;
  private final Runnable poll =
      new Runnable() {
        public void run() {
          if (!destroyed) {
            if ("downloads".equals(tab)) renderDownloads(false);
            handler.postDelayed(this, 800);
          }
        }
      };

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    store = RetroApplication.from(this).downloads;
    try {
      config = RetroApplication.from(this).credentials.current();
    } catch (Exception ignored) {
    }
    readLibraryPreferences();
    if (state != null) {
      tab = state.getString("tab", "library");
      query = state.getString("query", "");
      platformId = state.getString("platform", "");
      exportId = state.getString("exportId");
      saveExportPath = state.getString("saveExportPath");
    }
    if (getIntent().getBooleanExtra("downloads", false)) tab = "downloads";
    root = column();
    root.setBackgroundColor(BG);
    root.setPadding(dp(22), dp(12), dp(22), 0);
    root.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          root.setPadding(
              dp(22) + insets.getSystemWindowInsetLeft(),
              dp(8) + insets.getSystemWindowInsetTop(),
              dp(22) + insets.getSystemWindowInsetRight(),
              insets.getSystemWindowInsetBottom());
          return insets;
        });
    setContentView(root);
    LinearLayout header = row();
    ImageView logo = new ImageView(this);
    logo.setImageResource(R.drawable.ic_retroone);
    header.addView(logo, new LinearLayout.LayoutParams(dp(32), dp(32)));
    TextView brand = text("retroone", 22, TEXT);
    brand.setPadding(0, dp(6), dp(16), dp(6));
    brand.setTypeface(null, Typeface.BOLD);
    header.addView(brand, new LinearLayout.LayoutParams(-2, dp(44)));
    if (getResources().getConfiguration().screenWidthDp >= 700) {
      headerScopes = row();
      header.addView(headerScopes);
    }
    header.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
    Button download = chip("↓", () -> show("downloads"));
    download.setContentDescription("下载管理");
    download.setTooltipText("下载管理 · 离开页面仍继续下载");
    header.addView(download, new LinearLayout.LayoutParams(dp(44), dp(44)));
    Button more = chip("⋯", this::navigationMenu);
    more.setContentDescription("更多功能");
    header.addView(more, new LinearLayout.LayoutParams(dp(44), dp(44)));
    profileButton =
        chip(
            profileName + " ▾",
            () -> {
              if (config != null) chooseProfile();
              else show("settings");
            });
    profileButton.setContentDescription("切换玩家");
    profileButton.setMaxWidth(
        dp(getResources().getConfiguration().screenWidthDp < 600 ? 110 : 160));
    profileButton.setSingleLine(true);
    profileButton.setEllipsize(android.text.TextUtils.TruncateAt.END);
    profileButton.setBackground(controlBackground(true));
    header.addView(profileButton);
    root.addView(header, new LinearLayout.LayoutParams(-1, dp(48)));
    body = column();
    root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1));
    show(tab);
    handler.post(poll);
  }

  @Override
  protected void onNewIntent(Intent intent) {
    super.onNewIntent(intent);
    setIntent(intent);
    if (intent.getBooleanExtra("downloads", false)) show("downloads");
  }

  @Override
  protected void onSaveInstanceState(Bundle state) {
    state.putString("tab", tab);
    state.putString("query", query);
    state.putString("platform", platformId);
    state.putString("exportId", exportId);
    state.putString("saveExportPath", saveExportPath);
    super.onSaveInstanceState(state);
  }

  @Override
  protected void onDestroy() {
    destroyed = true;
    handler.removeCallbacksAndMessages(null);
    io.shutdownNow();
    images.shutdownNow();
    preferencesIo.shutdownNow();
    super.onDestroy();
  }

  private void ui(Runnable action) {
    handler.post(
        () -> {
          if (!destroyed) action.run();
        });
  }

  private void show(String page) {
    requestVersion++;
    tab = page;
    renderHeaderScopes();
    body.removeAllViews();
    images.getQueue().clear();

    if ("apps".equals(page)) {
      apps();
      return;
    }
    if ("settings".equals(page)) {
      settings();
      return;
    }
    if ("downloads".equals(page)) {
      scroll = new ScrollView(this);
      grid = column();
      scroll.addView(grid);
      body.addView(scroll);
      downloadSignature = "";
      renderDownloads(true);
      return;
    }
    library();
  }

  private void renderHeaderScopes() {
    if (headerScopes == null) return;
    headerScopes.removeAllViews();
    String[] values = {"all", "favorites", "recent"};
    String[] labels = {"游戏库", "★ 收藏", "最近玩过"};
    for (int i = 0; i < values.length; i++) {
      String value = values[i];
      Button item =
          chip(
              labels[i],
              () -> {
                scope = value;
                writeLibraryPreferences();
                show("library");
              });
      item.setBackground(controlBackground("library".equals(tab) && scope.equals(value)));
      headerScopes.addView(item);
    }
  }

  private void navigationMenu() {
    new AlertDialog.Builder(this)
        .setTitle("RetroOne")
        .setItems(
            new String[] {"游戏库", "应用", "下载管理", "刷新游戏库", "连接与模拟器设置"},
            (d, which) -> {
              if (which == 3) settingsBase = null;
              show(new String[] {"library", "apps", "downloads", "library", "settings"}[which]);
            })
        .setNegativeButton("返回", null)
        .show();
  }

  private void library() {
    platformStrip = row();
    LinearLayout navigation = row();
    HorizontalScrollView platformScroll = new HorizontalScrollView(this);
    platformScroll.setHorizontalScrollBarEnabled(false);
    platformScroll.addView(platformStrip);
    navigation.addView(platformScroll, new LinearLayout.LayoutParams(0, dp(52), 1));
    Button allPlatforms = chip("平台 +", this::choosePlatforms);
    allPlatforms.setContentDescription("更多平台");
    allPlatforms.setTooltipText("选择、搜索和关注平台；与当前玩家的网页设置同步");
    navigation.addView(allPlatforms);
    body.addView(navigation, new LinearLayout.LayoutParams(-1, dp(52)));
    renderPlatformStrip();
    LinearLayout choices = row();
    if (headerScopes == null) {
      String label = scope.equals("favorites") ? "★ 收藏" : scope.equals("recent") ? "最近玩过" : "全部游戏";
      choices.addView(
          chip(
              label + " ▾",
              () ->
                  new AlertDialog.Builder(this)
                      .setTitle("查看游戏")
                      .setItems(
                          new String[] {"全部游戏", "我的收藏", "最近玩过"},
                          (d, w) -> {
                            scope = new String[] {"all", "favorites", "recent"}[w];
                            writeLibraryPreferences();
                            show("library");
                          })
                      .show()));
      choices.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
    }
    sortButton = chip(sortLabel(), this::chooseSort);
    sortButton.setContentDescription("游戏排序");
    sortButton.setTooltipText("按整个游戏库排序");
    choices.addView(sortButton);
    Button searchButton = chip("⌕", this::searchDialog);
    searchButton.setContentDescription("搜索游戏");
    searchButton.setTooltipText(query.isEmpty() ? "搜索游戏" : query);
    choices.addView(searchButton, new LinearLayout.LayoutParams(dp(44), dp(44)));
    if (headerScopes == null) body.addView(choices, new LinearLayout.LayoutParams(-1, dp(48)));
    else navigation.addView(choices);
    status = text("", 12, MUTED);
    status.setSingleLine(true);
    status.setEllipsize(android.text.TextUtils.TruncateAt.END);
    status.setPadding(dp(4), 0, 0, 0);
    status.setGravity(Gravity.CENTER_VERTICAL);
    status.setTooltipText("点击刷新游戏库与平台偏好");
    status.setOnClickListener(
        v -> {
          settingsBase = null;
          loadLibrarySettings();
          loadGames(false);
        });
    body.addView(status, new LinearLayout.LayoutParams(-1, dp(24)));
    scroll = new ScrollView(this);
    grid = column();
    scroll.addView(grid);
    body.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    if (config == null) {
      status.setText("连接服务器，打开你的游戏库");
      grid.addView(button("配置服务地址", () -> show("settings")));
      return;
    }
    refreshProfile();
    loadPlatforms();
    loadLibrarySettings();
    loadGames(false);
  }

  private String sortLabel() {
    return LibraryQuery.LABELS[java.util.Arrays.asList(LibraryQuery.SORTS).indexOf(sort)]
        + ("asc".equals(direction) ? " ↑" : " ↓");
  }

  private void searchDialog() {
    EditText input = edit("搜索游戏名称", false);
    input.setText(query);
    input.setPadding(dp(24), dp(12), dp(24), dp(12));
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("搜索游戏")
            .setView(input)
            .setPositiveButton(
                "搜索",
                (d, w) -> {
                  query = input.getText().toString().trim();
                  loadGames(false);
                })
            .setNeutralButton(
                "清除搜索",
                (d, w) -> {
                  query = "";
                  loadGames(false);
                })
            .setNegativeButton("取消", null)
            .create();
    input.setImeOptions(android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH);
    input.setOnEditorActionListener(
        (v, action, event) -> {
          query = input.getText().toString().trim();
          dialog.dismiss();
          loadGames(false);
          return true;
        });
    dialog.show();
    input.requestFocus();
  }

  private void loadPlatforms() {
    ServerConfig current = config;
    LinearLayout target = platformStrip;
    io.execute(
        () -> {
          try {
            JSONArray rows =
                new ApiClient(current).json(current.api("platforms")).getJSONArray("platforms");
            List<JSONObject> values = new ArrayList<>();
            for (int i = 0; i < rows.length(); i++) values.add(rows.getJSONObject(i));
            ui(
                () -> {
                  if (target != platformStrip
                      || config == null
                      || !current.base.equals(config.base)) return;
                  platforms.clear();
                  platforms.addAll(values);
                  renderPlatformStrip();
                });
          } catch (Exception ignored) {
            /* The game request shows connection errors. */
          }
        });
  }

  private void renderPlatformStrip() {
    if (platformStrip == null || !"library".equals(tab)) return;
    platformStrip.removeAllViews();
    addPlatformChip("", "全部平台", false);
    for (JSONObject p : LibrarySettings.visible(platforms, pinnedPlatforms, platformId)) {
      String id = p.optString("id");
      addPlatformChip(
          id, PlatformArt.shortName(id, p.optString("name")), pinnedPlatforms.contains(id));
    }
  }

  private void addPlatformChip(String id, String name, boolean pinned) {
    Button item =
        chip(
            (pinned ? "★ " : "") + name,
            () -> {
              platformId = id;
              renderPlatformStrip();
              loadGames(false);
            });
    android.graphics.drawable.Drawable icon = getDrawable(PlatformArt.resource(id));
    icon.setBounds(0, 0, dp(26), dp(26));
    item.setCompoundDrawables(icon, null, null, null);
    item.setCompoundDrawablePadding(dp(7));
    item.setBackground(controlBackground(platformId.equals(id)));
    item.setTooltipText(name + (pinned ? " · 已关注" : ""));
    item.setContentDescription(name + (platformId.equals(id) ? "，已选中" : ""));
    platformStrip.addView(item);
  }

  private void loadLibrarySettings() {
    ServerConfig source = config;
    if (source == null || source.base.equals(settingsBase) || settingsBusy) return;
    settingsBase = source.base;
    int generation = ++settingsVersion;
    preferencesIo.execute(
        () -> {
          try {
            LibrarySettings settings = LibrarySettings.read(source);
            ui(
                () -> {
                  if (generation != settingsVersion
                      || config == null
                      || !source.base.equals(config.base)) return;
                  applyLibrarySettings(settings);
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (generation != settingsVersion
                      || config == null
                      || !source.base.equals(config.base)) return;
                  settingsBase = null;
                  if ("library".equals(tab)) {
                    status.setText("平台偏好未同步 · 点击重试");
                    status.setTooltipText(ApiClient.message(e));
                  }
                });
          }
        });
  }

  private void applyLibrarySettings(LibrarySettings settings) {
    boolean reorder = !sort.equals(settings.sort) || !direction.equals(settings.direction);
    pinnedPlatforms.clear();
    pinnedPlatforms.addAll(settings.pins);
    sort = settings.sort;
    direction = settings.direction;
    writeLibraryPreferences();
    if ("library".equals(tab)) {
      sortButton.setText(sortLabel());
      renderPlatformStrip();
      if (reorder) loadGames(false);
    }
  }

  private void saveLibrarySettings(
      String pin, Boolean follow, String newSort, String newDirection, Runnable done) {
    if (config == null || settingsBusy) return;
    ServerConfig source = config;
    int generation = ++settingsVersion;
    settingsBusy = true;
    preferencesIo.execute(
        () -> {
          try {
            LibrarySettings saved =
                LibrarySettings.update(source, pin, follow, newSort, newDirection);
            ui(
                () -> {
                  if (generation != settingsVersion
                      || config == null
                      || !source.base.equals(config.base)) return;
                  settingsBusy = false;
                  settingsBase = source.base;
                  applyLibrarySettings(saved);
                  if (done != null) done.run();
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (generation != settingsVersion
                      || config == null
                      || !source.base.equals(config.base)) return;
                  settingsBusy = false;
                  settingsBase = null;
                  alert("设置未保存：" + ApiClient.message(e));
                  if (done != null) done.run();
                });
          }
        });
  }

  private void choosePlatforms() {
    ServerConfig source = config;
    LinearLayout content = column();
    content.setPadding(dp(20), 0, dp(20), dp(12));
    EditText find = edit("查找平台，例如 PSP、街机", false);
    content.addView(find);
    ScrollView listScroll = new ScrollView(this);
    LinearLayout list = column();
    listScroll.addView(list);
    content.addView(listScroll, new LinearLayout.LayoutParams(-1, dp(320)));
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("选择平台")
            .setView(content)
            .setNegativeButton("完成", null)
            .create();
    Runnable render =
        () -> {
          if (config == null || source == null || !source.base.equals(config.base)) {
            dialog.dismiss();
            return;
          }
          list.removeAllViews();
          List<JSONObject> ordered = new ArrayList<>(platforms);
          ordered.sort(
              (a, b) -> {
                int ai = pinnedPlatforms.indexOf(a.optString("id")),
                    bi = pinnedPlatforms.indexOf(b.optString("id"));
                return Integer.compare(ai < 0 ? 1000 : ai, bi < 0 ? 1000 : bi);
              });
          String q = find.getText().toString().trim().toLowerCase(Locale.ROOT);
          for (JSONObject p : ordered) {
            String id = p.optString("id"), name = p.optString("name");
            if (!(name + " " + id).toLowerCase(Locale.ROOT).contains(q)) continue;
            LinearLayout line = row();
            ImageView icon = new ImageView(this);
            icon.setImageResource(PlatformArt.resource(id));
            line.addView(icon, new LinearLayout.LayoutParams(dp(32), dp(32)));
            Button select =
                chip(
                    name + "  ·  " + p.optInt("count"),
                    () -> {
                      platformId = id;
                      dialog.dismiss();
                      renderPlatformStrip();
                      loadGames(false);
                    });
            select.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
            line.addView(select, new LinearLayout.LayoutParams(0, dp(48), 1));
            boolean pinned = pinnedPlatforms.contains(id);
            Button follow = chip(pinned ? "★ 已关注" : "☆ 关注", () -> {});
            follow.setContentDescription((pinned ? "取消关注 " : "关注 ") + name);
            follow.setOnClickListener(
                v -> {
                  if (settingsBusy) return;
                  follow.setEnabled(false);
                  saveLibrarySettings(
                      id,
                      !pinnedPlatforms.contains(id),
                      null,
                      null,
                      () -> {
                        if (!dialog.isShowing()) return;
                        follow.setEnabled(true);
                        // Keep focus and scroll position when toggling a platform.
                        boolean now = pinnedPlatforms.contains(id);
                        follow.setText(now ? "★ 已关注" : "☆ 关注");
                        follow.setContentDescription((now ? "取消关注 " : "关注 ") + name);
                      });
                });
            line.addView(follow);
            list.addView(line);
          }
          if (list.getChildCount() == 0) list.addView(text("没有匹配的平台", 14, MUTED));
        };
    find.addTextChangedListener(
        new TextWatcher() {
          public void beforeTextChanged(CharSequence s, int a, int c, int f) {}

          public void onTextChanged(CharSequence s, int a, int b, int c) {
            render.run();
          }

          public void afterTextChanged(Editable s) {}
        });
    dialog.show();
    render.run();
  }

  private void loadGames(boolean more) {
    loadPage(more ? pageOffset + 60 : 0);
  }

  private void loadPage(int offset) {
    if (config == null || !"library".equals(tab)) return;
    int version = ++requestVersion;
    ServerConfig current = config;
    String route = LibraryQuery.route(query, platformId, scope, sort, direction, offset);
    writeLibraryPreferences();
    status.setText("正在读取游戏库…");
    io.execute(
        () -> {
          try {
            JSONObject result = new ApiClient(current).json(current.api(route));
            JSONArray values = result.getJSONArray("games");
            List<JSONObject> items = new ArrayList<>();
            for (int i = 0; i < values.length(); i++) items.add(values.getJSONObject(i));
            ui(
                () -> {
                  if (version != requestVersion || !"library".equals(tab)) return;
                  games.clear();
                  games.addAll(items);
                  pageOffset = offset;
                  total = result.optInt("total", games.size());
                  if (offset > 0 && offset >= total) {
                    loadPage(Math.max(0, ((total - 1) / 60) * 60));
                    return;
                  }
                  status.setText(
                      "共 "
                          + total
                          + " 款 · 第 "
                          + (offset / 60 + 1)
                          + " 页"
                          + (query.isEmpty() ? "" : " · 搜索：" + query));
                  renderGames(false);
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (version != requestVersion || !"library".equals(tab)) return;
                  status.setText(ApiClient.message(e));
                  games.clear();
                  grid.removeAllViews();
                  grid.addView(button("重试", () -> loadPage(offset)));
                });
          }
        });
  }

  private void renderGames(boolean more) {
    images.getQueue().clear();
    int y = more ? scroll.getScrollY() : 0;
    grid.removeAllViews();
    if (games.isEmpty()) {
      grid.addView(
          text(query.isEmpty() ? "此平台还没有游戏。请在服务端导入自己的收藏。" : "没有匹配的游戏，试试其他名称或平台。", 17, MUTED));
      return;
    }
    int width = getResources().getConfiguration().screenWidthDp - 44;
    int columns = Math.max(1, Math.min(6, width / 158));
    List<DownloadStore.Item> localItems = store.all();
    LinearLayout line = null;
    for (int i = 0; i < games.size(); i++) {
      JSONObject game = games.get(i);
      if (i % columns == 0) {
        line = row();
        line.setGravity(Gravity.TOP);
        grid.addView(line);
      }
      LinearLayout card = column();
      card.setPadding(dp(6), dp(6), dp(6), dp(8));
      card.setBackground(controlBackground(false));
      LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(0, -2, 1);
      cp.setMargins(dp(4), dp(6), dp(4), dp(6));
      line.addView(card, cp);
      FrameLayout cover = new FrameLayout(this);
      cover.setBackground(shape(0xff263b50, 8));
      card.addView(
          cover,
          new LinearLayout.LayoutParams(-1, dp(Math.min(230, (width / columns - 20) * 4 / 3))));
      TextView monogram =
          text(
              game.optString("platform_name", game.optString("platform")).toUpperCase(Locale.ROOT),
              22,
              ACCENT);
      monogram.setGravity(Gravity.CENTER);
      cover.addView(monogram, new FrameLayout.LayoutParams(-1, -1));
      String coverUrl = game.optString("cover_url", "");
      if (!coverUrl.isEmpty() && !"null".equals(coverUrl)) {
        ImageView image = new ImageView(this);
        image.setScaleType(ImageView.ScaleType.FIT_CENTER);
        cover.addView(image, new FrameLayout.LayoutParams(-1, -1));
        image.setTag(monogram);
        loadCover(image, coverUrl, config);
      }
      TextView name =
          text((game.optBoolean("favorite") ? "★ " : "") + game.optString("title"), 16, TEXT);
      name.setMaxLines(2);
      name.setMinHeight(dp(48));
      name.setTypeface(null, Typeface.BOLD);
      card.addView(name);
      card.addView(
          text(
              game.optString("platform_name", game.optString("platform"))
                  + "  ·  "
                  + bytes(game.optLong("size")),
              12,
              MUTED));
      String localLabel = "";
      for (DownloadStore.Item item : localItems) {
        if (item.base.equals(config.base)
            && item.game.equals(game.optString("id"))
            && !DownloadStore.CANCELED.equals(item.state)) {
          localLabel =
              switch (item.state) {
                case DownloadStore.READY -> "已下载";
                case DownloadStore.RUNNING, DownloadStore.PREPARING, DownloadStore.QUEUED -> "下载中";
                case DownloadStore.PAUSED -> "下载已暂停";
                default -> "下载需重试";
              };
          break;
        }
      }
      if (!localLabel.isEmpty()) card.addView(text(localLabel, 12, ACCENT));
      card.setFocusable(true);
      card.setClickable(true);
      card.setContentDescription(game.optString("title") + "，查看详情");
      card.setForeground(getDrawable(android.R.drawable.list_selector_background));
      card.setOnClickListener(v -> detail(game));
    }
    if (line != null)
      for (int i = games.size() % columns; i > 0 && i < columns; i++)
        line.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
    LinearLayout pages = row();
    grid.addView(pages);
    if (pageOffset > 0) pages.addView(button("上一页", () -> loadPage(Math.max(0, pageOffset - 60))));
    if (pageOffset + games.size() < total) pages.addView(button("下一页", () -> loadGames(true)));
    scroll.post(() -> scroll.scrollTo(0, y));
  }

  private void loadCover(ImageView view, String value, ServerConfig source) {
    loadCover(view, value, source, false);
  }

  private void loadCover(ImageView view, String value, ServerConfig source, boolean priority) {
    if (value.isEmpty() || "null".equals(value)) return;
    final String url;
    try {
      url = source.resolve(value).toString();
    } catch (Exception e) {
      return;
    }
    Bitmap cached = artwork.get(url);
    if (cached != null) {
      view.setImageBitmap(cached);
      if (view.getTag() instanceof View placeholder) placeholder.setVisibility(View.GONE);
      return;
    }
    (priority ? io : images)
        .execute(
            () -> {
              if (!view.isAttachedToWindow()) return;
              HttpURLConnection c = null;
              try {
                c = new ApiClient(source).open(java.net.URI.create(url), null);
                if (c.getResponseCode() != 200) return;
                byte[] data;
                try (java.io.InputStream input = c.getInputStream()) {
                  data = ApiClient.read(input, 3 * 1024 * 1024);
                }
                BitmapFactory.Options options = new BitmapFactory.Options();
                options.inJustDecodeBounds = true;
                BitmapFactory.decodeByteArray(data, 0, data.length, options);
                if (options.outWidth <= 0 || options.outHeight <= 0) return;
                options.inSampleSize =
                    Math.max(1, Math.max(options.outWidth, options.outHeight) / 512);
                options.inJustDecodeBounds = false;
                Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length, options);
                if (bitmap != null) {
                  artwork.put(url, bitmap);
                  ui(
                      () -> {
                        view.setImageBitmap(bitmap);
                        if (view.getTag() instanceof View placeholder)
                          placeholder.setVisibility(View.GONE);
                      });
                }
              } catch (Exception ignored) {
              } finally {
                if (c != null) c.disconnect();
              }
            });
  }

  private void detail(JSONObject basic) {
    ServerConfig source = config;
    LinearLayout content = column();
    content.setPadding(dp(22), dp(10), dp(22), dp(18));
    TextView info = text("正在读取游戏详情与下载资格…", 15, MUTED);
    content.addView(info);
    ScrollView detailScroll = new ScrollView(this);
    detailScroll.addView(content);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle(basic.optString("title"))
            .setView(detailScroll)
            .setNegativeButton("返回", null)
            .create();
    dialog.show();
    io.execute(
        () -> {
          try {
            ApiClient api = new ApiClient(source);
            String path = "games/" + ServerConfig.encode(basic.getString("id"));
            JSONObject game = api.json(source.api(path));
            DownloadCapability capability =
                new DownloadCapability(api.json(source.api(path + "/download-options")));
            ui(
                () -> {
                  if (!dialog.isShowing()) return;
                  String desc = game.optString("description", "");
                  if (desc.length() > 1200) desc = desc.substring(0, 1200) + "…";
                  String reason = Emulators.support(game.optString("platform"), capability.name);
                  info.setText(
                      game.optString("platform_name", game.optString("platform"))
                          + "  ·  "
                          + bytes(capability.size > 0 ? capability.size : game.optLong("size")));
                  LinearLayout hero = row();
                  content.addView(hero);
                  ImageView cover = new ImageView(this);
                  cover.setScaleType(ImageView.ScaleType.FIT_CENTER);
                  cover.setImageResource(PlatformArt.resource(game.optString("platform")));
                  cover.setBackground(shape(PANEL, 8));
                  hero.addView(cover, new LinearLayout.LayoutParams(dp(128), dp(175)));
                  loadCover(cover, game.optString("cover_url", ""), source, true);
                  LinearLayout actions = column();
                  actions.setPadding(dp(16), 0, 0, 0);
                  hero.addView(actions, new LinearLayout.LayoutParams(0, -2, 1));
                  Button favorite =
                      button(game.optBoolean("favorite") ? "★ 已收藏" : "☆ 收藏", () -> {});
                  favorite.setOnClickListener(
                      v -> {
                        favorite.setEnabled(false);
                        io.execute(
                            () -> {
                              try {
                                JSONObject result =
                                    new ApiClient(source)
                                        .post(
                                            source.api(path + "/favorite"),
                                            new JSONObject()
                                                .put("favorite", !game.optBoolean("favorite")),
                                            16384);
                                game.put("favorite", result.getBoolean("favorite"));
                                ui(
                                    () -> {
                                      favorite.setText(
                                          game.optBoolean("favorite") ? "★ 已收藏" : "☆ 收藏");
                                      favorite.setEnabled(true);
                                      if (config != null && config.base.equals(source.base))
                                        loadPage(pageOffset);
                                    });
                              } catch (Exception e) {
                                ui(
                                    () -> {
                                      favorite.setEnabled(true);
                                      alert(ApiClient.message(e));
                                    });
                              }
                            });
                      });
                  LinearLayout personal = row();
                  personal.addView(favorite);
                  personal.addView(button("云存档", () -> cloudSave(source, game)));
                  actions.addView(personal);
                  DownloadStore.Item local = null;
                  for (DownloadStore.Item item : store.all())
                    if (item.base.equals(source.base)
                        && item.game.equals(game.optString("id"))
                        && !DownloadStore.CANCELED.equals(item.state)) {
                      local = item;
                      break;
                    }
                  if (local != null) {
                    DownloadStore.Item item = local;
                    boolean direct =
                        DownloadStore.READY.equals(item.state)
                            && Emulators.support(item.platform, item.name).isEmpty()
                            && !Emulators.installed(this).isEmpty();
                    actions.addView(
                        button(
                            direct
                                ? "启动 PPSSPP"
                                : DownloadStore.READY.equals(item.state) ? "已下载 · 查看文件" : "查看下载任务",
                            () -> {
                              dialog.dismiss();
                              if (direct) launch(item);
                              else show("downloads");
                            }));
                  } else if (capability.available)
                    actions.addView(
                        button(
                            "↓ 下载到掌机",
                            () -> {
                              try {
                                store.enqueue(source, game);
                                requestNotifications();
                                DownloadService.command(this, "start", null);
                                dialog.dismiss();
                                show("downloads");
                              } catch (Exception e) {
                                alert(ApiClient.message(e));
                              }
                            }));
                  if (!capability.available) {
                    actions.addView(text("此资源暂不支持下载", 15, 0xffffcf9d));
                    actions.addView(text(capability.explanation(), 13, MUTED));
                  } else if (!reason.isEmpty()) {
                    actions.addView(text("可下载 · 需手动导入模拟器", 13, MUTED));
                  } else {
                    actions.addView(
                        text(
                            Emulators.installed(this).isEmpty()
                                ? "下载后安装 PPSSPP 即可启动"
                                : "PSP · 使用已安装的 PPSSPP",
                            13,
                            ACCENT));
                  }
                  Button explain =
                      chip(
                          "下载与启动说明 ⓘ",
                          () ->
                              alert(
                                  (capability.available
                                          ? (reason.isEmpty()
                                              ? "支持下载和 PPSSPP 启动，具体游戏兼容性由模拟器决定。"
                                              : reason)
                                          : capability.explanation())
                                      + "\n\n"
                                      + "文件下载和模拟器兼容性分别判断。已下载的文件可以从下载管理导出；多文件游戏需要完整资源，不能只下载入口文件。"));
                  explain.setTooltipText("能下载文件，不等于此设备已接入对应模拟器");
                  content.addView(explain);
                  if (!desc.isEmpty()) {
                    TextView description = text(desc, 14, MUTED);
                    description.setVisibility(View.GONE);
                    Button expand =
                        chip(
                            "关于这款游戏 ▾",
                            () ->
                                description.setVisibility(
                                    description.getVisibility() == View.GONE
                                        ? View.VISIBLE
                                        : View.GONE));
                    content.addView(expand);
                    content.addView(description);
                  }
                });
          } catch (Exception e) {
            ui(
                () -> {
                  if (dialog.isShowing()) info.setText(ApiClient.message(e));
                });
          }
        });
  }

  private void requestNotifications() {
    if (Build.VERSION.SDK_INT >= 33
        && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
      requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 10);
  }

  private void renderDownloads(boolean force) {
    List<DownloadStore.Item> items = store.all();
    StringBuilder signature = new StringBuilder();
    for (DownloadStore.Item i : items)
      signature.append(i.id).append(i.state).append(i.bytes).append(i.message);
    if (!force && signature.toString().equals(downloadSignature)) return;
    downloadSignature = signature.toString();
    int y = scroll.getScrollY();
    Object focus = getCurrentFocus() == null ? null : getCurrentFocus().getTag();
    grid.removeAllViews();
    grid.addView(text("本机与下载", 28, TEXT));
    grid.addView(text("完整文件通过校验后才能启动。离开本页，下载仍继续；进程中断后可手动续传。", 14, MUTED));
    if (items.stream().noneMatch(i -> !DownloadStore.CANCELED.equals(i.state)))
      grid.addView(text("这台设备还没有下载。去游戏库选择自己的收藏。", 17, MUTED));
    for (DownloadStore.Item item : items) {
      if (DownloadStore.CANCELED.equals(item.state)) continue;
      LinearLayout card = column();
      card.setPadding(dp(16), dp(12), dp(16), dp(12));
      card.setBackground(shape(PANEL, 10));
      LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(-1, -2);
      p.setMargins(0, dp(8), 0, dp(4));
      grid.addView(card, p);
      card.addView(text(item.title, 19, TEXT));
      card.addView(text(item.platform.toUpperCase(Locale.ROOT) + " · " + item.base, 12, MUTED));
      card.addView(
          text(
              stateLabel(item.state) + "  " + bytes(item.bytes) + " / " + bytes(item.size),
              15,
              ACCENT));
      ProgressBar progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
      progress.setMax(1000);
      progress.setProgress(item.size > 0 ? (int) (item.bytes * 1000 / item.size) : 0);
      progress.setIndeterminate(DownloadStore.PREPARING.equals(item.state));
      card.addView(progress, new LinearLayout.LayoutParams(-1, dp(8)));
      if (!item.message.isEmpty()) card.addView(text(item.message, 13, MUTED));
      LinearLayout actions = row();
      card.addView(actions);
      boolean active =
          DownloadStore.RUNNING.equals(item.state)
              || DownloadStore.PREPARING.equals(item.state)
              || DownloadStore.QUEUED.equals(item.state);
      if (active)
        actions.addView(
            tagged(
                button("暂停", () -> DownloadService.command(this, DownloadService.PAUSE, item.id)),
                item.id + "pause"));
      else if (DownloadStore.PAUSED.equals(item.state) || DownloadStore.FAILED.equals(item.state))
        actions.addView(
            tagged(
                button(
                    "继续 / 重试",
                    () -> {
                      requestNotifications();
                      DownloadService.command(this, DownloadService.RESUME, item.id);
                    }),
                item.id + "resume"));
      else if (DownloadStore.READY.equals(item.state)) {
        String reason = Emulators.support(item.platform, item.name);
        if (reason.isEmpty())
          actions.addView(tagged(button("启动 PPSSPP", () -> launch(item)), item.id + "launch"));
        else card.addView(text(reason, 13, 0xffffbb88));
        actions.addView(tagged(button("导出副本…", () -> export(item)), item.id + "export"));
      }
      if (!active)
        actions.addView(
            tagged(
                button(
                    "移除本机副本",
                    () ->
                        new AlertDialog.Builder(this)
                            .setTitle("移除「" + item.title + "」的本机下载？")
                            .setMessage(
                                "只移除 RetroOne 保存的这个文件与未完成分片。请先退出正在使用它的模拟器。服务器原文件和模拟器存档不受影响。")
                            .setNegativeButton("保留", null)
                            .setPositiveButton(
                                "移除",
                                (d, w) -> {
                                  try {
                                    store.cancel(item.id);
                                    renderDownloads(true);
                                  } catch (Exception e) {
                                    alert(ApiClient.message(e));
                                  }
                                })
                            .show()),
                item.id + "delete"));
    }
    if (focus != null) {
      View target = grid.findViewWithTag(focus);
      if (target != null) target.requestFocus();
    }
    scroll.post(() -> scroll.scrollTo(0, y));
  }

  private View tagged(View view, String tag) {
    view.setTag(tag);
    return view;
  }

  private void export(DownloadStore.Item item) {
    exportId = item.id;
    Intent create =
        new Intent(Intent.ACTION_CREATE_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("application/octet-stream")
            .putExtra(
                Intent.EXTRA_TITLE,
                item.title.replaceAll("[\\\\/:*?\"<>|]", "_")
                    + item.name.substring(item.name.lastIndexOf('.')));
    try {
      startActivityForResult(create, 40);
    } catch (android.content.ActivityNotFoundException e) {
      alert("设备未提供文件保存界面，无法导出；本机副本仍保留");
    }
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 41) {
      if (result == RESULT_OK && data != null && data.getData() != null && saveExportPath != null) {
        java.io.File backup = new java.io.File(saveExportPath);
        android.net.Uri uri = data.getData();
        io.execute(
            () -> {
              try {
                if (!backup
                    .getCanonicalPath()
                    .startsWith(
                        new java.io.File(getFilesDir(), "save-backups").getCanonicalPath()
                            + java.io.File.separator)) throw new java.io.IOException("备份路径无效");
                try (java.io.InputStream in = new java.io.FileInputStream(backup);
                    java.io.OutputStream out = getContentResolver().openOutputStream(uri, "w")) {
                  if (out == null) throw new java.io.IOException("无法写入目标文件");
                  byte[] block = new byte[65536];
                  int n;
                  while ((n = in.read(block)) != -1) out.write(block, 0, n);
                }
                ui(() -> alert("云存档 ZIP 已导出。此操作不覆盖模拟器现有存档。"));
              } catch (Exception e) {
                ui(() -> alert(ApiClient.message(e) + "；目标可能留有不完整副本"));
              }
            });
      }
      return;
    }
    if (request != 40
        || result != RESULT_OK
        || data == null
        || data.getData() == null
        || exportId == null) return;
    String id = exportId;
    exportId = null;
    android.net.Uri destination = data.getData();
    Toast.makeText(this, "正在导出，请保持应用前台，完成后会显示结果", Toast.LENGTH_LONG).show();
    io.execute(
        () -> {
          try {
            DownloadStore.Item item = store.get(id);
            if (item == null || !DownloadStore.READY.equals(item.state))
              throw new java.io.IOException("本机副本已移除或尚未就绪");
            try (java.io.InputStream input = new java.io.FileInputStream(store.complete(item));
                java.io.OutputStream output =
                    getContentResolver().openOutputStream(destination, "w")) {
              if (output == null) throw new java.io.IOException("目标位置不可写入");
              byte[] block = new byte[65536];
              int n;
              long copied = 0;
              while ((n = input.read(block)) != -1) {
                output.write(block, 0, n);
                copied += n;
              }
              if (copied != item.size) throw new java.io.IOException("导出未完整，请删除目标不完整副本后重试");
            }
            ui(() -> alert("副本已导出。原下载仍保留，可在模拟器中手动选择导出的文件。"));
          } catch (Exception e) {
            ui(() -> alert(ApiClient.message(e) + "。导出失败时，目标可能留有不完整副本。"));
          }
        });
  }

  private void launch(DownloadStore.Item item) {
    List<Integer> installed = Emulators.installed(this);
    if (installed.isEmpty()) {
      alert("未找到可用的 PPSSPP。请从 PPSSPP 官方渠道安装后重试；RetroOne 不附带模拟器、游戏或固件。");
      return;
    }
    if (installed.size() == 1) {
      launchWith(item, installed.get(0));
      return;
    }
    String[] labels = new String[installed.size()];
    for (int i = 0; i < labels.length; i++) labels[i] = Emulators.LABELS[installed.get(i)];
    new AlertDialog.Builder(this)
        .setTitle("选择模拟器")
        .setItems(labels, (d, w) -> launchWith(item, installed.get(w)))
        .show();
  }

  private void launchWith(DownloadStore.Item item, int index) {
    try {
      startActivity(Emulators.intent(this, store.get(item.id), index));
    } catch (Exception e) {
      alert(
          e instanceof android.content.ActivityNotFoundException
              ? "模拟器入口不可用，请检查安装版本"
              : ApiClient.message(e));
    }
  }

  private void settings() {
    ScrollView settingsScroll = new ScrollView(this);
    LinearLayout form = column();
    settingsScroll.addView(form);
    body.addView(settingsScroll);
    form.addView(text("连接你的私人游戏馆", 28, TEXT));
    form.addView(text("可填写服务根地址，或指定玩家入口 /p/player1。家庭玩家不是登录账户。", 14, MUTED));
    EditText address = edit("https://你的服务器:8781/p/player1", false);
    address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
    address.setSingleLine();
    address.setText(config == null ? "" : config.base);
    form.addView(address);
    CheckBox http = new CheckBox(this);
    http.setText("允许 HTTP 明文连接（仅用于可信网络；认证与文件也不加密）");
    http.setChecked(config != null && config.allowHttp);
    form.addView(http);
    form.addView(text("反向代理认证", 16, TEXT));
    Spinner auth = new Spinner(this);
    auth.setAdapter(
        new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new String[] {"保留此地址已有认证", "无认证", "HTTP Basic", "Bearer Token"}));
    auth.setSelection(config == null ? 1 : 0);
    form.addView(auth);
    EditText username = edit("Basic 用户名", false),
        secret = edit("Basic 密码或 Bearer Token（不会显示已保存值）", true);
    form.addView(username);
    form.addView(secret);
    form.addView(
        text("HTTPS 验证系统与用户安装的 CA，不接受忽略证书。网页登录、Cookie、OAuth 尚未接入。认证凭据仅保存在本机加密存储中。", 13, MUTED));
    form.addView(
        button(
            "保存并打开游戏库",
            () -> {
              try {
                String value = "";
                int mode = auth.getSelectedItemPosition();
                ServerConfig candidate =
                    new ServerConfig(address.getText().toString(), http.isChecked(), "");
                if (mode == 0)
                  value =
                      RetroApplication.from(this)
                          .credentials
                          .load(candidate.base, candidate.allowHttp)
                          .authorization;
                if (mode == 2) {
                  String user = username.getText().toString();
                  if (user.contains(":") || user.contains("\n"))
                    throw new IllegalArgumentException("Basic 用户名不能包含冒号或换行");
                  value =
                      "Basic "
                          + java.util.Base64.getEncoder()
                              .encodeToString(
                                  (user + ":" + secret.getText()).getBytes(StandardCharsets.UTF_8));
                }
                if (mode == 3) {
                  if (secret.getText().toString().trim().isEmpty())
                    throw new IllegalArgumentException("请填写 Bearer Token");
                  value = "Bearer " + secret.getText().toString().trim();
                }
                candidate = new ServerConfig(candidate.base, candidate.allowHttp, value);
                RetroApplication.from(this).credentials.save(candidate);
                config = candidate;
                artwork.evictAll();
                platformId = "";
                readLibraryPreferences();
                show("library");
              } catch (Exception e) {
                alert(ApiClient.message(e));
              }
            }));
    if (config != null) form.addView(button("读取并切换服务端玩家", this::chooseProfile));
    form.addView(
        button(
            "选择默认桌面…",
            () -> {
              try {
                startActivity(new Intent(android.provider.Settings.ACTION_HOME_SETTINGS));
              } catch (android.content.ActivityNotFoundException e) {
                alert("此系统未提供默认桌面设置入口");
              }
            }));
    List<Integer> installed = Emulators.installed(this);
    StringBuilder emulator = new StringBuilder("已发现模拟器：");
    for (int i : installed) emulator.append(Emulators.LABELS[i]).append("  ");
    if (installed.isEmpty()) emulator.append("无可用 PPSSPP");
    form.addView(text(emulator.toString(), 15, ACCENT));
    form.addView(
        text(
            "当前接入 PSP 独立 ISO / CSO 的 PPSSPP 启动接口。PBP 存在实测路径兼容问题；CHD"
                + " 尚未验证可靠直启。其他平台仅提供下载管理。多文件光盘、目录游戏和压缩包不会被自动改造。\n\n"
                + "本机文件保存在 RetroOne 私有目录，卸载应用会移除这些下载。模拟器独立管理存档；本版没有云存档同步，也不会为不同 RetroOne"
                + " 玩家隔离同一模拟器的存档。\n\n"
                + "RetroOne Android 0.2.1 · 自有内容客户端",
            14,
            MUTED));
  }

  private void chooseProfile() {
    ServerConfig source = config;
    io.execute(
        () -> {
          try {
            JSONArray profiles =
                new ApiClient(source).json(source.api("profiles")).getJSONArray("profiles");
            List<JSONObject> ready = new ArrayList<>();
            List<String> names = new ArrayList<>();
            for (int i = 0; i < profiles.length(); i++) {
              JSONObject p = profiles.getJSONObject(i);
              if ("ready".equals(p.optString("status"))) {
                ready.add(p);
                JSONObject presence = p.optJSONObject("presence");
                String playing = presence == null ? "" : presence.optString("game_title", "");
                names.add(
                    p.optString("name")
                        + (playing.isEmpty() || playing.equals("null") ? "" : " · 正在玩 " + playing));
              }
            }
            ui(
                () -> {
                  if (ready.isEmpty()) {
                    alert("服务尚未提供可用玩家；可手动填写玩家路径");
                    return;
                  }
                  new AlertDialog.Builder(this)
                      .setTitle("选择玩家")
                      .setItems(
                          names.toArray(new String[0]),
                          (d, w) -> {
                            try {
                              ServerConfig next =
                                  new ServerConfig(
                                      source
                                          .resolve(ready.get(w).getString("base_path"))
                                          .toString(),
                                      source.allowHttp,
                                      source.authorization);
                              RetroApplication.from(this).credentials.save(next);
                              config = next;
                              platformId = "";
                              profileName = ready.get(w).optString("name", "玩家");
                              getPreferences(0)
                                  .edit()
                                  .putString(next.key() + ".name", profileName)
                                  .apply();
                              readLibraryPreferences();
                              show("library");
                            } catch (Exception e) {
                              alert(ApiClient.message(e));
                            }
                          })
                      .show();
                });
          } catch (Exception e) {
            ui(() -> alert(ApiClient.message(e)));
          }
        });
  }

  private void refreshProfile() {
    ServerConfig source = config;
    if (source == null || source.base.equals(profileRequestedBase)) return;
    profileRequestedBase = source.base;
    io.execute(
        () -> {
          try {
            JSONObject result = new ApiClient(source).json(source.api("profiles"));
            JSONArray profiles = result.getJSONArray("profiles");
            for (int i = 0; i < profiles.length(); i++) {
              JSONObject p = profiles.getJSONObject(i);
              if (!p.optString("id").equals(result.optString("current_profile_id"))) continue;
              String name = p.optString("name", "玩家");
              ui(
                  () -> {
                    if (config == null || !source.base.equals(config.base)) return;
                    profileName = name;
                    profileButton.setText(name + " ▾");
                    getPreferences(0).edit().putString(source.key() + ".name", name).apply();
                  });
              break;
            }
          } catch (Exception ignored) {
            profileRequestedBase = null;
          }
        });
  }

  private void readLibraryPreferences() {
    String key = config == null ? "unconnected" : config.key();
    android.content.SharedPreferences prefs = getPreferences(0);
    sort = prefs.getString(key + ".sort", "popularity");
    if (!java.util.Arrays.asList(LibraryQuery.SORTS).contains(sort)) sort = "popularity";
    direction = prefs.getString(key + ".direction", "desc");
    if (!"asc".equals(direction)) direction = "desc";
    scope = prefs.getString(key + ".scope", "all");
    profileName = prefs.getString(key + ".name", "选择玩家");
    pinnedPlatforms.clear();
    platforms.clear();
    settingsBase = null;
    settingsVersion++;
    settingsBusy = false;
    profileRequestedBase = null;
    query = "";
    if (profileButton != null) profileButton.setText(profileName + " ▾");
  }

  private void writeLibraryPreferences() {
    if (config != null)
      getPreferences(0)
          .edit()
          .putString(config.key() + ".sort", sort)
          .putString(config.key() + ".direction", direction)
          .putString(config.key() + ".scope", scope)
          .apply();
  }

  private void chooseSort() {
    if (config == null || settingsBusy) return;
    new AlertDialog.Builder(this)
        .setTitle("游戏排序")
        .setSingleChoiceItems(
            LibraryQuery.LABELS,
            java.util.Arrays.asList(LibraryQuery.SORTS).indexOf(sort),
            (d, w) -> {
              String selected = LibraryQuery.SORTS[w];
              d.dismiss();
              saveLibrarySettings(
                  null,
                  null,
                  selected,
                  selected.equals("name") || selected.equals("default") ? "asc" : "desc",
                  null);
            })
        .setNeutralButton(
            "asc".equals(direction) ? "改为降序 ↓" : "改为升序 ↑",
            (d, w) ->
                saveLibrarySettings(
                    null, null, sort, "asc".equals(direction) ? "desc" : "asc", null))
        .setNegativeButton("返回", null)
        .show();
  }

  private void cloudSave(ServerConfig source, JSONObject game) {
    LinearLayout content = column();
    content.setPadding(dp(20), dp(12), dp(20), dp(12));
    TextView info = text("正在读取当前玩家的云存档…", 16, TEXT);
    content.addView(info);
    AlertDialog dialog =
        new AlertDialog.Builder(this)
            .setTitle("云存档 · " + game.optString("title"))
            .setView(content)
            .setNegativeButton("返回", null)
            .create();
    dialog.show();
    io.execute(
        () -> {
          try {
            JSONObject state =
                new ApiClient(source)
                    .json(
                        source.api(
                            "games/" + ServerConfig.encode(game.getString("id")) + "/cloud-save"));
            ui(
                () -> {
                  if (!dialog.isShowing()) return;
                  if (!state.optBoolean("supported")) {
                    info.setText("此平台尚未接入云存档传输。现有进度仍保存在对应模拟器中。");
                    return;
                  }
                  JSONObject snapshot = state.optJSONObject("snapshot");
                  String description =
                      snapshot == null
                          ? "尚未建立同步快照。读取最新备份时会检查云端模拟器存档。"
                          : "版本 "
                              + snapshot.optInt("revision")
                              + " · "
                              + snapshot.optInt("file_count")
                              + " 个文件 · "
                              + bytes(snapshot.optLong("size_bytes"))
                              + "\n"
                              + java.text.DateFormat.getDateTimeInstance()
                                  .format(
                                      new java.util.Date(
                                          (long) (snapshot.optDouble("updated_at") * 1000)));
                  boolean busy = state.optBoolean("native_active") || state.optBoolean("leased");
                  info.setText(description + (busy ? "\n当前游戏正在运行或同步，请退出后读取备份。" : ""));
                  Button fetch = button("下载最新存档备份…", () -> {});
                  fetch.setEnabled(!busy);
                  content.addView(fetch);
                  content.addView(text("备份为 PSP 游戏内存档，导出 ZIP。不会自动替换本机进度；自动双向同步尚未开放。", 13, MUTED));
                  fetch.setOnClickListener(
                      v -> {
                        fetch.setEnabled(false);
                        info.setText("正在读取并保存云存档…");
                        io.execute(
                            () -> {
                              try {
                                JSONObject latest = SaveBackup.fetch(source, game.getString("id"));
                                if (latest.getJSONArray("files").length() == 0)
                                  throw new java.io.IOException("该玩家尚未保存过这款游戏的云端进度");
                                java.io.File dir =
                                    new java.io.File(
                                        getFilesDir(),
                                        "save-backups/"
                                            + source.key()
                                            + "/"
                                            + ServerConfig.hash(game.getString("id")));
                                if (!dir.isDirectory() && !dir.mkdirs())
                                  throw new java.io.IOException("无法创建备份目录");
                                java.io.File file =
                                    new java.io.File(
                                        dir,
                                        "revision-"
                                            + latest.getLong("revision")
                                            + "-"
                                            + java.util.UUID.randomUUID()
                                            + ".zip");
                                SaveBackup.zip(latest, file);
                                ui(
                                    () -> {
                                      info.setText("备份已保存 · 版本 " + latest.optInt("revision"));
                                      fetch.setEnabled(true);
                                      if (!dialog.isShowing()) return;
                                      saveExportPath = file.getAbsolutePath();
                                      try {
                                        startActivityForResult(
                                            new Intent(Intent.ACTION_CREATE_DOCUMENT)
                                                .addCategory(Intent.CATEGORY_OPENABLE)
                                                .setType("application/zip")
                                                .putExtra(
                                                    Intent.EXTRA_TITLE,
                                                    "RetroOne-save-"
                                                        + latest.optInt("revision")
                                                        + ".zip"),
                                            41);
                                      } catch (android.content.ActivityNotFoundException e) {
                                        alert("系统没有文件保存界面，备份已保留在本应用内");
                                      }
                                    });
                              } catch (Exception e) {
                                ui(
                                    () -> {
                                      info.setText(ApiClient.message(e));
                                      fetch.setEnabled(true);
                                    });
                              }
                            });
                      });
                });
          } catch (Exception e) {
            ui(() -> info.setText(ApiClient.message(e)));
          }
        });
  }

  private void apps() {
    body.addView(text("Apps · 设备上的应用", 28, TEXT));
    ScrollView list = new ScrollView(this);
    LinearLayout rows = column();
    list.addView(rows);
    body.addView(list, new LinearLayout.LayoutParams(-1, 0, 1));
    Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
    List<android.content.pm.ResolveInfo> apps =
        getPackageManager().queryIntentActivities(launcher, 0);
    apps.sort(new android.content.pm.ResolveInfo.DisplayNameComparator(getPackageManager()));
    int count = Math.max(2, Math.min(6, getResources().getConfiguration().screenWidthDp / 170));
    LinearLayout line = null;
    int index = 0;
    for (android.content.pm.ResolveInfo app : apps) {
      if (getPackageName().equals(app.activityInfo.packageName)) continue;
      if (index++ % count == 0) {
        line = row();
        rows.addView(line);
      }
      LinearLayout card = column();
      card.setGravity(Gravity.CENTER);
      card.setPadding(dp(12), dp(12), dp(12), dp(12));
      ImageView icon = new ImageView(this);
      icon.setImageDrawable(app.loadIcon(getPackageManager()));
      card.addView(icon, new LinearLayout.LayoutParams(dp(64), dp(64)));
      TextView name = text(app.loadLabel(getPackageManager()).toString(), 15, TEXT);
      name.setGravity(Gravity.CENTER);
      name.setMaxLines(2);
      card.addView(name);
      card.setFocusable(true);
      card.setClickable(true);
      card.setBackground(controlBackground(false));
      card.setForeground(getDrawable(android.R.drawable.list_selector_background));
      card.setOnClickListener(
          v -> {
            try {
              startActivity(
                  new Intent(Intent.ACTION_MAIN)
                      .addCategory(Intent.CATEGORY_LAUNCHER)
                      .setComponent(
                          new android.content.ComponentName(
                              app.activityInfo.packageName, app.activityInfo.name))
                      .addFlags(
                          Intent.FLAG_ACTIVITY_NEW_TASK
                              | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED));
            } catch (Exception e) {
              alert("应用暂时无法打开，请检查是否已停用或卸载");
            }
          });
      LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(0, dp(140), 1);
      layout.setMargins(dp(5), dp(5), dp(5), dp(5));
      line.addView(card, layout);
    }
    if (line != null)
      for (int i = index % count; i > 0 && i < count; i++)
        line.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1));
  }

  private void alert(String message) {
    new AlertDialog.Builder(this)
        .setTitle("RetroOne")
        .setMessage(message)
        .setPositiveButton("知道了", null)
        .show();
  }

  private LinearLayout column() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.VERTICAL);
    return l;
  }

  private LinearLayout row() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.HORIZONTAL);
    l.setGravity(Gravity.CENTER_VERTICAL);
    return l;
  }

  private TextView text(String value, int size, int color) {
    TextView v = new TextView(this);
    v.setText(value);
    v.setTextSize(size);
    v.setTextColor(color);
    v.setPadding(0, dp(6), 0, dp(6));
    return v;
  }

  private EditText edit(String hint, boolean password) {
    EditText v = new EditText(this);
    v.setTextColor(TEXT);
    v.setHintTextColor(MUTED);
    v.setHint(hint);
    v.setTextSize(16);
    v.setSingleLine();
    v.setInputType(
        InputType.TYPE_CLASS_TEXT | (password ? InputType.TYPE_TEXT_VARIATION_PASSWORD : 0));
    v.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
    return v;
  }

  private Button button(String value, Runnable action) {
    Button v = new Button(this);
    v.setText(value);
    v.setAllCaps(false);
    v.setTextColor(ACCENT);
    v.setTextSize(14);
    v.setMinHeight(dp(48));
    v.setBackground(controlBackground(false));
    v.setPadding(dp(14), dp(6), dp(14), dp(6));
    v.setOnClickListener(w -> action.run());
    return v;
  }

  private Button chip(String value, Runnable action) {
    Button v = button(value, action);
    v.setTextSize(13);
    v.setMinWidth(0);
    v.setMinimumWidth(0);
    v.setMinHeight(dp(44));
    v.setMinimumHeight(dp(44));
    v.setSingleLine(true);
    v.setPadding(dp(12), 0, dp(12), 0);
    LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-2, dp(44));
    layout.setMargins(0, 0, dp(6), 0);
    v.setLayoutParams(layout);
    return v;
  }

  private StateListDrawable controlBackground(boolean selected) {
    StateListDrawable states = new StateListDrawable();
    GradientDrawable focused = shape(0xff284960, 10);
    focused.setStroke(dp(2), ACCENT);
    states.addState(new int[] {android.R.attr.state_focused}, focused);
    states.addState(new int[] {android.R.attr.state_pressed}, focused);
    GradientDrawable normal = shape(selected ? 0xff263d53 : PANEL, 10);
    if (selected) normal.setStroke(dp(1), 0xff6294af);
    states.addState(new int[] {}, normal);
    return states;
  }

  private GradientDrawable shape(int color, int radius) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(radius));
    return d;
  }

  private int dp(int n) {
    return Math.round(n * getResources().getDisplayMetrics().density);
  }

  static String bytes(long value) {
    if (value <= 0) return "—";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return String.format(Locale.ROOT, "%.1f KiB", value / 1024d);
    if (value < 1024L * 1024 * 1024)
      return String.format(Locale.ROOT, "%.1f MiB", value / (1024d * 1024));
    return String.format(Locale.ROOT, "%.2f GiB", value / (1024d * 1024 * 1024));
  }

  private static String stateLabel(String value) {
    return switch (value) {
      case DownloadStore.QUEUED -> "等待下载";
      case DownloadStore.PREPARING -> "正在准备";
      case DownloadStore.RUNNING -> "下载与校验中";
      case DownloadStore.PAUSED -> "已暂停";
      case DownloadStore.FAILED -> "下载失败，可重试";
      case DownloadStore.READY -> "已就绪";
      default -> "已移除";
    };
  }
}
