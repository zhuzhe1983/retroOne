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

现有 `Dockerfile` 保留完整本地构建方式，Switch 使用的固定 Eden 来源及构建步骤见 [Switch 运行环境](switch-runtime.md)。需要 Python 3.11、curl、7-Zip（命令为 `7z` 或 `7zz`）及 Docker；先准备运行文件，再构建：

```sh
python3 scripts/fetch_runtime.py
docker build --platform linux/amd64 -f Dockerfile -t retroone:local .
```

准备脚本读取 `container/runtime-lock.json`，逐个校验下载归档和核心二进制的 SHA256，正常运行不会改写锁文件。部分历史核心来自上游可变的 `/latest/` 地址；如果上游文件已经更新，首次下载会因哈希不符失败。此时应恢复对应的已审查私有缓存，或单独审查、测试新的上游版本后更新锁文件；不要删除哈希检查，也不要把“最新版”自动写回锁文件。公开 CI 不依赖这些可变地址。

已有缓存可在离线模式下校验、解包：

```sh
python3 scripts/fetch_runtime.py --verify-only
python3 scripts/fetch_runtime.py --offline
```

为了在自己的机器间恢复已验证版本，可生成只包含锁定运行归档的本地备份。此备份不会收集 ROM 或用户配置，**仍仅用于私有存储，不要上传到 GitHub Release 或公共镜像仓库**：

```sh
python3 scripts/fetch_runtime.py --verify-only --create-bundle /private/backups/retroone-runtime.tar.gz
python3 scripts/fetch_runtime.py --offline --from-bundle /private/backups/retroone-runtime.tar.gz
```

备份旁会生成 `.sha256` 文件；恢复时还会逐项检查锁文件一致性、成员名称和归档内容哈希。`--vendor-root /absolute/path` 可用于在独立目录验证恢复结果。完整镜像随后从 `vendor/` 复制这些运行文件，**不会复制你的 ROM、固件、密钥或存档**；实际游戏内容仍通过只读 `/roms` 挂载提供。

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
python3 -m pytest -q
docker compose config --quiet
node --check web/app.js
```

公开文件检查使用 Git 已跟踪文件列表；待发布文件需要先纳入 Git 暂存区才能接受审计。文档截图只允许清单中逐张审核的界面图片，并校验 SHA256。此检查用于防止误带文件，维护者仍需人工确认截图来源、展示范围与隐私处理；文件校验本身不能代替内容审核。
