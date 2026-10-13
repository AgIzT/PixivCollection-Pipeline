# X 书签自动归档

只处理书签图片，不处理点赞或视频。每日完整遍历接口可返回的书签，依据
`twitter:<帖子 ID 字符串>` 去重，不使用最大 ID、帖子发布时间或首个已知帖子作为停止条件。
本地已保存的图片与状态不会因为取消书签、原帖删除或列表缺项而被删除。
首轮会导入接口可返回的现有图片书签及直接引用原帖的图片；后续只处理没有完成备份的帖子。
被引用原帖保留自己的作者和原帖链接，`bookmark_contexts` 另存外层书签关联。
同一原帖被多次引用时合并关联，不重复下载；新增关联只刷新归档元数据并重试发布。
只展开直接一层引用，不处理递归多层引用、视频、卡片缩略图或站外链接图片。

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
已完成小样本及当前接口完整分页范围的图片下载、入库、上传和前端实测；
接口返回内容不等于已证明账号全部历史书签的可见范围。

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
原分支有 26 项离线测试，本分支新增 20 项隔离回归和 9 项引用关联回归，合计 55 项在服务器通过。
新增测试包含整帖限定、样本不推进全量检查点、磁盘保护、尺寸校验、隔离上传、取消书签后缓存重试，
以及使用实际安装的 gallery-dl 参数解析器检查命令（未安装时该项明确跳过）。
真实参数解析和账号实测发现旧下载命令的 `--timeout` 不受支持，已修正为 `--http-timeout`。
引用回归覆盖原作者与外层关联分离、重复关联合并、元数据发布中断恢复及普通帖的 `quote_id=0`。

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
- 完整范围的上述 63 张图片随后全部下载入库并隔离上传；191 个 R2 对象回读均为 HTTP 200 且 SHA-256 一致。
- 终检修复旧配置关闭引用帖的问题，补齐一个直接引用原帖：最终 60 个图片原帖、64 张图片，2 个多图帖。
  全部 64 张源尺寸与实际解码尺寸相符，公开媒体最终 URL 均为 `name=orig`，没有退回低清尺寸。
  普通帖 `quote_id=0` 已按直接书签处理；所有分P均保存关联，引用图仍指向自己的作者与原帖。
  最终 194 个 R2 对象回读均为 HTTP 200 且 SHA-256 一致，其中包含全部 64 张原图。
- 最终隔离归档根目录为 `/opt/pixiv-x-bookmarks-prep/metadata-20261013-a1`，R2 前缀为
  `x-smoke/full-20261013-a1/`。60 帖均已 `published`，没有待重试项；原图合计约 43.3 MB。
- 修复后再次真实完整读取下载量为 0，所有归档文件及元数据哈希不变，没有重复发布需要。
- 全部范围的本地前端预览显示 60 个作品、64 张图片、43 位作者；前端代码无改动。
- 土耳其服务器对公开 R2 域名的回读返回 HTTP 403；S3 接口上传和 Windows 公共域名回读正常。这不是 X 会话失效。

**没有执行真实取消书签/删帖测试、生产发布或 X 定时器启用。**
源列表消失后的永久保留使用实际归档加模拟空列表验证；下载/上传失败均为明确注入的故障。
既有生产 X 数据由手动脚本录入，不作为本自动采集器的历史成功证据。

## 待生产部署事项（均未执行）

1. 审查独立分支，并在保持原 Pixiv 服务的前提下安排生产整合。
2. 安排会话失效/限流告警与磁盘监控；隔离实测后仍有约 11.35 GiB 可用，不能代替长期容量监控。
3. 确认生产 R2 元数据增量合并及备份：本轮只读比对发现 4 个来源 ID 已有，需新增 56 帖、60 张图，
   另保留当前列表缺失的 13 个手动录入旧帖。合并后预计 X 共 73 帖、80 张图，总图片行数 1878。
   该计划尚未执行，不能用测试前缀元数据直接替换生产文件。
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

## 多账号接入

`--account-config NAME=PATH` 可以重复传入，为每个账号使用独立的私有会话文件。
同一批任务顺序读取各个账号的书签，然后合并到同一个归档根目录；不会并行修改元数据。
共享的 `twitter:<原帖 ID>` 只保存一份图片，多账号收藏该帖时只合并来源账号标签。
标签是本地短标识（例如 `account-1`），不会从图片作者推断登录账号，报告不包含会话文件路径或凭据。
`bookmark_accounts` 记录曾在哪些账号发现该原帖；它与 `bookmark_contexts` 分别表示账号及直接/引用来源，
不表示每一个账号与每一个引用外层帖的完整对应关系。

```bash
.venv/bin/python x_bookmarks.py --root /opt/pixiv-x-bookmarks-prep/combined-example --live \
  --account-config account-1=/opt/pixiv-x-bookmarks-prep/auth/gallery-dl.local.json \
  --account-config account-2=/opt/pixiv-x-bookmarks-prep/auth/gallery-dl.account2.local.json \
  --max-download-mib 512 --min-free-gib 5 \
  --publish --r2-prefix x-smoke/multi-example/
```

命名会话入口不能与 `--gallery-config`、离线 fixture 或 `--max-posts` 混用；重复标签、重复文件路径会被拒绝。
旧单会话入口继续兼容。无账号标签的历史帖不会被猜测归属，只有再次在该账号列表中发现才添加标签。
移除账号配置、取消书签或列表缺项均不删除图片与历史标签。
每个账号有独立扫描状态及完整扫描时间；一份会话失效不阻断另一份会话，
只有本轮所有账号均完整发现才推进全局完整扫描检查点。
下载预算由整个任务共享，失败项保留等待重试，公开媒体下载不携带任一账号会话。

