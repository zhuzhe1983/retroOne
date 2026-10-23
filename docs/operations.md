# 部署与维护

本文补充 [README](../README.md) 的使用方法。示例使用 `/srv/rom/one` 保存收藏，`/srv/retroone` 保存运行数据，请换成自己的宿主机绝对路径。不要将示例路径直接当作现有部署迁移指令。

## 首次部署

当前 Compose 云端配置要求 x86-64 Linux、Docker Engine、Compose 插件，以及 Intel `/dev/dri/renderD128`。宿主机驱动需能正常使用该设备。NVIDIA 尚未完成运行链路，macOS Docker 也不能直接使用这个设备映射。

### 1. 准备目录与身份

选择一个非 root 的运行用户，确保它能读取 ROM、写入数据目录。以下命令针对 Linux GNU 工具，仅为新部署创建目录，不移动或覆盖已有数据。

```sh
sudo mkdir -p /srv/rom/one/library /srv/rom/one/firmware
sudo mkdir -p /srv/retroone/data/retroone/artwork
sudo mkdir -p /srv/retroone/data/retroone/profile-control
sudo mkdir -p /srv/retroone/profiles/player2
sudo mkdir -p /srv/retroone/library-data
sudo chown -R "$(id -u):$(id -g)" /srv/retroone
```

ROM 的现有权限保持不变，确认运行用户已有读取权限即可。下面的 `.env` 写在仓库根目录，**仅在首次配置时生成**，已有配置请逐项编辑。

```sh
cat > .env <<EOF
RETROONE_IMAGE=retroone:local
PUID=$(id -u)
PGID=$(id -g)
RETROONE_SUPPLEMENTARY_GIDS=$(stat -c '%g' /dev/dri/renderD128)
RETROONE_DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
RETROONE_GPU_BACKEND=intel
RETROONE_ROM_PATH=/srv/rom/one
RETROONE_CONFIG_PATH=/srv/retroone/data
RETROONE_ARTWORK_PATH=/srv/retroone/data/retroone/artwork
RETROONE_PLAYER2_CONFIG_PATH=/srv/retroone/profiles/player2
RETROONE_PROFILES_PATH=/srv/retroone/profiles
RETROONE_HOST_PROFILES_ROOT=/srv/retroone/profiles
RETROONE_LIBRARY_DATA_PATH=/srv/retroone/library-data
RETROONE_HOST_LIBRARY_DATA_ROOT=/srv/retroone/library-data
RETROONE_DOCKER_NETWORK=retroone_default
RETROONE_MAX_PROFILES=16
RETROONE_HTTP_PORT=8780
RETROONE_HTTPS_PORT=8781
EOF
```

`PUID` / `PGID` 是容器内运行用户的 UID / GID；附加组需要允许读取 ROM 和访问渲染设备。Synology 等 NAS 还可能有共享目录 ACL，按实际权限设置附加组，不要为了运行服务开放整个 ROM 目录的写权限。Docker socket 的组号必须从当前宿主机读取，不能照搬其他机器。

`RETROONE_PROFILES_PATH` 是 Compose 挂载位置，`RETROONE_HOST_PROFILES_ROOT` 是新增成员容器使用的宿主机绝对路径，两者必须对应同一个目录。封面缓存共用 `/artwork`，存档和设置则各自独立。`RETROONE_LIBRARY_DATA_PATH` 与 `RETROONE_HOST_LIBRARY_DATA_ROOT` 同样必须对应同一个绝对路径，供所有成员共享检测资料与每游戏启动配置；详见 [游戏库管理](library-management.md)。

### 2. 构建与启动

公开入口版本不需要私有 `vendor/` 组件：

```sh
docker build -f Dockerfile.public -t retroone:local .
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=100 retroone
```

使用已发布镜像时，将 `.env` 的 `RETROONE_IMAGE` 改为确切标签，再 `docker compose pull` 和 `docker compose up -d --no-build`。不要把公开入口镜像和私有完整构建的能力混为一谈，详细差异与完整构建方法见 [镜像与发布说明](releasing.md)。

