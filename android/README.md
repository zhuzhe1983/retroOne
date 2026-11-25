# RetroOne Android

原生 Android 客户端：浏览自己的 RetroOne 游戏库，把游戏文件下载到本机，再交给已安装的 PPSSPP、Eden，或完成设置与测试的 RetroArch ARM64（NES / SFC / GB / GBC / GBA / MD / SMS / GG / PCE / 街机）。不会启动服务端云游戏，也不附带商业游戏、模拟器、BIOS、固件或密钥；卡带测试程序在构建时从原创源码生成。

使用步骤、兼容范围及真机验证见 [Android 使用与开发说明](../docs/android.md)。

## 构建

需要 JDK 17 或更新版本、Android SDK Platform 36 和 Python 3（仅标准库，用于构建原创 NES 诊断程序）。项目固定使用 Gradle 8.14.3、Android Gradle Plugin 8.13.2，使用 Java 原生界面和 AndroidX FileProvider。

```sh
# 在仓库根目录执行；也可以用已有的 JDK 17。
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
export ANDROID_HOME="$HOME/Library/Android/sdk"
./android/gradlew -p android assembleDebug testDebugUnitTest lintDebug
```

`android/local.properties` 也可配置 `sdk.dir`，该文件已忽略。APK 位于 `android/app/build/outputs/apk/debug/app-debug.apk`。调试 APK 只适合开发与本机验收；发布包须自行设置签名，不要将签名文件或密码写进仓库。

