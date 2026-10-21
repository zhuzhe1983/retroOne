# MCP 与只读观战

RetroOne 的 MCP 第一版提供真实云游戏截图、有限时长手柄输入和独立 Agent 档案。家庭网页也有独立的“观看”入口。MCP 是可选伴随服务，普通游戏库不依赖它启动。

English: The optional RetroOne MCP server provides authenticated cloud launches, screenshots, bounded controller actions, and read-only observation. It uses the official MCP SDK with Streamable HTTP. Small games can explicitly launch in cloud mode without changing their normal browser default. Native network matchmaking and browser-local screen sharing are not implemented in this MCP release.

## 在网页观看家人

1. 先选择自己的玩家进入游戏库。
2. 点顶栏家人的游戏状态，在弹窗中点击独立的 **观看 ×× 的游戏** 按钮。玩家选择窗口中的头像仍用于切换玩家。
3. 只有对方当前有云端游戏会话时才能观看。对方关闭网页、但云游戏仍运行时，也可以观看；这时不把对方标记为在线。
4. 观看页显示“只读观看”，可以全屏、退出观看。退出不会结束对方游戏，也不会切换自己的玩家、启动另一份游戏或改变对方分辨率。

浏览器本地模拟的画面尚不能分享。不能为观看而自动重启家人的游戏。服务未配置时，不显示一个无法使用的观看入口。

家庭观战沿用应用的**可信家庭网络**边界：玩家名字不是登录凭证。管理员通过 `family_watch.enabled` 和 `family_watch.profiles` 开启共享范围；第一版没有玩家逐人批准或踢出观看者的界面。不要把这理解为已经给整个家庭网站补上账号鉴权。观战代理本身限制为视频、音频和必要信令，拒绝输入与设置修改。

## 安装伴随服务

需要已有可用的 RetroOne Linux 云端运行环境。`Dockerfile.mcp` 只构建 MCP 服务，不含游戏、模拟器、固件、密钥或 Docker socket。配置参考 [mcp-config.example.json](mcp-config.example.json)，示例占位符不能直接使用。

现有部署使用 `retroone` 容器和成员 provisioner 时，可用管理脚本准备 **AI A / AI B** 两个新成员。脚本通过 Docker 管理新实例，因此只应在管理员宿主机执行；MCP 容器本身没有该权限。

```sh
# 先查看计划。image 必须是本地已经验证的完整运行镜像。
python3 scripts/create_mcp_runtime.py --root /srv/retroone/mcp \
  --docker /usr/bin/docker --image retroone:local

# 创建独立成员、存档目录、输入凭证和私有 MCP 配置。
python3 scripts/create_mcp_runtime.py --root /srv/retroone/mcp \
  --docker /usr/bin/docker --image retroone:local --apply
```

实际成员编号由注册表分配，不固定为 player5/6。脚本不重建已有家庭实例。重复执行使用同一私有根目录和原来的镜像 ID，恢复已有部署；不要删除其部署记录后重新执行。

生成文件：

| 文件 | 用途 |
| --- | --- |
| `config/mcp.json` | 档案上游、客户端 token 的 SHA256、每个客户端的权限范围；挂载只读 |
| `private/client-tokens.json` | 给 MCP 客户端使用的 Bearer token；不能提交 Git 或放到网页 |
| `private/deployment.json` 等 | 新实例的部署与恢复资料；保留并限制访问 |
| `state/requests.sqlite` | 操作幂等记录；必须持久化、可写 |
| `compose.env` | 配置路径和生成文件的 UID/GID；启动时必须加载 |

在生成的 `compose.env` 中补充自己的访问域名：

```dotenv
RETROONE_MCP_ALLOWED_HOSTS=localhost,127.0.0.1,retroone-mcp,retro.example.com
RETROONE_MCP_ALLOWED_ORIGINS=https://retro.example.com
```

`ALLOWED_HOSTS` 填主机名，不含协议；`ALLOWED_ORIGINS` 填完整 Origin，非默认端口也要写上。按实际网络填写 `RETROONE_DOCKER_NETWORK`。默认直接监听宿主 `127.0.0.1:8790`。

```sh
docker compose --env-file /srv/retroone/mcp/compose.env \
  -f compose.mcp.yaml up -d --build
curl --fail http://127.0.0.1:8790/healthz
```

在主服务配置 `RETROONE_MCP_UPSTREAM=retroone-mcp:8090`，由它生成 `/mcp` 和 `/spectate/` 的代理路由。先保存并结束需要升级的游戏，再按正常流程更新主服务；已有活跃游戏的维护者也可以验证配置后仅平滑重载 Nginx。修改环境变量本身不会自动更新已运行的代理。

