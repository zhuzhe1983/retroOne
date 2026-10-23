# 游戏库管理与启动配置

RetroOne 将资料展示、游戏资产与启动配置、玩家数据分开管理。扫描成功、模拟器进程存在或画面发生变化，都不代表游戏已经验证可玩。

## 数据分层

| 内容 | 容器路径 | 管理方式 |
| --- | --- | --- |
| 自己的 ROM、光盘轨道、PC 资源、BIOS | `/roms` | 只读挂载 |
| 标题、简介、排序与媒体引用 | `/library-data/metadata/games.sqlite3` | 全家共享；导入资料与人工修改分开 |
| 检测记录、指纹报告、管理任务 | `/library-data/metadata/` | 全家共享，独立于 ROM |
| 每游戏生成配置与人工覆写 | `/library-data/runtime/game-configs/<游戏ID>.json` | 全家共享；生成与覆写分开 |
| 封面缓存 | `/artwork` | 独立媒体缓存 |
| 收藏、最近游玩、存档、画面偏好 | 每位玩家自己的 `/config` | 按玩家隔离 |

`RETROONE_LIBRARY_DATA_ROOT` 指定共享数据根目录；未设置时使用 `<CONFIG_ROOT>/library-data`。Compose 使用 `/library-data` 挂载。`RETROONE_LIBRARY_DATA_PATH` 与 provisioner 的 `RETROONE_HOST_LIBRARY_DATA_ROOT` 必须指向同一个宿主机绝对路径。先创建目录并授予运行用户写权限，再升级容器。已有额外成员也要增加该挂载，保留原 `/config`。

Pegasus 元数据和媒体目录仍可作为只读导入来源，服务不会改写它们。新库可使用 `library/<平台>/ROM` 或 `library/<平台>/<收藏>/`，不再需要来源层；旧来源分层继续兼容。