Gradle ZIP 校验值写在 `gradle-wrapper.properties`，Wrapper JAR 的官方 SHA-256 由项目公开源码检查器单独锁定；不豁免其他二进制文件。来源：[Gradle ZIP 校验值](https://services.gradle.org/distributions/gradle-8.14.3-bin.zip.sha256)、[Wrapper 校验值](https://services.gradle.org/distributions/gradle-8.14.3-wrapper.jar.sha256)。

## 安装到指定设备

```sh
adb -s DEVICE_SERIAL install --no-incremental -r android/app/build/outputs/apk/debug/app-debug.apk
adb -s DEVICE_SERIAL shell am start -n io.retroone.android/.MainActivity
```

包名为 `io.retroone.android`。更新安装保留现有应用数据；不要使用 `pm clear`、卸载或任何全设备清理命令。设备同时有 USB 和 Wi-Fi 连接时，禁止使用不指定 serial 的 adb 命令，也不要直接运行会选择所有设备的 `connectedAndroidTest`。

## 自建夹具与真机测试

测试服务使用真实 RetroOne API，独立扫描临时目录。不会访问生产 ROM 根目录、调用游戏启动 API，或生成用户存档。生成器源码为本项目原创并以 CC0 提供；生成的 PBP/ISO/CSO 不纳入版本控制。

```sh
.venv/bin/pip install -r android/tools/requirements-fixtures.txt
.venv/bin/python android/tools/fixture_server.py \
  --root artifacts/android/local-fixture --port 18780 --throttle-kib 2048
```

保留此终端，在另一个终端构建测试 APK：

```sh
./android/gradlew -p android assembleDebug assembleDebugAndroidTest
adb -s DEVICE_SERIAL install --no-incremental -r android/app/build/outputs/apk/debug/app-debug.apk
adb -s DEVICE_SERIAL install --no-incremental -r android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb -s DEVICE_SERIAL reverse tcp:18780 tcp:18780
adb -s DEVICE_SERIAL shell input keyevent KEYCODE_WAKEUP
adb -s DEVICE_SERIAL shell am instrument -w -r \
  -e class io.retroone.android.DeviceIntegrationTest \
  -e fixtureBase http://127.0.0.1:18780/p/player1 \
  -e fixtureQuery RetroOne-ISO-Self-Test -e launchPpsspp true \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

`fixtureQuery=RetroOne-CSO-Self-Test` 测试 CSO。`launchPpsspp=true` 会打开当前模拟器，用原创程序的确定像素值检查它确实运行；执行前应退出自己的游戏。不会调整 PPSSPP 的设置。测试期间会暂时使用夹具连接，结束时恢复原来的连接设置，保留已下载的自制程序。测试 APK 只有测试代码，正式应用没有可从 Intent 导入服务器或凭据的调试后门。

USB 转发会随断开连接而消失。本次 RK3588 验收中，USB reverse 在传输期间曾断开，因此最终改用测试机 LAN 地址：给服务增加 `--host <Mac的局域网IP>`，并将 `fixtureBase` 改为对应 HTTP 地址。**adb 仍只使用 USB serial**。不要把临时测试服务绑定到公网。

其他真机测试类（相同 `fixtureBase`）：

- `io.retroone.android.DownloadLifecycleTest`：32 MiB 原创字节流，暂停、续传、返回桌面后完成；完成后只移除该测试副本。
- `io.retroone.android.DownloadPlayUiTest`：真实点击下载、完成后卡片自动出现“游玩”、右上角浮层暂停续传、切换 Apps 后进度持续显示；加 `-e launchPpsspp true` 时点击浮层“游玩”并检查原创 PSP 程序实际渲染。仅在独立测试设备或已退出当前游戏的设备上执行。
- `io.retroone.android.RecoveryTest`：先单独运行 `-e phase prepare`，结束后 Android 会终止被测进程；再单独运行 `-e phase recover`，确认持久队列转为暂停并从已校验分片继续。不要在两次调用间清除数据。
- `DeviceIntegrationTest` 的 `-e verifyInput true` 是可选的键盘 Z→PSP CROSS 检查，需要模拟器具备相应映射。本机注入按键未通过这一检查，不能据此声称手柄已验证；默认测试仅断言程序画面正确。

缺少 `fixtureBase` 时设备测试跳过，不能把跳过当作验收通过。`am instrument` 的 shell 退出码不一定表示测试通过，要查看输出中的 `OK`、`FAILURES` 和测试数量。

已在客户端配置服务后，可独立验证真实目录的搜索与翻页（只读，不下载 ROM）：

```sh
adb -s DEVICE_SERIAL shell am instrument -w -r \
  -e class io.retroone.android.LibraryUiTest -e readOnlyLibraryUi true \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

这项验收使用当前连接，要求目录超过一页，且包含 Mario 搜索结果；不是适用于空库的通用测试。未显式传入 `readOnlyLibraryUi=true` 时跳过。

结束后 Ctrl-C 停止自己启动的临时服务，移除自己创建的转发：

```sh
adb -s DEVICE_SERIAL reverse --remove tcp:18780
```

只移除本次创建的转发；不要移除其他任务的转发。


0.2.1 的 `LibraryPreferencesUiTest` 使用同一 `fixtureBase`，从界面关注/取消关注 PSP、切换全库排序、收藏自制测试程序并验证服务端数据；只对独立夹具写入，结束恢复原连接及夹具偏好。`LibraryUiTest` 仍只读生产目录，增加紧凑布局高度、PSP 下载入口与玩家选择检查；宽屏须使用默认热度排序和全部游戏视图。截图留在设备应用的外部私有目录，不纳入公开仓库。

## 0.2.3 存储与同步验收

新增 `LocalContentUiTest`（先运行 `DeviceIntegrationTest` 准备原创下载）：验证离线“已下载”、本机排序、游戏文件删除保留备份，以及不同玩家备份单独移除。验证迁移时可先在 0.2.2 下载夹具，再覆盖安装 0.2.3；不要清空应用数据。

`SaveSyncUiTest` 使用同一 `fixtureBase`，通过测试 APK 专属 DocumentsProvider 验证真实云存档 API 与目录读写：取消、选云端、选本机、比较期间两端分别变化、替换失败回滚、其他游戏文件不变和占用释放。它会给独立夹具游戏写入原创测试字节；**只对独立测试服务与设备运行**。测试 Provider 和授权 Activity 仅在 AndroidTest APK 中，正式 APK 不包含这些入口。实际用户目录仍由 Android 系统选择器授权。

手动同步支持 PSP 游戏内 SAVEDATA，要求先退出 PPSSPP。还没有运行中自动同步或模拟器玩家环境隔离；完整操作与边界见 [使用说明](../docs/android.md#023-已下载本地内容与存档冲突)。

## 0.2.4 NES 验收

`tools/create_nes_fixture.py` 是 CC0 原创 NROM-128 程序生成器：蓝条和 RETROONE 字样，按 A 变红；`--variant alternate` 生成绿条与 RETROONE ALT，用于区分真实游戏切换。Gradle 自动生成应用内探针，二进制只进入构建目录；源码不包含商业 ROM、BIOS 或固件。`fixture_server.py` 已把这两份 NES 加入独立测试库。

首先按 [NES 设置步骤](../docs/android.md#024-nes-直启与依赖检查) 授权目录，确认核心、画面和输入，再运行：

```sh
adb -s DEVICE_SERIAL install --no-incremental -r android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb -s DEVICE_SERIAL shell am instrument -w \
  -e class io.retroone.android.RetroArchDeviceTest \
  -e probeRetroArchFolder true \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

这检查应用探针的固定 SHA-256、FileProvider 不能越过专用目录、SAF 写入与二次复用同一副本，以及启动 Intent 的六项环境参数指向已安装的 RetroArch、默认全局配置和当前系统输入法。配置路径应为目标模拟器的路径，不能误用 RetroOne 自己的 dataDir / APK。RetroOne 不改写配置内容。默认不启动模拟器；先从 RetroArch 菜单退出旧实例，显式添加 `-e launchRetroArch true` 才启动，必须另外核对蓝条画面和 A 键变色，不能把测试返回成功当成游戏已经运行。

`-e class io.retroone.android.RetroArchDeviceTest#diagnosticContentUriLaunch -e probeRetroArchUri true` 只用于诊断普通 content URI 的兼容性（带 ClipData 只读授权）。正式适配使用经过授权的共享原生路径；该诊断只报告 Intent 发出，不宣称内容运行。请先结束自己正在进行的 RetroArch 游戏。

### Eden URI 独立诊断

测试 APK 中的 `EdenUriProbeTest` 可验证 Eden 对单文件 FileProvider URI 的支持。先在 Eden 保存并退出当前游戏，再明确指定一个已完成下载的 Switch 任务 UUID：

```sh
adb -s DEVICE_SERIAL shell am instrument -w \
  -e class io.retroone.android.EdenUriProbeTest#explicitReadyTaskReadOnlyUri \
  -e edenUriProbe true -e edenTaskId DOWNLOADED_TASK_UUID \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

只接受 READY 的 NSP/XCI；不自动选择游戏，不下载、不复制镜像，不配置密钥/固件。使用 `dev.eden.eden_emulator/org.yuzu.yuzu_emu.activities.EmulationActivity` 的 `ACTION_VIEW` 和单文件只读授权。测试通过表示已发出启动请求，需另外核对真实画面、按键和日志；不能据此认定任何 Switch 游戏都兼容；0.2.5 正式入口与独立诊断的验证范围分别记录。模拟器自行管理运行记录与存档，诊断不会主动结束已有模拟器进程。

探针显式授予 Eden 对这个稳定 URI 的只读访问，并默认保留进程 15 秒（`edenWaitMs` 可设 5000–45000）。为避免游戏按需读取时突然失去权限，成功派发后保留该文件授权；保存并退出 Eden 后用同一任务 UUID 回收：

```sh
adb -s DEVICE_SERIAL shell am instrument -w \
  -e class io.retroone.android.EdenUriProbeTest#revokeExplicitTaskGrantAfterExit \
  -e edenUriRevoke true -e edenTaskId DOWNLOADED_TASK_UUID \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

首个即时结束的探针曾出现 FileProvider Permission Denial；因此 Intent 派发成功不能当成文件已读或游戏已运行。

## 0.2.5 原生适配验收

正式 UI 新增 Switch / Eden 和街机 / FinalBurn Neo 入口，使用流程与限制见 [Android 使用说明](../docs/android.md#025-switch-与街机启动)。Eden 的 `resumeIntent` 不带 data 或选定游戏 extras，避免 v0.2.1 把同款恢复当作 ROM swap；这与冷启动的只读 URI 不同。实现依据：[Eden v0.2.1 EmulationActivity](https://git.eden-emu.dev/eden-emu/eden/src/tag/v0.2.1/src/android/app/src/main/java/org/yuzu/yuzu_emu/activities/EmulationActivity.kt)。

`NativeAdapterDeviceTest` 每个设备用例独立运行。真实游戏测试必须显式指定已下载任务 UUID，不自动选择游戏，不改连接、固件或存档：

- `#edenResumeCannotRequestRomSwap`：只断言恢复 Intent 无 URI、游戏 extras 或 grant，不启动应用。
- `#edenDispatchFailureRevokesOnlyNewReadGrant -e edenFailureProbe true -e edenTaskId UUID`：先保存退出 Eden，并通过设置回收旧授权。故意在派发前抛启动异常，确认新只读权和持久记录均回收，不会实际开始游戏。已有授权时拒绝执行，避免干扰后台游戏。
- `#explicitFbneoArchiveAndEnvironment -e fbneoAdapterProbe true -e fbneoTaskId UUID -e expectedSet SET`：按官方索引识别、校验和复用共享副本，核对核心与默认配置。仅显式添加 `-e launchFbneo true` 才发启动 Intent；需另外检查画面和输入。
- `#explicitUnmatchedArchivePreserved -e fbneoMismatchProbe true -e fbneoTaskId UUID`：只针对明确选定的未匹配改版归档，验证拒绝直启且下载原件状态、大小和时间不变；会保存本机核对提示。

所有方法均位于 `io.retroone.android.NativeAdapterDeviceTest`，使用前文相同的 instrumentation runner。不得把跳过、Intent 派发或仅模拟器前台计作游戏运行通过。

### FBNeo 索引来源

`app/src/main/assets/fbneo-sets.json` 只包含 ROM 集标识、成员名称／CRC／大小、父集／BIOS 关联及 CRC/size 多重集的 SHA-256，不包含任何 ROM 字节、固件或密钥。来源为 [官方 DAT 固定提交 2e6605c4](https://github.com/libretro/FBNeo/blob/2e6605c4a08b86455e3aee03294fe35e7618fa91/dats/FinalBurn%20Neo%20%28ClrMame%20Pro%20XML%2C%20Arcade%20only%29.dat)，原文件 SHA-256 为 `0031b6557d493bc52d601e25b640e8a2d830bd6c78d4f3fff227463d2c62ae88`。索引附上游完整 [许可证](app/src/main/assets/fbneo-license.txt)，适用范围见该文件；APK 不附带 FBNeo 核心。

可用 `python3 android/tools/build_fbneo_index.py /path/to/reviewed.dat android/app/src/main/assets/fbneo-sets.json --neogeo-source /path/to/d_neogeo.cpp` 重建；NeoGeo 的必需／可选标志来自同一固定提交的 `neogeoRomDesc`，源码 SHA-256 为 `7e51b413aab7da1965ba2d003d0b76fd7e5dfac5a9f82ea356fc74b0680602b7`。生成器拒绝不同摘要，更新前需审阅新 DAT 与核心版本。打开 `ZipFile` 前先以有界读取核对标准 ZIP、EOCD、全部中央目录记录与计数；限制物理大小 512 MiB、全部条目 4096、中央目录 4 MiB，拒绝 ZIP64／多卷／加密／前缀。JVM 回归包含稀疏超大文件、大量目录、伪造计数、超大目录记录与截断。严格匹配可能保守拒绝缺可选 PLD 的有效游戏，界面会说明需核对，不将其误判为下载损坏。官方背景：[FBNeo ROM 集与依赖说明](https://docs.libretro.com/library/fbneo/)。

## 0.2.6 卡带与精确 MOD 兼容验收

`create_handheld_sega_fixtures.py`、`create_snes_pce_fixtures.py` 从原创源码生成八个平台的测试程序，构建时自动打入 APK。可单独运行 `python3 android/tools/create_handheld_sega_fixtures.py /tmp/retroone-handheld-probes` 与 `python3 android/tools/create_snes_pce_fixtures.py /tmp/retroone-snes-pce-probes`；已有目录默认拒绝覆盖，`--replace` 只覆盖固定产物。两个 `build_all()` 供隔离 API 夹具使用。生成器不下载 ROM/BIOS，GB/GBC/GBA logo 区域为空，验收使用核心内置启动或跳过 BIOS；不会自动改变用户设置。

`CartridgeDeviceTest` 的入口：

- `#zipExtractionPublishesAndReusesOnAndroid`：把九个原创探针临时打成 ZIP，在应用私有缓存验证真实提取、重命名发布、重复复用、异常已有副本不覆盖及 pending 清理。不访问服务或共享目录，不改验证状态，结束只清理这次独立测试目录。此项用于回归 Android SELinux 禁止硬链接的问题。
- `#originalCartridgeAssets`：检查所有已打包原创程序的基本结构，不启动模拟器。
- `#authorizedCartridgeRoundTrip -e cartridgeFolder true -e cartridgePlatform snes`：使用应用中明确授权的共享目录，验证该平台副本创建／复用、核心路径、六项启动环境、其他平台路径拒绝，以及不改变 NES 确认。仅加 `-e launchCartridge true` 才实际派发启动；需另看画面和输入，测试本身不设置“已确认”。平台参数可为 `snes/gb/gbc/gba/megadrive/mastersystem/gamegear/pce`。
- `#fixtureDownloadAndStage -e cartridgeDownload true -e cartridgePlatform snes -e fixtureBase http://TEST_HOST:18780/p/player1`：先在正式设置页面确认这个平台的原创探针。仅对独立夹具下载 `RetroOne-Cart-平台-Self-Test`，分片校验后创建并复用共享副本；不更改当前服务连接。加 `-e cartridgeZip true` 改为下载唯一 ROM 的 ZIP 夹具。运行后通过本地内容界面精确移除这些测试下载，保留正常取消历史。

NES 的原 `fixtureDownloadStagesWithoutChangingConnection` 增加可选 `-e nesZip true`，在两款原探针外增加一份单 ROM ZIP；已有 NES 验证状态继续使用。ZIP 解析采用 CP437 处理未标记 UTF-8 的旧档案，带 UTF-8 标记的条目仍按 UTF-8；文件名只做后缀及安全路径检查，不作为输出路径。实际 CRC、大小和卡带头均需通过；缓存目录采用同目录重命名发布（不覆盖、进程内串行），并核对已有字节；不使用 Android SELinux 禁止的硬链接。真机须覆盖实际解包，不能以纯 JVM 结果代替 Android 文件系统验证。

圆桌兼容配置新增 `NativeAdapterDeviceTest#explicitCompatibilityArchiveAndEnvironment -e arcadeCompatProbe true -e fbneoTaskId UUID`：只接受 `ArcadeCompatProfile` 内固定 SHA-256 对应的 MOD，确认副本复用、MAME2003-Plus 核心和原件不变；仅加 `-e launchArcadeCompat true` 才启动。它不属于 `explicitUnmatchedArchivePreserved` 的负例；后者仍用于没有专用兼容配置的未知归档。正式入口必须明确显示兼容模式，首次仍需真实画面和投币／动作确认。

所有真机操作由持有设备的会话统一执行，先保存并退出已有游戏，不清应用数据、不强杀其他会话。普通平台的 BIOS 范围以 [Snes9x](https://docs.libretro.com/library/snes9x/)、[Gambatte](https://docs.libretro.com/library/gambatte/)、[mGBA](https://docs.libretro.com/library/mgba/)、[Genesis Plus GX](https://docs.libretro.com/library/genesis_plus_gx/) 和 [PCE Fast](https://docs.libretro.com/library/beetle_pce_fast/) 官方说明为依据；本次 PCE 仅 HuCard，不含 CD 或 SuperGrafx。


NeoGeo 的正式流程先通过系统文件选择器导入用户自己的 BIOS，再从游戏入口准备副本。可选设备检查：

```sh
adb -s DEVICE_SERIAL shell am instrument -w -r \
  -e class io.retroone.android.NativeAdapterDeviceTest#explicitNeoGeoBiosAndArchive \
  -e neoGeoAdapterProbe true -e fbneoTaskId DOWNLOADED_TASK_UUID \
  -e expectedSet mslug -e expectedMerged false \
  io.retroone.android.test/androidx.test.runner.AndroidJUnitRunner
```

合并 KOF97 样本改为 `expectedSet kof97`、`expectedMerged true`。测试复核实际成员、BIOS、失败／取消导入保留旧选择、共享副本复用及连接／下载原件不变，不派发模拟器，也不把内容匹配标成可玩。正式游玩须另测画面、投币、开始与动作。BIOS 私有副本按 SHA-256 命名；共享 bundle 的路径同时包含游戏与 BIOS 摘要，避免更换 BIOS 后污染旧会话，旧副本保留供退出模拟器后清理。所有样本、BIOS 和设备证据仅保留在本地忽略目录，不进入仓库或公开产物。
