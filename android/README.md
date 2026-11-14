# RetroOne Android

原生 Android 客户端：浏览自己的 RetroOne 游戏库，把独立游戏文件下载到本机，再交给已安装的 PPSSPP。不会启动服务端云游戏，也不附带游戏、模拟器、BIOS、固件或密钥。

使用步骤、兼容范围及真机验证见 [Android 使用与开发说明](../docs/android.md)。

## 构建

需要 JDK 17 或更新版本、Android SDK Platform 36。项目固定使用 Gradle 8.14.3、Android Gradle Plugin 8.13.2，使用 Java 原生界面和 AndroidX FileProvider。

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