已有库按完整集合搬迁时，`one/.retroone/library-layout.json` 的 `collections` 保存当前 `path` 与 `original_path`。扫描读取新路径，但沿用原路径对应的游戏 ID，保护收藏、游玩记录与存档关联；布局文件也需要备份。没有映射的手动移动、集合内部改名仍可能改变 ID。映射不能替代完整依赖检查，也不会自动搬迁玩家存档。示例与迁移边界见 [维护指南](operations.md#整理已有收藏)。显示名称、封面不能作为内容判重依据。

游戏资料由共享 metadata 库管理；每位玩家的 catalog 只是用于搜索和展示的本地索引。更新资料后，各玩家在下次读取时同步。重新扫描更新导入资料，保留人工修改；不会覆盖每游戏的模拟器配置。显示标题可修改，浏览器原有存档名称保持不变。

资料与启动配置使用不同接口。资料接口目前支持修改标题、简介和排序；媒体引用从只读导入源读取，封面图片仍使用独立缓存：

```sh
curl 'http://localhost:8780/p/player1/api/games/<游戏ID>/metadata'
curl -X PATCH 'http://localhost:8780/p/player1/api/games/<游戏ID>/metadata' \
  -H 'Content-Type: application/json' \
  -d '{"title":"家里熟悉的游戏名","description":"游戏介绍"}'
```

传 `null` 恢复对应导入字段。此接口不能修改 ROM 路径、模拟器、启动参数或存档。

## 网页管理流程

打开 **管理游戏库**：

1. **扫描目录**：读取支持的平台和元数据，识别关联文件，为每款游戏生成启动配置。引用文件缺失的条目会保留，并显示原因。
2. **内容核验**：后台比较完整 SHA256，核验 ZIP 内容；按同平台大小和资源布局筛选候选。默认限速 8 MiB/s，分批读取约 2 GiB；单个更大的核验单元会完整读取后再暂停，确保续跑能前进。
3. 查看详情中的版本标记、指纹范围、原始副本和检测状态。地区版、修订版、汉化、改版只要内容不同就保留。文件名标签会注明来源，不冒充官方版本鉴定。
4. 修复缺失文件或调整配置后重新扫描，再核验。源文件变化或出现新的同名补丁后，旧去重证据会失效。

没有大小或布局相同候选的文件，默认可能尚未读取 SHA256。“本轮完成”表示候选任务结束，不代表全库已完整读取或逐款运行。使用命令行 `--hash-all` 可要求完整读取受支持内容。浏览器核心和服务器核心可能不同，检测结果不能混用。

当前列表自动合并**完整内容相同、运行上下文相同、无旁侧资源歧义的独立卡带**；经过专门核验的 Switch 容器也可按完整 NCA 内容集合合并。街机父子包、多碟游戏、PC 目录和其他压缩包继续保留，等待完整依赖确认。原 ID、收藏、历史和存档入口都保留；每个玩家优先看到自己最近玩过的副本，详情可打开其他原记录。**列表合并不会删除 ROM，也不代表释放磁盘空间。**

“封装 1/2”只是导入时为同名条目分配的编号，不能据此判断是两张碟、不同地区或 MOD。版本标记会区分文件名推断与实际内容核验；内容不同但原因不明确时继续保留，不擅自标成改版。地区、汉化、更新和 DLC 也不能只凭大小推断。

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

`scripts/library_audit.py` 与仓库内的布局辅助模块只依赖 Python 标准库，运行时需保留对应仓库文件。输入为扫描导出的 `{games:[...],path_prefix:"/roms"}`，输出必须在 ROM 根目录外：

```sh
python3 scripts/library_audit.py --catalog /srv/retroone/catalog.json \
  --rom-root /srv/rom/one --cache /srv/retroone/audit/hashes.sqlite \
  --output /srv/retroone/audit/report.json --rate-mib 8 --max-read-gib 2
```

上述命令使用严格读取预算。不设预算并加 `--hash-all` 可读取全部受支持内容；`--platform nes --platform gba` 可限定平台。中断后用相同参数重跑，缓存会重新核对源文件状态。7z 等未完整解读的格式、资源边界不明确的目录保持未核验，不猜测重复。

目录游戏优先使用记录中的显式 `resource_root`。没有显式值时，只有布局清单声明的集合或已识别的历史来源层可用于推断：将收藏下的独立游戏目录视为完整资源根，不会按固定层数截取路径。Pegasus 元数据可能描述一款游戏，也可能描述整个收藏，仅有该文件不能证明完整游戏边界。新库中没有集合证据的嵌套 EXE 路径需显式指定资源根；目录条目则核验该完整目录。启动文件直接位于收藏根、游戏分散在多个目录或布局不明确时，应提供 `resource_root`，不能仅哈希一个 EXE 就判定整款游戏重复。

大集合的旁侧资源查询按目录缓存；目录发生变化或开始新一轮检测时重新检查。与 ROM 同名的补丁、配置或数据目录仍会阻止独立卡带合并，缓存不会忽略这些资源。

候选检测会先对同平台、同大小且至少 16 MiB 的不透明独立文件（例如 ISO、CHD）读取头、中、尾各 64 KiB。样本不同只排除“原始文件完全相同”；样本相同仍读取全文计算 SHA256。这一步不比较解码后的游戏内容，不产生完整指纹，也不会自动合并光盘游戏。ZIP、卡带、目录资源和 `--hash-all` 不采用此预筛。采样缓存使用独立键，并核对完整源文件状态；中断未完成的采样不写入缓存。

所有采样读取都计入限速和预算。报告中的 `bytes_hashed` / `actual_hash_bytes` 是实际参与哈希的读取量，包含中断、失败和采样；`bytes_sampled` / `actual_sample_bytes` 是其中的采样部分，不能据此推断有多少文件完成核验。`raw_verified` / `content_verified` 才是完整指纹数量；`raw_sample_excluded_games` 只表示通过样本差异排除原包完全相同的条目数。ZIP 目录等元数据读取仍不计入正文预算。

将报告发布为运行时检测记录时，显式 `--platform` 范围之外的平台保留原有证据；本轮报告内出现的游戏以新结果为准，原来的指纹不能盖过文件变化、错误或未完成检测。未限定平台的报告仍按全库替换处理，达到读取预算不等于只更新已完成的条目，因此应使用原缓存继续核验。保留记录仍须通过当前文件状态和旁侧资源检查，报告中的统计仅代表本轮检测范围。

对于 NSP / XCI，另有只读工具核对容器边界和每个 NCA 的完整 SHA256：

```sh
python3 scripts/audit_switch_packages.py --catalog /private/catalog.json \
  --root /srv/rom/one --ids /private/candidate-ids.json \
  --output /private/switch-audit.json --hash
```

不加 `--hash` 时只检查容器结构，不能判定内容重复。哈希结果复用前会核对源文件状态；大型镜像的完整读取可能耗时数小时。比较排序后的 `(SHA256, 大小)` 集合，可识别包装目录不同而 NCA 内容完全相同的容器。不同更新、DLC 或重新加密的 NCA 不会被当成完全相同。工具只生成私有审查报告，不自动发布合并记录或删除文件；完整哈希也不能代替密钥、固件和实际运行验证。被采纳的 `switch_nca_set` 合并证据仍须符合相同启动上下文、源文件未变且无旁侧资源歧义的条件。通用核验重跑会保留仍然有效的专门证据，源文件变化后则失效。

`scripts/validate_games.py` 是独立的运行检测入口。当前适配 **FBNeo / FBA2012**，不能把该结果套到其他平台。manifest 必须明确核心及 DAT 的版本、SHA256 与来源。

```sh
python3 scripts/validate_games.py --manifest /private/validation.json \
  --rom-root /roms --system-root /system --output /results/check-001 \
  --level metadata --limit 3
```

`metadata` 比较 ZIP 目录和精确 DAT；`integrity` 读取内容；`observe` 加载真实核心并记录有限帧、输入时序及日志。每款游戏使用独立子进程、超时与可恢复结果。**observe 必须使用单独的受限检测容器**，例如 1 CPU、2 GiB 内存：只读挂载 ROM/BIOS、单独可写结果目录，不挂玩家存档、GPU 或 Docker socket，不接触生产会话。网页目前不会启动此类观察任务。

核心可能返回加载成功却显示缺 ROM 的错误画面；DAT 差异也不必然导致启动失败。因此结果分开记录依赖差异、核心报错、加载与帧观察，`playable` 保持未知。报告及实际游戏证据属于私有数据，不提交 GitHub。