主入口默认 HTTPS `8781`、HTTP `8780`。可信证书反向代理需保留 WebSocket 转发、路径前缀及隔离响应头，不要强行长期缓存入口 HTML。页面使用 JS/CSS 内容版本号，避免新 HTML 混用旧脚本。

### 3. 检查后再扩充

先检查网页玩家选择、扫描和单款游戏，再检查保存后退出、重新启动恢复，以及第二玩家的数据隔离。`/api/health` 提供目录扫描及运行能力状态；玩家实例的接口位于 `/p/playerN/api/`。这些状态只能说明环境检测结果，不能替代每款游戏的实际验证。

## 成员实例与升级

同一个镜像供主玩家、第二玩家、新增成员和成员创建服务使用。主入口按 `/p/playerN/` 转发，各成员实例不另外公开宿主机端口。玩家 1 的持久化目录是 `data/`，其他玩家通常是 `profiles/playerN/`；容器内均挂载为 `/config`。

独立的 provisioner 处理新增成员队列并访问 Docker socket，网页和游戏容器本身不挂载 socket。新增成员只有经过同源健康检查才可进入，准备失败可重试。`RETROONE_MAX_PROFILES` 可设为 2～32，默认 16；这是档案数量限制，并发游戏数量仍需按主机性能安排。

升级前先通知正在游玩的家人，在游戏内保存并结束各自会话，然后备份数据。更新镜像时保留全部数据挂载，不能将 `/config` 换成空目录。

如果为保留正在运行的游戏而先更新页面、稍后升级成员接口，需要验证普通网页启动流程。旧接口可能以 HTTP 422 拒绝新的 `device_id`：网页仅在明确收到这一项多余参数错误时，去掉该参数重试一次，并提示沿用玩家默认画质。它不会在超时或会话冲突时自动重试，也不会去掉会话替换、联机房间等保护参数。按设备设置画质仍需要升级对应成员接口。

**`docker compose up -d` 不会自动升级 provisioner 之前创建的额外成员容器。** 更新它们时需逐个保留原来的玩家编号、环境变量、网络和配置挂载，使用目标镜像重建。`RETROONE_PROFILE_ID`、`RETROONE_BASE_PATH` 和 Selkies `SUBFOLDER` 必须一致，例如 `player3`、`/p/player3`、`/p/player3/stream/`。

## 存档与备份

备份整个运行数据根目录中的 **`data/`、`profiles/` 和 `library-data/`**，不要只备份 Docker 镜像或 `saves/`。DOS / Windows 原生存档可能留在工作副本里。可恢复备份应在游戏正常保存并结束后进行，避免复制正在写入的文件。

| 内容 | 玩家 1 数据目录下的相对位置 |
| --- | --- |
| 目录索引、收藏、最近记录与成员注册表 | `data/retroone/` |
| 浏览器主动备份的即时状态 | `data/retroone/browser-saves/<游戏ID>.state` |
| RetroArch / SDLPAL 存档和状态 | `data/retroone/saves/` |
| PS2 记忆卡和状态 | `data/retroone/pcsx2/PCSX2/` |
| DOS / Windows 工作副本、原生存档、Wine 前缀 | `data/retroone/work/` |
| GameCube / Wii 数据 | `data/retroone/dolphin/` |
| 玩家画面偏好 | `data/retroone/display-settings.json` |

其他玩家将 `data/` 替换为 `profiles/playerN/`。游戏库 `one/` 应另有备份；只读挂载和文件系统写时复制都不能代替备份。

浏览器普通游戏存档留在 IndexedDB 中，服务器即时状态备份需要玩家主动操作。更换网站域名也会更换浏览器存储来源，应提前导出或备份状态。即时状态受模拟器及核心版本影响，游戏内保存与即时状态最好分别保留。RetroArch 常用快捷键为 F1 菜单、F2 保存状态、F4 读取状态。

## 固件与运行组件

