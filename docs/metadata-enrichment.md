# 游戏资料补全与发现排序

`scripts/enrich_store_metadata.py` 生成供审核的资料批次，不修改 ROM、启动配置、存档或线上数据库。生成的 `[{"id": "…", "details": {…}}]` 可交给 `GameMetadataStore.import_details_many()`，由共享资料库分发到玩家的索引。

## 来源和身份匹配

- 本地已有 Nintendo eShop 资料快照：只读取发行日期、发行商、开发商、类型、人数、语言、简介和官方截图地址；不复制密钥、票据、容量或整个源对象。来源标记为“本地资料快照”，不能当作刚验证的最新商店信息。
- Switch 使用容器检查报告中唯一的本体 Title ID 对应资料。含多个本体、只有更新/DLC 或检查失败的条目不自动推断身份。
- 最新官方商店页：同时核对产品 `applicationId` 和 `softwareDetails.rights[].titleId`，避免把页面中的推荐游戏资料误用到当前游戏。只保留结构化事实和官方媒体链接，不复制整篇商店宣传文。
- 官方发行日期、语言和人数描述的是该地区商店里的原版游戏；它们不能证明 ROM 的地区、汉化内容或模拟器的联机兼容性。
- 简介使用原始 `intro`，缺失时取 `description` 的首个正文段落。优先已有中文，其次英文；转换为纯文本、去掉脚本和样式，只保留前两句、最多约 700 字符。它是注明来源的简短摘录，不是生成的评价，也不填补来源里不存在的内容。独立保存在 `details.description`，不覆盖 ROM 的技术说明。

## 热门排序的含义

可读取以下一手资料：

