# 多人模式评审

评审日期：2026-10-19。用户要的 Network 是每位玩家拥有独立游戏实例，原游戏通过模拟的网络接口互连；各自控制角色，可拥有独立视角。这里不把通过网络分配同一台主机的手柄称为 Network。

## 三种能力分别记录

| 能力 | 实例与进度 | 当前结果 |
| --- | --- | --- |
| 多用户独立游玩 | 每人独立实例、存档、收藏与历史；可以运行相同游戏 | 双档案已部署；新增成员已在独立实例完成创建及隔离验收 |
| Same Console 同机多人 | 一台游戏主机的多个控制器；远程输入传输也属于此类 | 模拟器具有多人控制器能力，尚未进行实体多手柄验收 |
| Network 原生网络联机 | 多台独立主机通过原游戏的 LAN、Ad-hoc、Wi-Fi 等功能加入同一局 | PSP《山脊赛车2》已通过双实例同场比赛；其他游戏未据此自动标为支持 |

## 浏览器版本的实际情况

当前安装的是 EmulatorJS 4.2.3。`vendor/emulatorjs/data/src/emulator.js` 的构造函数要求同时打开 `EJS_DEBUG_XX` 和 `EJS_EXPERIMENTAL_NETPLAY` 才启用旧联机实现，代码还明确注明控制同步未完成。RetroOne 没有开启这些开关，也没有配置相应的房间服务。因此当前浏览器模式不能宣称支持跨设备联机。

上游 4.3.0-pre 加入了另一套 WebRTC 实现。检查上游 `data/src/netplay.js`（提交 `02356035f3181afe17f808d9c5c4f7268392f904`）可见它由房主 `captureStream(30)` 发送画面与声音，来宾回传控制器输入。这是 Same Console 的远程多人方式，不是多个模拟主机之间的原生网络协议，也不符合用户此次优先目标。

曾检查 NEStation（提交 `f71c175dd33bb950eea0bb7bc00aabb763d83d37`），它确实让两端分别运行 JSNES 并同步控制器，但模拟的仍是同一台 NES 的两只手柄。测试仅启动真实《魂斗罗》的标题画面；没有声称完成双人联机。用户澄清 Network 后已停止该方向的实验、关闭浏览器和本地信令服务，未接入正式镜像。

当前浏览器 NDS 的 EmulatorJS melonDS 构建明确在 Emscripten 分支设置 `HAVE_WIFI=0`，相应网络发送与接收函数为禁用实现。因此它不能直接提供原生 DS 无线联机。普通 mGBA Libretro 的联机支持请求仍未完成，其独立版的同机多窗口功能也不能直接用于当前浏览器核心。新版 melonDS DS 和 gpSP 的 Netpacket 接口属于另行验证的候选，不能仅替换核心名称便宣称浏览器支持。

## 候选与分流原则

**现有原生 PPSSPP Libretro 核心已确认具有 PSP Ad-hoc 能力**，无需仅为了联机就更换独立版。除了核对官方文档，还通过真实 Libretro ABI 读取了已安装二进制的选项：存在 WLAN 开关、内置 Ad-hoc 服务器、服务器 IPv4 地址、MAC、频道与端口偏移配置。该二进制 SHA-256 为 `19a25baeff92127d4cc4dc6c995182dc70efd3a16f4ab7954a9bfdce6ffb4237`。初步二进制检查只证明能力入口存在；下方的双浏览器实测进一步确认了具体游戏可互联。

PSP 首轮测试使用两个 Docker bridge 地址，各自独立存档与固定 MAC。玩家1启用内置服务，玩家2关闭；双方填写玩家1的 bridge IP，使用相同频道和端口偏移。服务器地址选项是固定枚举；自定义 IP 必须选择 `IP address` 并设置12个十进制位，不依赖任意 Docker hostname。测试配置生成器为 `artifacts/network-assessment/make-ppsspp-network-options.py`，所有生成值已逐项对镜像内核心定义验证；示例 IP 只是占位，不能直接用于部署。

GameCube 可评估 Dolphin 的宽带适配器与游戏 LAN；DS 可评估明确支持网络接口的 melonDS DS。不能把独立模拟器的能力直接套到同名 Libretro 核心，更不能套到 WebAssembly 构建。

尤其要注意术语例外：melonDS DS 文档说明它能利用 RetroArch Netplay 传输原生 DS 无线通信。因此应按实际的游戏/核心/构建能力判断，不能简单用“Netplay”这个名称决定属于哪一种玩法。

单人默认分流规则仍为：解压总量不超过 20 MiB 且浏览器核心和固件可用时在浏览器运行。将来选择 Network 时增加运行能力检查：浏览器构建确实支持相应网络接口才留在浏览器，否则评估独立云实例；云端也只有完成具体游戏的互联测试后才能标记为可用。