固件根目录为 `one/firmware/`，只使用自己已有且可使用的文件。PS2 BIOS 从 `one/firmware/pcsx2/` 读取；3DS 的实验运行环境可读取 `one/firmware/azahar/`。其他平台的文件名检测见 [runner.py](../retroone/runner.py) 和 [api.py](../retroone/api.py)，不应仅凭同名 `firmware.bin` 判断是正确固件。

缺少核心与缺少固件是两件事，补充 BIOS 不会为尚未接入的系统创建模拟器。Switch 的 Eden 运行组件由本地完整镜像提供，还需要自己的密钥和匹配固件；准备目录、格式与验证边界见 [Switch 指南](switch-runtime.md)。NVIDIA 仅预留接口。3DS 存档恢复问题尚待解决，不应使用重要进度直接做兼容性实验。

本地完整构建所需组件的来源、版本与校验值记录在 [UPSTREAM.md](../container/UPSTREAM.md) 和 [runtime-lock.json](../container/runtime-lock.json)。保存自己的已核验缓存和私有镜像。上游滚动下载内容改变时，校验应失败，不能为了构建通过而随意更新散列值。

## 封面与元数据

优先读取收藏内的媒体文件和 Pegasus 元数据，再使用可核验的同平台同作图片及 Libretro 图库。图片无法准确匹配时保留文字占位卡，不用无关游戏海报填补。共享缓存通过 `RETROONE_ARTWORK_PATH` 挂载到 `/artwork`。

`audit_artwork.py` 读取布局中的集合边界，并在库存中记录 `collection_path`；`prepare_artwork.py` 按该边界优先复用本集合封面。数字编号等通用文件名只在同一明确集合内匹配，不能把另一套收藏的 `001` 当作同一款游戏。新旧布局均可使用，辅助脚本需与仓库内的 `retroone.library_layout` 一起部署。

自动匹配或下载工具的预计匹配量不代表已经发布的图片数量。维护批量任务时保留来源、匹配依据和实际覆盖报告；这些私人收藏报告、封面文件与游戏媒体不提交到公开仓库。

## 整理已有收藏

新导入使用 `one/library/<平台>/ROM` 或 `one/library/<平台>/<收藏>/`，无需增加来源层。旧收藏也可继续使用原目录，不必为了升级立刻搬动文件。

已有收藏移除来源层时，按完整集合迁移，例如将 `library/fbneo/new/FBNEO ACT/` 移到 `library/fbneo/FBNEO ACT/`，保留集合内部的相对路径。布局文件 `one/.retroone/library-layout.json` 记录当前集合根与原集合根：

```json
{
  "schema_version": 1,
  "collections": [
    {
      "path": "library/fbneo/FBNEO ACT",
      "original_path": "library/fbneo/new/FBNEO ACT"
    }
  ]
}
```

扫描使用当前路径读取 ROM、媒体和关联文件，以映射后的原路径生成原游戏 ID；收藏、最近游玩和按 ID 关联的存档无需重新编号。应同时备份布局文件、各玩家 `/config`、共享 `library-data/` 和封面缓存。迁移前结束涉及的游戏，先核对计划与目标冲突，再搬迁并重新扫描、核对原 ID 与关联文件。布局缺失、配置冲突或无效时，不应继续手动改路径来绕过错误。文件夹内任意改名不在集合搬迁保护范围内。

使用 `scripts/consolidate_library.py` 完成这类迁移。它只处理 `new`、`old`、`pegasus`、`import` 四种历史来源层，其他目录保留并列入报告；同名集合采用“（合集 2）”等中性后缀，不覆盖、合并或删除其中的游戏。集合内文件位置保持不变。若元数据、光盘描述文件等从集合外引用待移动文件，或跨集合引用不能安全保留，预检会拒绝迁移，需先明确解决这些关联。

以下路径是维护进程中的挂载路径，`--profile-config-root` 指向每个玩家的 `retroone` 配置子目录，必须列全所有玩家。维护进程使用新版镜像和可写 ROM 挂载；平时游戏服务仍只读挂载 ROM。

