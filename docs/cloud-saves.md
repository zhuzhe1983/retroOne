# PSP 游戏内云存档

RetroOne 为同一玩家、同一游戏同步 PPSSPP 的 **游戏内存档**。它与工具栏的 `.state` 即时存档是两个功能；不会把模拟器内存快照转换成游戏内存档，也不会同步 ROM、密钥、固件、DLC 或着色器缓存。

当前支持边界是原生 libretro PPSSPP 与本项目锁定的 EmulatorJS 4.3.0-pre PPSSPP（兼容桥也验证过旧版 4.2.3）。其他平台返回 `supported: false`，继续使用原有启动流程。HTTP 与 P2P 只决定 ROM 的取得方式，不影响存档同步。

## 使用流程

1. 在云游戏里正常进行游戏内保存，然后结束该云会话。
2. 启动同一玩家、同一游戏的浏览器模式。核心启动前，RetroOne 读取云端最新进度并放入浏览器的独立存档空间。
3. 在游戏中正常保存。浏览器每 3 秒检查目录变化，至少两次一致且没有已打开的可写存档文件时自动上传；一般在保存后约 3–6 秒完成。游玩菜单中的状态显示“云存档已同步”后，再切换设备；正常同步完成时不常驻遮挡游戏画面。
4. 浏览器正常退出会等待上传并释放租约。云端下一次启动直接使用更新后的原生目录。

不能保证网络中断或浏览器突然被终止前的最后一次写入已经到达服务器。待上传文件与同步基线保留在游戏独立的 IndexedDB 中；重新打开时继续上传，若另一台设备已经产生不同进度，则停止自动覆盖并提示冲突。清除浏览器站点数据会删除尚未上传的本地内容。

## 冲突与旧数据

同一玩家、同一游戏只允许一个浏览器写租约；租约有效期 90 秒，每 20 秒续期。云端运行该游戏时拒绝浏览器同步；浏览器租约有效时拒绝启动该游戏的新云会话。其他玩家、其他游戏的存档不受影响。

冲突时两边都保留。可以导出本地 JSON 备份；明确选择“用云端进度重新启动”后，脚本先在本地数据库保存完整冲突备份，再重新启动，避免把新存档热替换到旧游戏的内存状态中。当前不提供通用备份导入工具；不要手工把这些 JSON 当成 `.state` 文件。

旧版浏览器 PSP 使用按玩家共用的 `/data/saves` 数据库，无法只凭目录可靠地认定每份进度属于哪个 RetroOne 游戏。检测到旧数据时会暂停启动，要求明确选择“保留旧数据库，使用云存档”。旧库不会被删除，也不会混入当前游戏。新的 PSP 游戏内数据库按玩家和游戏 ID 隔离；既有 `EmulatorJS-states` 数据库与 `EJS_gameName` 保持原规则。

## 服务端存放位置

所有路径都相对于该玩家自己的 `CONFIG_ROOT`：

- 原生目录：`saves/<game_id>/PPSSPP/PSP/SAVEDATA/`。
- 版本与租约：`cloud-saves/games.sqlite3`。
- 每个游戏保留当前版本和最近 15 个旧版本。版本数据来自游戏内存档完整文件树，服务器不根据文件时间盲目覆盖。

SQLite 事务记录版本与待落盘标记；原生目录先完整暂存，再通过目录重命名替换。若进程中断，下次同步或云启动会根据已提交版本恢复落盘。备份与目录恢复仅在该玩家、该游戏的固定路径下执行。若恰在目录切换时崩溃，带唯一后缀的 `SAVEDATA.previous-*` 原目录会保留供检查，恢复不会删除这些中断副本。

## API

均使用当前玩家前缀 `/p/<player>/api/games/<game_id>`；请求不能指定另一玩家目录。

| 路由 | 用途 |
| --- | --- |
| `GET /cloud-save` | 支持情况、最近已记录版本、是否有云会话/浏览器租约；不导出文件 |
| `POST /cloud-save/lease` `{client_id: UUID}` | 获取或续接本浏览器租约，返回最新 `revision`、`digest`、`files`、`lease_id`、到期时间 |
| `POST /cloud-save/heartbeat` `{lease_id: UUID}` | 延长有效租约 |
| `PUT /cloud-save` `{lease_id,base_revision,files}` | 乐观版本比较后保存完整快照；丢失响应后的同内容重试可幂等成功 |
| `POST /cloud-save/release` `{lease_id: UUID}` | 释放匹配租约，不停止游戏 |

`files` 为 `[{path: 'SAVE_SLOT/DATA.BIN', data: '<base64>'}]`。路径相对于 `SAVEDATA`，不能包含绝对路径、反斜杠、`..`、链接、即时存档或密钥扩展名。最多 256 个文件、32 MiB 解码内容、4 层相对路径；请求体上限 45 MiB。不接收 ZIP，因此没有 ZIP 解压或压缩炸弹路径。

错误包括 `native_active`、`lease_busy`、`browser_active`、`lease_expired`、`revision_conflict`、`invalid_snapshot`。返回 `detail: {code,message}`；冲突为 409。存档响应均禁止缓存。

播放器提供 `window.RetroCloudSaveSync.prepareExit()`，返回 `{safeToExit,synced,reason?}`。失败时调用方应保留播放器，明确告知用户本地待上传进度仍在。另有 `flush()`、`retry()`、`status()`；同源父页面通过 `retroone:cloud-save` 消息接收 `gameId`、`profileId`、`status`、`message`。状态包括 `starting/syncing/synced/conflict/offline/busy/legacy/unsupported`。

## 实现依据与验证范围

[EmulatorJS GameManager](https://github.com/EmulatorJS/EmulatorJS/blob/main/data/src/GameManager.js) 在核心启动前挂载可写 IDBFS；RetroOne 兼容 4.2.3 的 `mountFileSystems()` 与 4.3 的 ESM `initializeGameManager()`，在真正运行核心前等待恢复完成，不依赖不会等待异步处理的事件回调。浏览器的实际路径为 `/data/saves/PPSSPP/PSP/SAVEDATA`；启动前明确固定 RetroArch 的存档根目录与按核心分目录规则。只同步这份游戏内存档，不同步 `PPSSPP_STATE`。[Libretro 的 PPSSPP 文档](https://docs.libretro.com/library/ppsspp/#directories)明确区分游戏内 `SAVEDATA` 与即时状态文件。原生 Runner 启用按核心保存，因此包含 `PPSSPP` 这一目录层。

已用合成数据验证真实 PPSSPP WASM 的内存文件系统、断网待上传恢复、退出竞态和冲突保护。另以用户自有《空之轨迹 FC》及存档的隔离副本验证完整流程：干净浏览器自动读取两份云端存档、进入原场景、通过游戏内 NEW DATA 新建第三槽、自动回传到隔离服务器、正常退出释放租约。原始存档未改动，测试副本不随代码发布。

真实游戏验收发现并修正了早期合成测试没有覆盖的核心分目录行为：缺少 `PPSSPP` 前缀会使文件虽已同步，游戏仍显示没有存档。旧错误目录中的本地副本会保留；两处内容不同则要求明确选择，不静默覆盖。这次验收证明该版本《空之轨迹 FC》的读写链路，不代表所有 PSP 游戏和区域版本都已验证。