验收必须包含：两个不同玩家和独立存档、相同游戏版本、不同虚拟主机身份、通过游戏内网络菜单进入同一局、双方能独立操作角色、停止一方不会误停另一方。房间连通、双画面出现或两个进程存在，均不能单独作为成功依据。

## 已完成的原生 Network 实测

2026-10-19，NAS i7-1255U 上两套独立测试容器、独立 `/config`、固定不同 PSP MAC，以当前 PPSSPP 核心运行同一份《山脊赛车2》（ULJS00080；库 ID `f9eb2a58978f3b367e18a092`）。两个独立浏览器上下文进入 WIRELESS BATTLE：房主建立赛事，另一方查找并加入。比赛中红色与蓝色车辆各自显示独立视角、时速与名次；只让红车加速时，蓝车仍停在原地，其画面能看到红车驶过，同时双方名次从 2/2 与 1/2 对调。随后蓝车也能独立加速并超车。这是原游戏 Ad-hoc 通信，不是给同一模拟主机分配第二个手柄。

两路串流在同一 5 秒窗口各测得约 60 fps 的呈现帧率，分辨率 1280×720；此数值是浏览器视频呈现采样，不等于所有游戏或所有场景均可保持 60 fps。双方都有非零音频解码电平，尚未实际试听。刷新来宾网页后回到原比赛，两个模拟器会话 ID 不变；结束来宾游戏不会停止房主模拟器实例。

双方均通过游戏自己的保存界面建立存档，各自在独立持久化目录。测试未改家庭成员的真实游戏存档。证据：`artifacts/network-assessment/ridge-network-validation.json`、`output/playwright/ridge-p1-moving.png`、`ridge-p2-sees-p1.png`、`ridge-p2-moving.png`、`ridge-guest-reconnected.png`。目前仅对这一款已验证游戏开放，不能把整个 PSP 平台标成已验证 Network。

随后完成统一入口的邀请流程实测：通过顶栏家人头像发送邀请，另一浏览器接受；此时双方模拟器均未启动。两人各自点击“启动我的游戏”后，系统自动配置独立 PSP 身份及房主地址，在游戏内创建、加入同一场比赛。本轮交换了房主和来宾身份，玩家二加速至 103 km/h、名次 1/2，玩家一保持静止、名次 2/2，双方画面同步显示位置变化。

关闭邀请房间会保留两人的游戏。点击本人顶栏状态进入游戏空间后，结束操作先显示游戏名和未保存进度提醒，默认聚焦“继续当前游戏”；按 Escape 取消后会话 ID 不变。确认结束只停止本人实例，另一玩家仍保留原会话。游戏画面中的结束按钮同样需要确认。本轮未重新测量帧率、实际试听或连接实体手柄。证据：`artifacts/network-assessment/invitation-end-to-end-validation.json`、`output/playwright/invitation-p2-moving.png`、`invitation-p1-peer-moving.png`、`nas-current-status-header.png`、`nas-stop-confirmation.png`。

## 依据

- [EmulatorJS 发布记录](https://github.com/EmulatorJS/EmulatorJS/releases)：4.2.3 为稳定版，4.3.0-pre 为预发布版。
- [EmulatorJS 当前 WebRTC 实现](https://github.com/EmulatorJS/EmulatorJS/blob/02356035f3181afe17f808d9c5c4f7268392f904/data/src/netplay.js)：检查画面捕获、接收视频及输入回传代码。
- [EmulatorJS 联机服务说明](https://emulatorjs.org/docs4devs/netplay/)：房间/信令服务与配置入口；不能据此假定当前稳定版已经启用。
- [PPSSPP 多人设置](https://www.ppsspp.org/docs/multiplayer/quickstart/)：区分 Ad-hoc 与 Infrastructure，支持原生局域网玩法。
- [PPSSPP Libretro 核心](https://docs.libretro.com/library/ppsspp/)：文档的 Network 选项支持 PSP 联网；特性表的 Netplay 不可直接作为原生网络能力结论。
- [所装 PPSSPP 版本的网络配置代码](https://github.com/hrydgard/ppsspp/blob/1265a3cef4463000b78af5b7c31ab5fd219f2344/libretro/libretro.cpp)：读取 WLAN、MAC、12位 IP 和端口偏移；实际核心定义另存于 `artifacts/network-assessment/installed-ppsspp-options.json`。
- [EmulatorJS melonDS 构建配置](https://github.com/EmulatorJS/melonDS/blob/18a057d372783b42d7e297b8731f3c3d673cdcab/Makefile#L179)：Emscripten 分支禁用 Wi-Fi。
- [mGBA Libretro 联机请求](https://github.com/mgba-emu/mgba/issues/756)：普通 Libretro 核心的联机功能尚未实现。
- [melonDS DS Wi-Fi 与 LAN](https://docs.libretro.com/library/melonds_ds/)：区分 WFC 和局域无线通信，要求适当的实例身份与网络条件。
- [NEStation](https://github.com/afska/nestation)：双端模拟和按键同步，属于 NES 同机多人，不作为本次 Network 方案。
