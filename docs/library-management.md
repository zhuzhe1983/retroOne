# 游戏库管理与启动配置

RetroOne 将资料展示、游戏资产与启动配置、玩家数据分开管理。扫描成功、模拟器进程存在或画面发生变化，都不代表游戏已经验证可玩。

## 数据分层

| 内容 | 容器路径 | 管理方式 |
| --- | --- | --- |
| 自己的 ROM、光盘轨道、PC 资源、BIOS | `/roms` | 只读挂载 |
| 检测记录、指纹报告、管理任务 | `/library-data/metadata/` | 全家共享，独立于 ROM |
| 每游戏生成配置与人工覆写 | `/library-data/runtime/game-configs/<游戏ID>.json` | 全家共享；生成与覆写分开 |
| 封面缓存 | `/artwork` | 独立媒体缓存 |
| 收藏、最近游玩、存档、画面偏好 | 每位玩家自己的 `/config` | 按玩家隔离 |

`RETROONE_LIBRARY_DATA_ROOT` 指定共享数据根目录；未设置时使用 `<CONFIG_ROOT>/library-data`。Compose 使用 `/library-data` 挂载。`RETROONE_LIBRARY_DATA_PATH` 与 provisioner 的 `RETROONE_HOST_LIBRARY_DATA_ROOT` 必须指向同一个宿主机绝对路径。先创建目录并授予运行用户写权限，再升级容器。已有额外成员也要增加该挂载，保留原 `/config`。

Pegasus 元数据和媒体目录仍可作为只读导入来源，服务不会改写它们。玩家索引保留原 ID 和标题以兼容现有存档；本次不重新编号、不自动搬迁存档。**移动、重命名 ROM 仍可能改变路径派生的游戏 ID**，请先备份并保留映射。显示名称、封面不能作为内容判重依据。

## 网页管理流程

打开 **管理游戏库**：

1. **扫描目录**：读取支持的平台和元数据，识别关联文件，为每款游戏生成启动配置。引用文件缺失的条目会保留，并显示原因。
2. **内容核验**：后台比较完整 SHA256，核验 ZIP 内容；按同平台大小和资源布局筛选候选。默认限速 8 MiB/s，分批读取约 2 GiB；单个更大的核验单元会完整读取后再暂停，确保续跑能前进。
3. 查看详情中的版本标记、指纹范围、原始副本和检测状态。地区版、修订版、汉化、改版只要内容不同就保留。文件名标签会注明来源，不冒充官方版本鉴定。
4. 修复缺失文件或调整配置后重新扫描，再核验。源文件变化或出现新的同名补丁后，旧去重证据会失效。

没有大小或布局相同候选的文件，默认可能尚未读取 SHA256。“本轮完成”表示候选任务结束，不代表全库已完整读取或逐款运行。使用命令行 `--hash-all` 可要求完整读取受支持内容。浏览器核心和服务器核心可能不同，检测结果不能混用。

当前列表只自动合并**完整内容相同、运行上下文相同、无旁侧资源歧义的独立卡带**。街机父子包、多碟游戏、PC 目录和其他压缩包继续保留，等待完整依赖确认。原 ID、收藏、历史和存档入口都保留；每个玩家优先看到自己最近玩过的副本，详情可打开其他原记录。**列表合并不会删除 ROM，也不代表释放磁盘空间。**

## 每游戏启动配置

扫描更新 `generated`，人工调整保存在 `overrides`。再次扫描保留人工覆写。配置关联 ROM 路径、源文件签名、有效模拟器核心、启动方式与现有输入映射；标题和封面不参与启动签名。

启动前重新核对资源与配置，明确的文件变化、缺失或停用问题会在结束旧会话之前返回原因。浏览器入口与云端启动均使用解析后的配置。生成配置不会标记游戏已经实测；BIOS、实际安装的核心和浏览器体积限制仍需检查。

可通过当前玩家接口读取或设置有限覆写：

```sh
curl 'http://localhost:8780/p/player1/api/games/<游戏ID>/configuration'
curl -X PATCH 'http://localhost:8780/p/player1/api/games/<游戏ID>/configuration' \
  -H 'Content-Type: application/json' \
  -d '{"mode":"stream","notes":"此版本使用服务器运行"}'
```

允许字段为 `mode`（auto/browser/stream）、平台白名单中的 `native_core` / `browser_core`、已识别布局的 `engine_hint`、现有 `input_profile`（目前仅 default）、`disabled`、`notes`。传 `null` 移除对应覆写。接口不接受命令、环境变量或任意执行路径。不同核心的存档格式可能不同，换核心前保留原存档。

## 命令行与隔离试运行

`scripts/library_audit.py` 只依赖 Python 标准库。输入为扫描导出的 `{games:[...],path_prefix:"/roms"}`，输出必须在 ROM 根目录外：

```sh
python3 scripts/library_audit.py --catalog /srv/retroone/catalog.json \
  --rom-root /srv/rom/one --cache /srv/retroone/audit/hashes.sqlite \
  --output /srv/retroone/audit/report.json --rate-mib 8 --max-read-gib 2
```

上述命令使用严格读取预算。不设预算并加 `--hash-all` 可读取全部受支持内容；`--platform nes --platform gba` 可限定平台。中断后用相同参数重跑，缓存会重新核对源文件状态。7z 等未完整解读的格式、资源边界不明确的目录保持未核验，不猜测重复。

`scripts/validate_games.py` 是独立的运行检测入口。当前适配 **FBNeo / FBA2012**，不能把该结果套到其他平台。manifest 必须明确核心及 DAT 的版本、SHA256 与来源。

```sh
python3 scripts/validate_games.py --manifest /private/validation.json \
  --rom-root /roms --system-root /system --output /results/check-001 \
  --level metadata --limit 3
```

`metadata` 比较 ZIP 目录和精确 DAT；`integrity` 读取内容；`observe` 加载真实核心并记录有限帧、输入时序及日志。每款游戏使用独立子进程、超时与可恢复结果。**observe 必须使用单独的受限检测容器**，例如 1 CPU、2 GiB 内存：只读挂载 ROM/BIOS、单独可写结果目录，不挂玩家存档、GPU 或 Docker socket，不接触生产会话。网页目前不会启动此类观察任务。

核心可能返回加载成功却显示缺 ROM 的错误画面；DAT 差异也不必然导致启动失败。因此结果分开记录依赖差异、核心报错、加载与帧观察，`playable` 保持未知。报告及实际游戏证据属于私有数据，不提交 GitHub。