```sh
# 默认只生成计划，不修改 ROM 或玩家数据
python3 scripts/consolidate_library.py \
  --rom-root /roms --library-data-root /library-data --artwork-root /artwork \
  --profile-config-root /profile-configs/player1 \
  --profile-config-root /profile-configs/player2 --output /maintenance/plan.json

# 核对计划，停止全部玩家实例、MCP 和档案创建服务后，使用相同参数添加：
# --apply --workers-stopped
```

工具会备份索引、启动配置、解压缓存标记和布局，再移动目录并更新实际引用；不重新分配游戏 ID，不清空工作目录或存档。备份及事务日志保存在 `library-data/private-migrations/<事务>/`，不要公开发布。它同时生成 `one/.retroone/library-layout.required` 的清单校验值，并在各玩家索引保存绑定快照。以后误删或改写旧绑定时，扫描会报错并保留索引，避免把老游戏误认成新游戏。

成功后使用新版镜像启动服务，核对游戏数、原链接、收藏、历史和存档，再恢复使用。如需回退，先再次停止全部相关服务，使用相同挂载运行：

```sh
python3 scripts/consolidate_library.py \
  --rollback /library-data/private-migrations/<事务> --workers-stopped
```

迁移失败会尝试自动回滚。手动回滚会检查迁移后是否产生新的用户数据或配置修改；发现变化时拒绝覆盖，应保留现场并单独处理。不要在回滚未完成时启动旧版本服务，也不要直接手改清单或校验文件。

新导入的 DOS / Windows 裸目录，应通过 Pegasus 元数据声明入口或明确集合边界；未声明的 `main.exe`、`setup.exe` 不会自动拆成多个游戏。压缩包可直接放在平台或集合目录，仍需在启动配置里确认真正入口。

### 历史三来源整理脚本

`scripts/migrate.py` 保留用于历史收藏整理：它针对同时具有 `new`、`old`、`Pegasus G` 三个原始目录的收藏，输出旧的来源分层结构，**不是新布局迁移工具，也不是任意目录的通用导入器**。已经完成新布局迁移的库不应再次用它导入相同收藏。

```text
rom/
├── new/
├── old/
├── Pegasus G/
└── one/
    ├── library/<平台>/<来源>/<收藏>/
    ├── firmware/
    ├── archive/
    └── .retroone/
```

脚本在支持的同一 Btrfs 文件系统上使用写时复制建立独立文件，保留原目录，不使用会互相影响的硬链接。它保留汉化版、修改版、多碟、街机依赖与元数据，不执行自动删除。

```sh
# 预览分类计划
python3 scripts/migrate.py --root /srv/rom
# 分类复制后逐文件核验，可续传
python3 scripts/migrate.py --root /srv/rom --apply --verify
# 再次独立核验
python3 scripts/migrate.py --root /srv/rom --verify
# 生成小型卡带内容重复报告，不删除任何文件
python3 scripts/duplicate_report.py /srv/rom/one
```

迁移进度在 `one/.retroone/migration-status.json`，逐文件审计在 `migration-audit.sqlite`。核验类型、大小、修改时间和独立 inode，**不等于对全部内容重新计算 SHA-256**。重复报告也只覆盖其支持的小型卡带范围。确认文件、依赖和存档都完整后，再由收藏者决定是否移除原目录。

## 常见排查

| 现象 | 先检查 |
| --- | --- |
| 列表为空 | 是否存在 `one/library/<平台>/` 或其收藏子目录；布局映射是否有效；`RETROONE_ROM_PATH` 是否指向 `one`；运行用户是否可读；点击更新游戏库 |
| 显示待配置 | 详情原因、所用镜像、实际安装核心、固件与完整关联文件；入库不代表运行环境已齐备 |
| 手柄没有反应 | HTTPS 安全上下文、浏览器权限、手柄连接后按任意键唤醒，再检查模拟器内映射 |
| 新增成员一直准备中 | provisioner 日志、Docker socket 组号、绝对挂载路径、网络名和实例健康状态 |
| 设置读取慢 | 对比局域网与反向代理入口的 `GET /p/playerN/api/settings/display` 耗时，不需要启动、停止或调整游戏 |
| 换域名后浏览器进度不见 | 浏览器存储按网站来源隔离；从原来源导出或载入先前服务器状态备份 |

