# X 书签独立备份（已完成小样本实测，尚未生产部署）

只处理书签图片，不处理点赞或视频。每日完整遍历接口可返回的书签，依据
`twitter:<帖子 ID 字符串>` 去重，不使用最大 ID、帖子发布时间或首个已知帖子作为停止条件。
本地已保存的图片与状态不会因为取消书签、原帖删除或列表缺项而被删除。
首轮会导入接口可返回的全部现有图片书签；后续只处理没有完成备份的帖子。

## 固定版本与接口依据

可替换适配器使用独立的 gallery-dl CLI，固定为 **1.32.16**：

- [官方发布](https://github.com/mikf/gallery-dl/releases/tag/v1.32.16)，2026-10-09 发布并指向 Codeberg。
- [固定版本 Twitter 提取器](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.16/gallery_dl/extractor/twitter.py)：
  `TwitterBookmarkExtractor` 支持 `/i/bookmarks`；公开媒体直链有独立的 `TwitterImageExtractor`。
- [固定版本 DataJob](https://codeberg.org/mikf/gallery-dl/src/tag/v1.32.16/gallery_dl/job.py)：
  `--dump-json` 输出 Directory=2、Url=3；错误消息编号为 -1，**退出码 0 不保证提取成功**。
- [配置文档](https://gdl-org.github.io/docs/configuration.html) 和
  [CLI 文档](https://gdl-org.github.io/docs/options.html)。

不会复制官方 GPL 源码。只使用 CLI 接口，任何后续升级都应重新检查格式及回归测试。
只接受 `size=["orig"]`，不再以 4096x4096 或 large 回退满足原图要求。
每张图片记录 X 返回的源尺寸、实际解码尺寸、请求 URL 和 SHA-256；尺寸不符时保留重试。
`orig` 指 X 提供的原尺寸文件，不能据此保证与作者上传前的文件逐字节相同。
已完成小样本读取及完整接口元数据遍历；接口返回内容不等于已证明账号全部历史书签的可见范围。
尚未实测全量原图下载与下载限额。

## 五个阶段与失败恢复

| 阶段 | 确认条件 |
| --- | --- |
| discovered | 作者、原帖链接、媒体顺序与预期张数已原子保存到私有状态 |
| downloaded | 一帖所有图片通过 Pillow 完整性及原尺寸检查，并记录 SHA-256 |
| queued | 复用 receiver 的原子 stage_item，图片目录先落盘、清单后出现 |
| archived | collection.json 的来源注册表和 images.json 的全部分P一致 |
| published | 既有 pipeline 成功，R2 报告无错误且 publication_id 与本次任务一致 |

状态、报告、下载缓存保存在 `.x-sync/`，不会提交到 Git。
已经下载成功的图片不会因同帖另一张失败而重下；临时损坏文件会重新下载。
即使取消书签导致后续列表不含该帖子，已发现的待重试项仍保留并继续尝试媒体直链。
下载完的文件、失败队列和归档均不做自动清理；需要持续监控磁盘空间。

队列消费改为在两个元数据文件成功写入、导出包含该帖全部分P后确认；两个文件分别采用原子替换。
两者不是单个数据库事务：若 collection.json 成功而导出失败，队列保留，重试按来源 ID 去重并补齐导出。
如果目录已移到 consumed 而清单尚未移动，持久化注册表仍允许下一次补完确认。
consumed 文件本身不再被当作落盘成功证据，已有 consumed 资料不会被覆盖删除。

## 离线验证

`tests/fixtures/gallery_bookmarks.json` 只含合成作者和帖子，不包含真实账号数据。
测试动态生成图片，禁止 socket 网络访问，模拟 gallery-dl、Pixiv API 和 R2。
图片加工、元数据写入、去重及队列确认使用实际实现。
原分支有 26 项离线测试，本分支新增 20 项隔离回归测试，合计 46 项在服务器通过。
新增测试包含整帖限定、样本不推进全量检查点、磁盘保护、尺寸校验、隔离上传、取消书签后缓存重试，
以及使用实际安装的 gallery-dl 参数解析器检查命令（未安装时该项明确跳过）。
真实参数解析和账号实测发现旧下载命令的 `--timeout` 不受支持，已修正为 `--http-timeout`。

```bash
python -B -m unittest discover -s tests -v
```

Windows 的受限沙箱可能禁止原子文件替换；可将 `PIXIV_TEST_TMP` 指向获准的临时目录，
或在获准的本机运行环境执行同一套离线测试。无需安装 gallery-dl、loguru、pixivpy3 来跑这些测试；需要 Pillow。

## 用户会话接入

**由用户亲自准备自己的 X 会话，按官方文档保存到私有 `gallery-dl.local.json`。**
当前 Twitter 提取器不支持用户名/密码登录；需要浏览器会话 Cookie。
会话值只能由用户输入到本机私有输入窗口或服务器私有文件，不得发送到聊天。
`gallery-dl.example.json` 只有非秘密选项，没有账号会话；不能直接用于 live 模式。
不要把会话发到聊天或提交仓库，私有配置与会话文件应限制为仅任务用户可读。
新建 `.x-sync/` 在 POSIX 下默认权限为 0700；已有目录应确认权限，示例服务另设 UMask=0077。
适配器显式禁用自动浏览器凭据提取、netrc、用户名密码登录以及 cookie 文件更新，
不会把子进程原始输出复制到报告。媒体下载不接收账号配置文件。

## 隔离小样本入口

先使用新的归档根目录；不要指向生产目录、脚本目录或它们的子目录：

```bash
/opt/pixiv-x-bookmarks-prep/.venv/bin/python x_bookmarks.py \
  --root /opt/pixiv-x-bookmarks-prep/smoke-example --live \
  --gallery-config /opt/pixiv-x-bookmarks-prep/auth/gallery-dl.local.json \
  --max-posts 5 --page-size 2 --max-download-mib 256 --min-free-gib 5
```

`--max-posts` 只限制整帖，允许 1～10 帖，不截断一帖的多图；`--page-size` 是每页查询数。
样本只记录 `sample_complete`，不会更新全量的 `scan_complete` 或 `last_complete_scan`。
样本选中来源 ID 持久化，后续取消书签仍可重试已经完整发现的媒体。
如果首次发现不完整后又新增书签，把选中帖挤出前 N，样本可能无法补齐缺失媒体；
实测期间保持书签列表稳定，必要时从新样本根目录重新验证。日常全量模式没有这个截断条件。

下载前保留至少 5 GiB 空间，另预留缓存、队列和原图副本所需空间。
每张图最大 50 MiB；样本默认成功下载文件预算为 256 MiB。
预算不包含 gallery-dl 内部失败重试产生的网络流量，不能视为严格流量上限。

本地入库先使用 `pipeline.py --root <样本目录> --ingest-only --skip-upload`。
样本 `--publish` 强制要求显式 `--r2-prefix x-smoke/<run>/`，也可显式覆盖 `--r2-bucket`。
本地 root 与 R2 目标是两个独立参数；仅换 root 不能确保 R2 隔离。
样本外的 pending 项会阻止样本发布。上传前检查新前缀为空及 dry-run 对象计划。
前端预览必须同时把数据和三类图片路径指向测试前缀，不能覆盖生产根目录的 `images.json`。

## 2026-10-13 隔离实测记录

- 用户亲自输入会话，经现有 SSH 保存到隔离文件；目录权限 0700，文件 0600。
- 真实 X GraphQL 书签读取连续 3 页均返回 HTTP 200，取到 5 帖、8 张图片，包含一个四图帖。
- 8 张图片全部下载，源尺寸与实际尺寸一致；仅记录公开媒体请求的最终 URL，均为 `name=orig`。
- 作者 ID、名称、账号、作者链接和原帖链接已入库；本地完整性校验通过。
- 独立 R2 前缀共 26 个对象，约 6.4 MB。Windows 回读全部 26 个对象均为 HTTP 200 且 SHA-256 一致。
- 对真实图片下载注入一次失败后，下一轮补齐，已成功的第一张没有重下且缓存哈希不变。
- 在另一个独立 R2 前缀注入一次上传失败：25 项成功、1 项失败；重试补传失败原图并刷新两个元数据，跳过 23 个图片对象。
- 重复真实读取没有新增文件或重下；模拟源列表为空后，归档文件及元数据哈希保持不变。
- 实际 pipeline 上传回执通过，5 个样本帖均到达 `published` 状态。
- 本地隔离前端显示 5 个作品、8 张图片、5 位作者，四图帖 p0～p3 全部可解码和切换。
- 后续仅元数据的完整接口遍历也通过：3 次 Bookmarks 请求均为 HTTP 200，发现 59 个图片帖、63 张图片，
  其中 2 个多图帖；59 帖媒体集合均完整。该步骤没有下载或上传新增图片。
- 土耳其服务器对公开 R2 域名的回读返回 HTTP 403；S3 接口上传和 Windows 公共域名回读正常。这不是 X 会话失效。

原图下载和展示验证仍为小样本。**没有执行全量图片下载、真实取消书签/删帖测试、生产发布或 X 定时器启用。**
既有生产 X 数据由手动脚本录入，不作为本自动采集器的历史成功证据。

## 待生产部署事项（均未执行）

1. 审查独立分支，并在保持原 Pixiv 服务的前提下安排生产整合。
2. 核实全量图片下载、磁盘容量和会话失效/限流告警；完整接口遍历不能证明全部历史书签无遗漏。
3. 先确认生产 R2 元数据的增量合并计划与备份，避免用样本元数据替换生产文件。
4. 最后明确每日执行时间，安装 `.service.example` / `.timer.example`。示例尚未启用。

未来 live 入口（下列命令此轮没有执行）：

```bash
.venv/bin/python x_bookmarks.py --root /opt/pixiv_collection --live \
  --gallery-config /opt/pixiv_collection/gallery-dl.local.json --publish
```

程序内部获取现有 `.pipeline.lock` 并覆盖其 pipeline 子进程；不要在外面再次包一层 flock。
与现有每日 Pixiv 服务使用同一把锁，冲突时安全退出，未完成项留待重试。
仅传 `--live` 而不加 `--publish` 会下载并排队，不触发入库上传。
离线 fixture 模式禁止 `--publish`；没有明确的 `--live` 不会访问 X。

每日拉取无法保存“添加书签后、下一次扫描前就被删除”的图片；接口不再返回的未发现内容也无法恢复。
适配器会标记显式提取错误，但平台静默隐藏内容时不能证明已获取账户的所有书签。
不同帖子引用同一图片仍分别归档；这里做来源 ID 与分P 去重，不是跨帖内容去重。