- [Nintendo Switch](https://www.nintendo.co.jp/ir/en/finance/software/switch.html)、[Nintendo 3DS](https://www.nintendo.co.jp/ir/en/finance/software/3ds.html) 官方累计销量。
- Nintendo 保留的 [DS](https://www.nintendo.co.jp/ir/en/sale/software/ds.html)、[Wii](https://www.nintendo.co.jp/ir/en/sale/software/wii.html) 历史榜单。页面统计日期可能较旧，不能称为当前销量。
- [Nintendo 2007 财年](https://www.nintendo.co.jp/ir/pdf/2007/070427e.pdf)、[2008 财年](https://www.nintendo.co.jp/ir/pdf/2008/080425e.pdf) 财报的百万销量表，使用最后一列累计销量；该列单位是万份，并非年度销量。支持 GBA、DS、GameCube、Wii。
- [Capcom 白金作品](https://www.capcom.co.jp/ir/english/business/million.html)。只读取单一平台的作品统计；多个平台合计、多个版本合计不会分配给其中一个版本。

每条记录保留实际份数、统计日期、来源链接和 `official_lifetime_sales` 类型。排序值为“本游戏销量 ÷ 已加载来源中同平台的最高销量 × 100”。同一平台的任天堂和 Capcom 数据共用一个分母；同一作品在多个日期出现时优先采用较新的统计。

这是历史销量参考，并非实时热度、媒体评分或玩家口碑，也不能直接比较不同平台的市场规模。官方榜单往往只列出少数头部作品；没有数据的游戏保持未知，不赋假分，不把家庭游玩次数冒充大众热度。家庭游玩次数应使用独立排序项。

匹配必须同时符合平台和完整游戏名，不能使用模糊包含。自动匹配仅可忽略已知地区、语言、修订号等零售标签，不能删掉续作序号、特别版、复刻版或 MOD 标记。原版销量是同平台原作的全球统计，不代表某个地区 ROM 单独的销量。

必要时可提供经过人工核对的 `game_id → {platform, title, evidence}` 映射，`evidence` 记录身份核验依据。仅做汉化或翻译、且原作身份明确的版本，可以显式设置 `original_title_reference: true`；此时类型为 `original_title_sales_reference`，显示“原作官方销量参考（非当前版本独立销量）”。不自动给汉化文件继承原作统计。大幅 MOD、改变玩法、测试版、合集和跨平台复刻仍不匹配，即使手工映射也不会放行。

商业合辑本身若有独立销量，应以该合辑自己的身份另建资料，不能套入其中一部原作的数据。没有官方资料的平台仍保留未知；同系列的现代作品销量、掌机销量、系列总销量不能补作旧游戏销量。

## 离线生成

```sh
python scripts/enrich_store_metadata.py \
  --catalog /private/catalog.json \
  --identity-audit /private/header-audit.json \
  --title-databases /private/titledb-sanitized.json \
  --cache-dir /private/store-cache \
  --source-plan /private/source-plan.json \
  --output /private/details-batch.json
```

`catalog.json` 是游戏数组或含 `games` 数组的对象；身份报告的每条记录包含 `id`、`header_valid`、`meta_title_ids`。`title-databases` 是以 `US.en.json`、`JP.ja.json`、`HK.zh.json`、`titles.json` 为键的对象，每个值为资料数组或原始字典。建议先在资料所在机器上按白名单导出，避免转移完整数据库。

可选来源计划示例：

```json
{
  "official_pages": [{
    "cache_name": "odyssey",
    "title_id": "0100000000010000",
    "url": "https://www.nintendo.com/us/store/products/super-mario-odyssey-switch/"
  }],
  "sales_pages": [{
    "cache_name": "switch-sales",
    "platform": "switch",
    "url": "https://www.nintendo.co.jp/ir/en/finance/software/switch.html"
  }, {
    "cache_name": "capcom-platinum",
    "format": "capcom_platinum",
    "url": "https://www.capcom.co.jp/ir/english/business/million.html"
  }, {
    "cache_name": "nintendo2008",
    "format": "nintendo_financial_text",
    "url": "https://www.nintendo.co.jp/ir/pdf/2008/080425e.pdf"
  }]
}
```

默认仅使用缓存；显式添加 `--fetch` 才联网。每批最多 32 页、每页最多 8 MiB、每次请求 20 秒。只访问 Nintendo / Capcom 官方 HTTPS 域名，重定向也必须满足此限制；TLS 验证始终开启，必要时用 `--ca-file` 指定可信证书包。HTML 缓存名为 `<cache_name>.html`；财报使用 `<cache_name>.txt`，联网时需安装 Poppler 的 `pdftotext`，以 `-layout` 提取表格。页面结构变化或身份不匹配会写入旁边的 `.report.json`，不会伪造缺失字段。

报告包含各平台覆盖量，以及逐条游戏身份、对应原作、来源 URL 和匹配依据，方便在导入前审核。手工映射通过 `--aliases /private/reviewed-aliases.json` 传入。ROM 头部的产品代码只能佐证原作身份；确认文件未修改还需比对完整内容哈希。哈希不匹配时不得把“产品代码相同”写成“已验证原版”。

## 截图与公开仓库

官方媒体链接也可能因地区、过期或跨域策略无法显示。网页启用 COEP 时，有些旧 eShop CDN 没有 CORS/CORP 响应头；RetroOne 已通过受限的同源截图缓存提供这些图片，校验官方域名、阻止私网目标并限制单图大小、总缓存量与并发。保留实际取得的来源 URL，不把旧 URL 批量改成尚未验证的其他 CDN 地址。不要把封面错误当成游戏不可运行。

如果网络网关启用了 Fake-IP DNS，官方域名可能被解析到 `198.18.0.0/15`。默认 `system` 模式仍会拒绝这些保留地址。可为主服务和每个玩家服务显式配置：

```yaml
environment:
  RETROONE_SCREENSHOT_DNS: cloudflare
```

可选值为 `system`（默认）、`cloudflare`、`google`。随附的 `compose.yaml` 已把此变量传给主服务、玩家 2 和 provisioner；在部署目录的 `.env` 设置 `RETROONE_SCREENSHOT_DNS=cloudflare` 后重建相应服务即可。provisioner 创建的新玩家自动继承，既有玩家容器需在正常升级时用同一配置重建。此配置只影响官方截图获取，不改动游戏流、ROM、宿主机 DNS 或全局代理。

`cloudflare` 使用 [Cloudflare DoH JSON 接口](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)，固定从 `1.1.1.1` / `1.0.0.1` 引导连接；`google` 使用 [Google Public DNS JSON 接口](https://developers.google.com/speed/public-dns/docs/doh/json)，从 `8.8.8.8` / `8.8.4.4` 引导连接。两者都验证解析服务原域名的 TLS 证书，仅查询允许的图片域名，拒绝返回的私网或保留地址，并按 TTL 缓存至多 5 分钟。该方式需要容器能通过 IPv4 访问相应公网服务。

所有模式下，图片连接都会固定到刚验证的公网 IP，TLS 与 HTTP Host 仍使用原图片域名；每次重定向重新检查域名与地址，避免“检查后再次解析”的 DNS 重绑定问题。不会读取环境中的 HTTP 代理，也不接受自定义解析 URL、私网例外或关闭 TLS 验证。修正解析配置后，旧策略的失败标记不会继续遮住可用图片。

抓取缓存、家庭目录清单、资料批次和扫描报告属于私有运行数据，应存放在忽略目录或独立数据卷。不要随代码发布 ROM、密钥、固件或用户的整套媒体资料。导入批次前先验证字段和身份，导入后再检查 API 返回、图片显示与排序结果。