私有配置的 `family_watch` 默认关闭。要在家庭网页启用，设置 `enabled: true` 并明确列出允许观看的 profile ID。客户端授权配置每次请求读取；撤销 token 可设 `enabled: false`。MCP 对某个家庭档案拥有观察权限，不等于允许控制它。

## 连接 Agent

在支持 **Streamable HTTP** 的 MCP 客户端中填写：

```text
URL: https://retro.example.com/mcp
Header: Authorization: Bearer <分配给这个 Agent 的私有 token>
```

不要把 token 放进 URL。服务没有 OAuth 登录流程；客户端需要支持自定义 Authorization header。官方 Python MCP 2.3.0 客户端和 1.30.0 兼容客户端均纳入协议测试。

将 [RetroOne Agent Skill](skills/retroone-agent/SKILL.md) 放入客户端支持的技能目录，例如 `~/.codex/skills/retroone-agent/SKILL.md`。Skill 只提供使用规则；连接和凭证仍在 MCP 客户端配置中设置。先让 Agent 列出工具、读取 capabilities，再选择它真正获授权的档案。

### 服务器、专属玩家与运行模式

`https://retro.mess.host` **仅用于简单连通性测试，不允许进行真实游戏**。只做基本的只读 HTTP 或健康检查，不通过该地址启动、继续或操作游戏、写入存档、创建玩家，也不把别名或 IP 当作绕过限制的方法。云游戏和浏览器游戏都适用；实际游玩使用另外明确授权的部署地址。

Agent 尽量使用自己创建的专属玩家，也可以使用管理员明确分配的 Agent 玩家。不要因为某位家人暂时空闲就使用其档案，更不能占用别人正在进行游戏的玩家。除非用户明确要求使用某个档案，否则不要在别人的档案中保存进度。

已提供 **`retroone_create_profile`** 和 **`retroone_profile_status`**。有创建权限的 Agent 提交昵称、头像和稳定的 `request_id`，即可通过现有成员准备队列创建自己的玩家；不会启动游戏。新玩家的存档目录独立，MCP 持久记录创建者，并只按管理员配置向创建者授予权限。网页的 **+** 按钮仍可使用，但网页创建的玩家不会自动归某个 MCP Agent 所有。

```json
{"name": "探索者 AI", "avatar": "rocket", "request_id": "my-agent-player-1"}
```

用上述参数调用 `retroone_create_profile`，返回 `profile.id`、`profile.status`、`permissions`。状态为 `provisioning` 时，调用 `retroone_profile_status({"profile_id": "返回的 ID"})` 等待 `ready`，再读取 capabilities 并启动游戏。头像可选 `pixel / leaf / star / rocket / cat`；昵称为 1–20 个字符。超时、断连或 MCP 重启后，用**同一个 request_id 和相同参数**重试；后台沿用同一个 UUID，不会重复创建。状态为 `error` 时联系管理员处理已有玩家，不要不断换请求编号创建新档案。

### 开启 Agent 创建权限

默认关闭。管理员在 MCP 私有配置中增加：

```json
{
  "profile_creation": {
    "enabled": true,
    "api_url": "http://retroone:3000",
    "allow_legacy_input": true,
    "share_with_family": false
  }
}
```

`api_url` 是管理员指定的家庭网关 Origin；Agent 不能传入 URL、容器参数、玩家 ID 或权限。对应凭证增加 `create_profiles: true`、`max_created_profiles: 2` 和 `created_profile_permissions: ["observe", "control", "launch", "stop"]`。每个凭证默认最多创建 1 个玩家，可配置 1–8 个，未确认的创建也占配额；家庭总人数仍受主服务限制。新权限立即生效，MCP 配置继续只读，服务不需要 Docker socket。

新玩家复用家庭 provisioner 的**可信局域网运行时**。`allow_legacy_input: true` 是管理员对这些独立新玩家启用单手柄槽 1 的显式选择；它不会修改现有家庭玩家或已有 AI 安全实例，也不把它们的权限授予创建者。若保留 `false`，新玩家只可获得观察权限，不能通过 MCP 启动、控制或停止。新增玩家的普通网页入口仍遵循家庭应用现有信任边界，不等于新增了网页账号认证。已有带 Selkies slot token 的 AI 实例继续使用原凭证。

`share_with_family: true` 可将这些新玩家加入已经启用的家庭只读观战范围；不授予家人 MCP 控制权。撤销后续创建可设置凭证 `create_profiles: false`；撤销已创建档案的操作权限则修改 `created_profile_permissions` 或禁用凭证。`state/requests.sqlite` 同时保存所有权和创建幂等记录，升级时必须保留并备份。更换网关 Origin 不会把旧档案编号自动绑定到另一台服务器。全局关闭 `profile_creation.enabled` 也会关闭动态档案授权。

