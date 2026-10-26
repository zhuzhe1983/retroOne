# 构建与发布

RetroOne 提供公开入口镜像和私有完整镜像两种构建方式。GitHub Actions 只构建、发布公开入口镜像；ROM、BIOS、固件、密钥、独立封面缓存、存档，以及本地缓存的模拟器二进制均不进入仓库或 Release。源码文档可以包含逐张审核的界面截图，其中的真实游戏封面仅用于展示界面；来源与文件校验值见 [截图说明](screenshots/README.md)。这些截图不进入公开镜像的构建上下文。

## 公开入口镜像

从干净源码即可构建，目标架构为 `linux/amd64`：

```sh
docker build --platform linux/amd64 -f Dockerfile.public -t retroone:local .
python3 scripts/smoke_public_image.py retroone:local
```

该镜像包含 RetroOne 网页、目录 API、成员管理、Selkies 串流基础环境，以及 Debian 软件源中的 RetroArch、DOSBox、Dolphin、MAME、Wine 等软件包。它不包含本地审查缓存中的 Libretro 核心、EmulatorJS 运行文件、PCSX2、SDLPAL 或 Eden AppImage。缺少对应运行环境的平台会显示不可用，浏览器模拟也不会被标记为可用；这不是已经包含所有模拟器的完整游戏镜像。

`Dockerfile.public.dockerignore` 使用构建上下文白名单。即使开发目录里存在 `vendor/`、游戏目录和测试截图，也不会传入公开镜像构建。镜像保留 Debian 包的许可证文档及 `/opt/retroone/runtime-packages.tsv`，用于查看已安装包版本。Selkies 基础镜像按摘要固定；APT 软件仓库仍会更新，因此这里不承诺跨时间逐字节重建相同镜像。

部署路径、GPU 权限与 ROM 导入步骤见 [部署与维护](operations.md)。`compose.yaml` 中所有玩家容器和新成员创建器共用 `RETROONE_IMAGE`；默认值为 `retroone:local`。完成首次发布后，可在 `.env` 中指定：

```dotenv
RETROONE_IMAGE=ghcr.io/zhuzhe1983/retroone:v0.1.0
```

发布页会同时给出不可变摘要。需要锁定部署版本时，用 `ghcr.io/zhuzhe1983/retroone@sha256:...` 替代版本标签。请勿在玩家进行游戏时直接更新运行中的容器。

## 完整运行环境仅在本地准备

现有 `Dockerfile` 保留完整本地构建方式，Switch 使用的固定 Eden 来源及构建步骤见 [Switch 运行环境](switch-runtime.md)。需要 Python 3.11、curl 及 Docker；只有恢复旧版 EmulatorJS 7z 归档时才额外需要 7-Zip（`7z` 或 `7zz`）。先准备运行文件，再构建：

```sh
python3 scripts/fetch_runtime.py
docker build --platform linux/amd64 -f Dockerfile -t retroone:local .
```

准备脚本分别读取 `container/runtime-lock.json`（原生模拟器）和 `container/browser-runtime-lock.json`（浏览器运行包），逐个校验大小及 SHA256，正常运行不会改写锁文件。浏览器包默认是官方 **4.3.0-pre 预发布版**，完整锁定 290 个文件；直接准备这一版本，不先下载 4.2.3 再覆盖。上游该目录的 `version.json` 仍写着 4.2.3，而 `src/consts.js` 和实际运行实例标识为 4.3.0-pre；项目保留原始字节，不修改上游版本文件。官方版本目录也可能变化，哈希不符时会停止。

部分历史原生核心来自上游可变的 `/latest/` 地址；如果上游文件已经更新，首次下载会因哈希不符失败。此时应恢复对应的已审查私有缓存，或单独审查、测试新的上游版本后更新锁文件；不要删除哈希检查，也不要把“最新版”自动写回锁文件。公开 CI 不依赖这些下载地址。

已有缓存可在离线模式下校验、解包：

```sh
python3 scripts/fetch_runtime.py --verify-only
python3 scripts/fetch_runtime.py --offline
```

只更新浏览器环境时加 `--browser-only`，不读取或修改原生核心。已有完整官方 `data/` 目录可作为离线来源；只复制锁文件列出的资源，额外文件不会进入安装树：

```sh
python3 scripts/fetch_runtime.py --browser-only
python3 scripts/fetch_runtime.py --browser-only --offline \
  --browser-source /private/emulatorjs-4.3.0-pre/data
python3 scripts/fetch_runtime.py --browser-only --verify-only
```

下载最多并发 4 个文件，临时文件通过校验后才进入缓存；缓存位于 `vendor/runtime-downloads/browser/<锁文件SHA256>/`，中断后可继续。安装先构建、校验完整新树，再替换 `vendor/emulatorjs`，不会把新旧核心合并。旧目录完整保留在命令输出的 `browser_activation.backup` 路径，通常为 `vendor/runtime-backups/emulatorjs-<标识>`。切换过程有独占锁和恢复记录；若异常中断导致活动目录缺失，再次运行会先恢复旧目录。新包的 `data/.retroone-runtime.json` 记录版本和锁文件哈希，供服务区分浏览器缓存版本。