多账号运行是完整模式，因此 **必须同时显式指定隔离 root 与 R2 前缀**；
其上传目标保护不能依赖单账号 `--max-posts` 样本入口的检查。
第二账号的会话由用户在自己的第二账号书签页亲自输入，保存到独立 0600 文件，不覆盖第一账号。
用户切换账号是本轮身份依据；gallery-dl 当前没有内置会话登录者 UID 方法，本轮没有新增 OAuth 或身份探测接口。

新增 `tests/test_x_accounts.py` 8 项离线回归，全部 63 项在服务器通过。
覆盖共同及独有帖子去重、账号异常脱敏和独立续跑、部分扫描检查点、旧状态迁移、发布失败重试、
空列表/移除配置后的永久保留、公开媒体无凭据下载以及 CLI 兼容与参数拒绝。

### 2026-10-13 双账号隔离实测

- 在新的 `/opt/pixiv-x-bookmarks-prep/combined-20261013-a1` 复制原隔离归档，
  上传只指向 `x-smoke/multi-20261013-a1/`。第一账号原来的隔离数据和会话文件保留。
- 两个账号接口扫描均完整。首次合并 178 个图片原帖、215 张图片；复跑发现一个新原帖，
  并更新一个已经归档帖的共享账号标签，仅补下新图片，不重复处理已有原图。
- 最终已记录来源：`account-1` 为 64 帖、74 图；`account-2` 为 129 帖、159 图；
  两账号共享 14 帖、17 图，去重合并共 179 帖、216 图。账号标签是曾发现来源，列表消失时不移除。
- 216 张图片全部通过原尺寸检查，作者及原帖链接与各自原帖清单一致，所有分P均保存账号标签。
  原图共约 125.2 MB；新增下载约 81.9 MB，剩余磁盘约 10.95 GiB。
- 179 帖均为 `published`，没有待重试项；650 个 R2 对象公网回读均为 HTTP 200 且 SHA-256 一致，
  包含全部 216 张原图。稳定列表再次真实扫描下载量为 0，所有归档文件和元数据哈希不变。
- 前端合并预览显示 179 个作品、216 张图片、130 位作者、103 个标签；没有修改前端代码。
- 一个会话失败不阻断另一个的行为通过离线回归；未故意令真实会话过期或触发限流。
  生产元数据哈希、Pixiv 服务及定时器保持不变，没有合并 main、生产部署或启用 X 定时器。

## 会话、限流与维护边界

当前采用免费的网页会话提取方式，没有购买或使用付费官方 API。
会话失效需要用户重新接入；429 按配置中止本轮，不推进完整扫描检查点，已发现队列和归档保留。
本轮没有故意触发真实限流或让会话失效，不能宣称这两种线上错误已实测通过。
X 网页接口及 gallery-dl 提取器可能变化；固定版本可保证本轮复现，但后续升级仍需检查接口并重新验证。
官方依据：[引用图片](https://gdl-org.github.io/docs/configuration.html#extractor-twitter-quoted)、
[原图尺寸](https://gdl-org.github.io/docs/configuration.html#extractor-twitter-size)、
[限流行为](https://gdl-org.github.io/docs/configuration.html#extractor-twitter-ratelimit)。

## 生产部署与每日随机启动

生产入口保留现有 `/opt/pixiv_collection` 归档，复用其来源注册表分配内部 ID；
不能把隔离 `collection.json` 或 `images.json` 直接替换到生产。
部署前先备份实际代码、元数据与 units，在独立目录用旧图片只读硬链接做增量入库预演，
并确认新文件名不与 R2 现有对象冲突。生产投递和入库期间短暂停止 receiver，
同时持现有 `.pipeline.lock`；成功提交或异常退出后立即恢复接收器。
现有 Pixiv unit、定时器及 `.venv` 均保留。X 使用独立 `.venv-x`，依赖固定在
`requirements-x-runtime.txt`；两个账号分别由 `auth/gallery-dl.account1.local.json` 与
`auth/gallery-dl.account2.local.json` 提供会话，目录 0700、文件 0600，永不提交或打印。

`systemd/pixiv-x-bookmarks.service.example` 和 `.timer.example` 配置为：

- 每天北京时间 03:00 起加新随机延迟（最多 10799 秒），在 03:00–06:00 内启动一次。
- 显式 `Asia/Shanghai`，兼容主机 systemd 249；`AccuracySec=1s`、`FixedRandomDelay=no`。
- `Persistent=false`，停机错过当天不在窗口外补跑；服务入口及取得归档锁后都复检窗口。
- 已启动的任务可以在 06:00 后完成。归档锁冲突每 60 秒有限重试，06:00 尚未拿到就跳过。
- wrapper 使用独立 daily.lock；主采集器自己持 pipeline.lock，避免重复外层 flock。
- 北京日期的尝试标记在持归档锁后、读取账号前原子保存；同日成功、失败或中断均不自动重复整批。
  失败项保留，次日继续；运维手动排障可直接调用主采集器。
- 没有无界 `Restart=on-failure`。日志仅输出固定结果和允许的计数，详细状态在私有 `.x-sync/`。

上传先完成所有媒体对象；任一媒体失败时保留旧线上 JSON。
媒体齐全后顺序发表 `collection.json`，最后发表浏览器读取的 `images.json`；
两个对象各自原子 PUT，仍不是跨对象事务，失败留待重试。
新增 7 项调度及 4 项发布顺序回归，全部 74 项在专用 X 环境通过。

官方依据：[systemd 249 timer](https://github.com/systemd/systemd/blob/v249/man/systemd.timer.xml)。