- **需要家人、直播导演或其他 Agent 观看**：使用 `mode: "stream"`，NES 等小游戏也一样。当前观战只支持云端画面。
- **只做私密测试或打榜尝试**：支持浏览器模拟器的游戏可以通过网页选择浏览器模式，用自己的玩家和独立浏览器环境操作。浏览器截图和输入由浏览器自动化完成，不能调用云端 MCP 的 `observe` / `act` 控制本地模拟器；也不能声称网页打榜记录已经由 MCP 验证或提交。
- 浏览器存档属于对应浏览器环境，临时环境销毁前应通过受支持的功能导出所需存档。后续改为云端观看时，不保证能直接沿用浏览器存档，不自动重启或迁移游戏。

## 工具与操作闭环

所有工具带 `retroone_` 前缀。只读 token 只发现读取工具；创建和游戏操作工具按凭证授权显示，即使手工调用未授权名字也会被拒绝。

| 工具 | 作用 |
| --- | --- |
| `capabilities` | 当前凭证的档案、权限、核心就绪状态、输入限制 |
| `list_games` | 按 `profile_id`、`q`、`platform` 搜索真实目录 |
| `sessions` | 获授权档案当前的云游戏会话 |
| `watch` | 用 `profile_id`、`expected_session_id` 取得只读 handle |
| `observe` | handle → PNG 图像、observation ID、采集时间和动作序号 |
| `profile_status` | 查询自己创建的玩家准备状态，不启动游戏 |
| `create_profile` | 提交 `name`、`avatar`、`request_id` 创建专属玩家；要求创建权限和剩余配额 |
| `launch` | `profile_id`、`game_id`、`request_id`、`mode: "stream"` 启动 |
| `claim_control` | 对准确会话申请一个获授权的 `slot`，返回 lease ID |
| `act` | 持有租约，以新截图为依据，按键/摇杆操作 1–1000 ms |
| `release_control` | 释放自己的控制租约；不结束游戏 |
| `stop` | 带准确会话 ID 和 request ID，结束获授权的游戏 |

通过 MCP 游玩或需要观战时，NES 等小 ROM 也使用 `mode: "stream"`，仅作用于这一次启动，不修改日常浏览器模式。MCP 的 `auto` 目前也解析为云端；`browser` 会明确拒绝，因为服务端看不到本地浏览器画面。需要缺失的核心时返回错误。上面允许的浏览器私密测试走网页操作，不是这个 MCP 启动接口。

控制顺序为 `launch → claim_control → observe → act → observe`。`act` 参数：

```json
{
  "lease_id": "<claim_control 返回值>",
  "expected_session_id": "<当前会话>",
  "action_id": "<本次动作的唯一 ID>",
  "observation_id": "<刚取得的截图 ID>",
  "buttons": ["right", "a"],
  "duration_ms": 150
}
```

按钮支持 `a/b/x/y/lb/rb/lt/rt/back/start/l3/r3/up/down/left/right`，摇杆为 `axes: {"lx": 0.5, "ly": 0.0}`，也有 `rx/ry`。以客户端发现的 schema 为准。截图有效期 30 秒；一次输入尝试后需要重新截图，包括失败或取消的尝试。租约 90 秒，后续有效操作可续期。handle 有效期一小时，会话替换会立即使旧授权失效。

同一 `request_id` 或 `action_id` 重试返回已有结果，不再次输入；换一个 ID 会被当作新动作。中途重启留下的未确认操作不会自动重放。服务重启后临时 handle、租约失效，需要重新获取。

## 当前边界

- 输入在真实时间执行，不是逐帧暂停/步进。输入提交成功不等于游戏响应成功；Agent 必须查看操作后的画面。
- 每次动作都会归零并关闭输入连接。上游按来源地址限制重连，适配器预留约 550 ms 间隔。网络完全失联时另有约 2.1 秒的上游心跳释放兜底；不是零延迟硬实时控制。
- 每个 Agent 可用独立成员和存档。默认只授权一个手柄槽；SameConsole 多槽需管理员授权并逐游戏验证，尚不能宣称双 Agent 已在同一局对战。
- 本版没有原生 Network 建房/加入工具、存档快照工具、直播弹幕连接器，也没有浏览器本地画面发布功能。后续方案见 [AI Agent 设计记录](ai-agents.md)。
- 观看视频与音频复用 Selkies；MCP `observe` 返回静态截图，不返回声音。语音、游戏速度和实际帧率需要独立测量。

诊断与升级检查见 [维护指南](operations.md#mcp-与观战检查)，镜像与 CI 见 [发布说明](releasing.md#mcp-伴随镜像)。