准备脚本只管理本地构建材料，不会更新正在运行的容器。回滚时应先等玩家退出，保留当前 `vendor/emulatorjs` 作为另一份备份，再将输出记录的旧目录移回原位，并重建、部署对应版本；不要在活跃播放器使用的挂载目录内直接换包。

为了在自己的机器间恢复已验证版本，可生成只包含锁定原生归档、浏览器资源及两份锁文件的本地备份。此备份不会收集 ROM 或用户配置，**仍仅用于私有存储，不要上传到 GitHub Release 或公共镜像仓库**：

```sh
python3 scripts/fetch_runtime.py --verify-only --create-bundle /private/backups/retroone-runtime.tar.gz
python3 scripts/fetch_runtime.py --offline --from-bundle /private/backups/retroone-runtime.tar.gz
```

备份旁会生成 `.sha256` 文件；恢复时还会逐项检查锁文件一致性、成员名称和内容哈希。制作浏览器专用备份时加 `--browser-only`，恢复时也必须使用同一参数；`--native-only` 同理。历史备份需要当时对应的锁文件：使用 `--native-only --lock /private/old-runtime-lock.json` 恢复旧归档，不会将旧 4.2 包误认成新的完整运行包。`--vendor-root /absolute/path` 可用于在独立目录验证恢复结果。完整镜像随后从 `vendor/` 复制当前运行文件，历史备份和下载缓存不进入构建上下文，**你的 ROM、固件、密钥或存档也不会进入镜像**；实际游戏内容仍通过只读 `/roms` 挂载提供。

完整核心集合的许可并不统一。例如 [Snes9x 的许可证](https://github.com/snes9xgit/snes9x/blob/master/LICENSE) 限定非商业使用和分发，[Genesis Plus GX 的许可证](https://github.com/libretro/Genesis-Plus-GX/blob/master/LICENSE.txt) 也对商业用途、修改版本源码及声明保留提出条件。因此项目不把整套缓存视为可任意再分发的统一软件包；以后若要公开某个运行核心，应逐项核对对应版本的许可证、源码与声明要求。本项目源码的许可选择也不代表第三方组件许可。

## GitHub Actions

工作流为 [release.yml](../.github/workflows/release.yml)：

- Pull Request 和 `main` 提交：检查公开文件边界、截图内容哈希、运行锁格式、Compose 配置、JavaScript 语法及 Python 测试；随后构建公开镜像，并在没有 GPU、ROM、固件或宿主存档挂载的空容器内检查真实 API 和网页启动。不会推送镜像。
- `v数字.数字.数字` 标签（可带预发布后缀）：完成同样检查后，把**刚刚通过启动测试的同一个镜像**推送到 `ghcr.io/<仓库所有者>/<仓库名小写>:<标签>`，再创建 GitHub Release。Release 仅包含 GitHub 自动生成的源码包、说明及镜像摘要，不上传本地运行缓存。
- 手动执行：默认只验证。显式勾选 `publish` 后，推送 `sha-<提交前12位>` 标签；手动分支构建不创建版本 Release。

例如维护者确认源码和文档后，可发布一个版本：

```sh
git tag -a v0.1.0 -m 'RetroOne v0.1.0'
git push origin v0.1.0
```

工作流只使用仓库内置的 `GITHUB_TOKEN`，不需要另存个人访问令牌。检查任务仅有源码读取权限；镜像任务增加 packages 写入权限；创建 Release 的任务仅增加 contents 写入权限。Action 均固定为完整提交 SHA，对应官方标签及核验日期记录在 [actions-pins.json](../.github/actions-pins.json)。若仓库或组织禁用了 Packages 写入，需要在对应 GitHub 设置中启用允许的发布权限。

本地预检：

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check_public_source.py
python3 -m pytest -q tests
docker compose config --quiet
node --check web/app.js
```

公开文件检查使用 Git 已跟踪文件列表；待发布文件需要先纳入 Git 暂存区才能接受审计。文档截图只允许清单中逐张审核的界面图片，并校验 SHA256。此检查用于防止误带文件，维护者仍需人工确认截图来源、展示范围与隐私处理；文件校验本身不能代替内容审核。

## MCP 伴随镜像

`Dockerfile.mcp` 使用独立的 `requirements-mcp.txt`，不与模拟器 API 的依赖环境混装。构建上下文采用独立允许列表，只包含服务代码和依赖声明；不包含 ROM、运行缓存、私人凭证或家庭存档。

```sh
docker build -f Dockerfile.mcp -t retroone-mcp:local .
docker compose --env-file /srv/retroone/mcp/compose.env -f compose.mcp.yaml up -d --no-build
```

GitHub Actions 的 `mcp` 任务检查鉴权、幂等输入、只读代理，以及官方 MCP 2.3.0 / 1.30.0 客户端兼容性，然后构建伴随镜像。`main` 和 PR 只测试、构建；版本标签或手动 `publish` 会推送 `ghcr.io/<owner>/<repo小写>-mcp:<版本>`，RetroOne 仓库对应 `ghcr.io/zhuzhe1983/retroone-mcp`。发布任务成功前，不应把尚未推送的标签写成可下载镜像。

主镜像构建等待基础检查和 MCP 检查通过。伴随服务的私有配置始终由部署者单独提供，使用说明与 Skill 随源码发布：[MCP 指南](mcp.md)、[RetroOne Agent Skill](skills/retroone-agent/SKILL.md)。
