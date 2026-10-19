# Switch 运行环境来源与构建

Switch 游戏通过每位成员自己的云端模拟器进程运行，画面和声音仍由现有 Selkies 服务传输。公开镜像不包含 Eden、游戏、固件或密钥；完整本地镜像使用下述固定上游版本。平台配置是否就绪与具体游戏是否可玩是两件事，不能仅凭安装成功承诺所有游戏达到 60 fps。

## 已审查的上游版本

本次选择 [Eden 官方稳定版 v0.2.1](https://git.eden-emu.dev/eden-emu/eden/releases/tag/v0.2.1)，发布日期为 2026-06-01。它提供 Linux amd64 构建，发布说明包含针对第 12 代 Intel CPU 的修复。这里使用通用 amd64 GCC 标准包，不使用针对 Steam Deck、Zen 4 或 ARM 优化的包。

- 官方下载：<https://stable.eden-emu.dev/v0.2.1/Eden-Linux-v0.2.1-amd64-gcc-standard.AppImage>
- 文件大小：70,220,957 字节。
- SHA256：`2fae658397daf13c118082a3eb65d61a6519967b5e22e6667756baecf6000c5a`。
- 官方源码：[v0.2.1 标签](https://git.eden-emu.dev/eden-emu/eden/src/tag/v0.2.1)；项目提供的 [GitHub 镜像](https://github.com/eden-emulator/mirror) 可供浏览。
- 许可证：[GPL-3.0-or-later](https://git.eden-emu.dev/eden-emu/eden/raw/tag/v0.2.1/LICENSE.txt)。该许可属于模拟器代码，不授予任何游戏、固件、密钥或封面的分发权限。

Ryubing 是另一个维护分支，其[官方组织](https://github.com/Ryubing)验证域名为 `ryujinx.app`，[官方文档](https://docs.ryujinx.app/)说明使用 MIT 许可。研究时其发布服务器连接不稳定，因此本轮选择已经从官方版本地址取到并校验的 Eden，不使用搜索结果中的同名下载站替代。

## 私有完整镜像构建

```sh
python3 scripts/fetch_runtime.py --verify-only
# 尚未下载时改用 python3 scripts/fetch_runtime.py；所有资产仍须符合已审查锁文件。
docker build --platform linux/amd64 -f Dockerfile -t retroone:local .
```

Eden 缓存在被 Git 忽略的 `vendor/eden.AppImage`。归档、来源和 SHA256 记录在 `container/runtime-lock.json`。`fetch_runtime.py` 只校验/准备本地资产，正常执行不会更新锁文件；公开 CI 不下载或打包它。

该 AppImage 使用 [URuntime](https://github.com/VHSgunzo/uruntime) 与 DwarFS，7-Zip 会误识别其中嵌入的 Zstd 数据，不能正确解出应用。私有 Dockerfile 在独立阶段安装 Debian `qemu-user`，先核对 SHA256，再执行官方包的 `--appimage-extract`。通过 QEMU 调用是为了兼容 Mac 上的 amd64 buildx；正式镜像只复制解出的 AppDir，不携带这个构建工具。运行入口保留上游 AppRun 所需的库与 hooks，不在每次游戏启动时解压整个包。

## 准备自己的游戏与依赖

保持 ROM 挂载只读，在宿主机按以下布局准备自己已有的文件，再点击“更新游戏库”：

```text
one/
├── library/switch/manual/我的Switch收藏/
│   └── 自有游戏本体.nsp             # 也可为 XCI / NRO
└── firmware/switch/
    ├── prod.keys                    # 自己主机导出的完整密钥
    ├── title.keys                   # 可选，仅在内容需要时提供
    └── firmware.zip                 # 自己导出的固件，内容为 NCA
```

也可以用 `firmware/switch/registered/` 下解开的 NCA 替代 ZIP；两者同时存在时优先使用 `registered/`。NCA 文件名应为 32 位十六进制内容 ID。不要混合不同版本的固件，压缩包不接受加密条目、符号链接或重复文件名。ZIP 内可以保留文件夹，准备时按 NCA 文件名放置。

游戏 ZIP / 7z 仅在能够安全解压且只有一个候选启动文件时可用。NSZ / XCZ 会显示需要转换，不会直接送入模拟器。更新、DLC 的安装与内容管理尚未集成，应导入可独立启动的本体；文件名无法可靠判断它们是否完整。

游戏无论大小均使用云端模拟。启动检查分别报告模拟器、密钥格式和固件结构；结构检查不会解密内容，也不保证密钥版本与游戏匹配。尚缺文件时保持“待配置”。即使 `.nro` 本身不加密，当前统一入口仍要求这些依赖就绪。

首次启动会在当前成员自己的配置目录准备固件副本、私有密钥和配置，所需空间随固件大小增加。后续启动复用同一代固件，替换源固件时保留旧代，不覆盖 NAND 存档。备份时保存每位成员完整的 `retroone/switch/user/`，不要只备份网页的用户设置。旧固件代不会自动清理，确认不再需要后可由管理员处理。

## 每位成员的数据边界

固定版本的 Qt 前端使用 `eden -f -u 0 -g /绝对路径/游戏.nsp` 启动；本项目通过 `/usr/local/bin/retroone-eden` 包装入口传入相同参数，保留上游 AppRun 的环境与库。不要套用其他版本文档中的 `--user-data` 或 `--config` 参数。v0.2.1 的 `--config` 仅属于另一个 SDL 命令行前端。参数依据[该版本 CommandLine 文档](https://git.eden-emu.dev/eden-emu/eden/raw/tag/v0.2.1/docs/user/CommandLine.md)及 Qt 参数解析源码。

Linux 下，Eden 优先使用当前工作目录中的 `user/`，否则使用 XDG 数据、缓存和配置目录。RetroOne 为每位成员保留独立 `/config`，在其 `/config/retroone/switch/` 工作目录内建立 `user/`，对应结构如下：

```text
/config/retroone/switch/user/
├── config/qt-config.ini
├── keys/prod.keys
├── nand/system/Contents/registered/  # 使用者已有的固件 NCA
├── nand/user/                      # 该成员自己的数据与存档
└── shader/                         # 该成员的着色器缓存
```

`title.keys` 仅在相应内容需要时由使用者提供。不要把其他成员的整个 NAND 或存档目录复制过来，也不要把只读 ROM 目录改成模拟器的可写用户目录。固件和密钥必须来自使用者已有资料，项目不提供下载；文件存在仅表示找到了候选依赖，运行时仍可能报告版本不符或内容损坏。

v0.2.1 的配置读取有一个重要细节：覆盖一个设置时还必须写入对应的 `字段\default=false`，否则程序可能继续采用内置默认值。初始图形配置采用 Vulkan、1 倍内部分辨率和掌机模式；云端的 720p/1080p 设置控制输出画布，不等于游戏内部渲染倍率。

## 输入、格式和退出限制

键盘映射如下；标准 Xbox / PlayStation 手柄沿用按键的物理位置，并保留双摇杆与扳机力度。Switch 的 A 位于右侧、B 位于下方，与 Xbox 的字母标记不同。

| 操作 | 键盘 |
| --- | --- |
| 左摇杆 / 右摇杆 | WASD / 方向键 |
| B / A / Y / X | J / K / U / I |
| L / R，ZL / ZR | Q / E，1 / 3 |
| − / ＋，按下左 / 右摇杆 | Shift / Enter，Z / X |

当前每个云端 Switch 实例合并一个玩家的键盘与首个标准手柄。其他成员仍使用各自实例；同实例多手柄、触屏虚拟手柄和 Switch 原生联网尚未验证。

Eden 支持 SDL 手柄与键盘，但这个版本的单个按键绑定不提供通用“键盘或手柄”合并表达式。它的双手柄映射主要用于左右 Joy-Con 拼接。RetroOne 的输入桥需要把键盘状态与浏览器标准手柄状态合入同一个虚拟手柄，保留真实摇杆轴；不能用方向键模拟来声称完整的模拟摇杆支持。Selkies 对浏览器手柄的转换也不等同于把 Switch 陀螺仪、NFC 或特殊附件全部透传。

上游加载器原生识别 NSP、XCI 和 NRO 等格式；ZIP 不是直接可启动格式，NSZ/XCZ 也不能直接交给这个版本的 Eden。压缩内容需要先经过明确、安全的准备步骤，不能根据扩展名就宣称可玩。补丁和 DLC 也不应作为独立游戏入口。

官方文档列出 Vulkan 1.1 或 OpenGL 4.6；实际 Vulkan 初始化还会校验所需扩展。Intel Iris Xe 需要可用的 Mesa ANV 驱动及真实 GPU 设备访问；这里没有依据宣称某个未经验证的 Mesa 最低小版本，也不使用 `MESA_VK_VERSION_OVERRIDE` 掩盖驱动能力不足。现有 i7-1255U 的实际帧率仍须逐游戏测试，串流编码达到 60 fps 不代表游戏本身运行到 60 fps。

结束游戏应先保存游戏内进度，再关闭该会话自己的 Eden 窗口，让 Qt 执行正常关机流程。超时强制终止与正常退出需要区别记录；不能把发出 SIGTERM/SIGKILL 当作“进度已保存”的证明。本项目也不把多个可启动实例视作已经验证的 Switch 原生联网。

## 本轮验证范围

- 官方包校验与 Docker 解包阶段通过；Mac amd64 环境的静态入口使用 QEMU 转接，空界面启动、仅关闭自身窗口及退出码 0 已验证。
- 已核对 Selkies 实际 SDL 手柄 GUID、按钮和轴映射，以及输入库在 Eden 进程中的加载；浏览器输入桥通过自动回归与实际 Selkies 轮询代码测试。
- 玩家目录隔离、固件安全准备、缺失依赖、格式分流和退出边界使用合成数据验证，不包含实际游戏或有效密钥。
- 尚未验证商业游戏解密、实际游戏内操控、存档再载入、原生 amd64 GPU 性能或多人联网。串流目标仍为 60 FPS，不代表 Switch 游戏能达到此速度。