画面设置按玩家、浏览器设备分别保存，在下次新启动云游戏并重新建立串流连接时应用。旧会话、旧连接不会因为点击“保存”或换设备继续游戏而改变。界面同时显示已保存值和当前实际输出，未知值不能当作已经生效。帧率、声音和散热排查见 [性能说明](performance.md)。

每位玩家的原默认值仍在 `display-settings.json`，设备覆盖值存于同目录的 `display-devices.json`，备份时一并保留。浏览器在本地保存随机 UUID，不使用硬件指纹；更换浏览器、域名或清除网站数据会得到另一个设备标识。`GET/PATCH /api/settings/display?device_id=<UUID>` 读写设备设置，PATCH 请求体仍是 `{"stream_resolution":"720p"}` 或 `{"stream_resolution":"1080p"}`；`DELETE` 同地址恢复继承默认，未传设备标识不能删除。省略设备标识的 GET/PATCH 保持旧版玩家默认值接口。每个玩家最多保留 128 份设备设置，满额返回错误，不会自动淘汰其他设备。

## 本地开发

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
ROM_ROOT=/path/to/one CONFIG_ROOT="$PWD/data/dev" \
  .venv/bin/python -m uvicorn retroone.api:app --host 127.0.0.1 --port 18780
.venv/bin/python -m pytest -q tests
```

开发库同样使用 `library/<平台>/` 或 `library/<平台>/<收藏>/`，旧来源分层也兼容。macOS 可开发目录与网页；浏览器播放还需自己的 EmulatorJS 资源，云端需要 Linux 图形、音频和串流环境。API 与组件边界见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## MCP 与观战检查

可选服务的安装与私有配置见 [MCP 指南](mcp.md)。核心检查如下：

- `docker inspect retroone-mcp` 的 health 为 healthy；`http://127.0.0.1:8790/healthz` 返回正常。配置目录只读，state 目录由 `compose.env` 中的 UID/GID 写入，SQLite 位于持久卷。
- 开启 MCP 创建玩家后，验证有创建权限的凭证能发现 `retroone_create_profile`，只读凭证不能；同一请求重试得到相同玩家，另一个 Agent 无法获得该玩家的控制权。`retroone_profile_status` 为 `ready` 后才启动。SQLite 同时保存创建所有权，必须与配置一起备份；准备失败由管理员处理现有成员，不能用反复新增来规避配额。
- 更新设备独立画面设置时须同步部署后端，不能只换网页静态文件。升级后用专用测试设备 UUID 检查 `/api/settings/display?device_id=...` 的 `scope/device_id/default_settings/source`，完成 PATCH→GET→DELETE，确认恢复玩家默认值且其他设备设置不变。
- 对外 `POST /mcp` 未带凭证返回 401。使用授权客户端完成 initialize、list_tools、sessions；只读凭证看不到控制工具，手工调用 stop 仍被拒绝。不要把 token 写在 curl 命令、URL 或日志中。
- 首页与 `app.js` 必须来自同一版完整 `web/` 目录。以自己身份点开家人活动，再点“观看”，确认真实画面持续更新；退出后本人身份、目标游戏 session/PID/分辨率保持不变。
- 独立 AI 测试档案用一个小 ROM 显式 `stream` 启动，检查真实截图、Start/方向/动作的画面效果、释放与重复 action ID。不能拿“输入已提交”代替游戏实际响应。
- 游戏停止或更换后，旧 watch handle 不能继续获取画面；断开观战不能结束目标游戏。使用测试实例检查，不切换正在玩的家庭游戏。

升级 MCP 只重建伴随服务，会使旧 handle/控制租约失效，不需要重启家庭模拟器。升级网页应同步完整静态目录；Nginx 代理配置先 `nginx -t` 再平滑重载。回滚时使用部署前的静态文件与代理备份；停掉 MCP 后，普通游戏库仍可工作。`scripts/create_mcp_runtime.py --rollback` 只恢复它创建且当前空闲的 AI 实例，保留成员与存档，不能作为删除家庭档案的工具。
