# X 书签独立备份（隔离实现，尚未部署）

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
原尺寸优先，允许官方提供的 4096x4096、large 回退；没有实测账号端的可见范围和下载限额。

## 五个阶段与失败恢复

| 阶段 | 确认条件 |
| --- | --- |
| discovered | 作者、原帖链接、媒体顺序与预期张数已原子保存到私有状态 |
| downloaded | 一帖所有图片已通过 Pillow 完整性检查并记录 SHA-256 |
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
当前 26 项离线测试通过，包含不完整导出拒绝确认及图片加工失败后从缓存重入队。

```bash
python -B -m unittest discover -s tests -v
```

Windows 的受限沙箱可能禁止原子文件替换；可将 `PIXIV_TEST_TMP` 指向获准的临时目录，
或在获准的本机运行环境执行同一套离线测试。无需安装 gallery-dl、loguru、pixivpy3 来跑这些测试；需要 Pillow。

## 上线前唯一人工账号步骤

**由用户在目标服务器自行准备私有 `gallery-dl.local.json`，按官方文档配置自己账户的登录会话。**
`gallery-dl.example.json` 只有非秘密选项，没有账号会话；不能直接用于 live 模式。
不要把会话发到聊天或提交仓库，私有配置与会话文件应限制为仅任务用户可读。
新建 `.x-sync/` 在 POSIX 下默认权限为 0700；已有目录应确认权限，示例服务另设 UMask=0077。
适配器显式禁用自动浏览器凭据提取、netrc、用户名密码登录以及 cookie 文件更新，
不会把子进程原始输出复制到报告。媒体下载不接收账号配置文件。

## 待部署事项（此轮均未执行）

1. 经确认后在适当运行环境安装 `requirements-x.txt` 中固定版本的外部下载器。
   当前电脑没有安装 gallery-dl；版本协议通过官方源码与模拟 CLI 测试核实。
2. 审查代码和账号接入后，把本地分支发布并部署到服务器；准备私有配置与磁盘水位监控。
3. 先小规模真实账号试跑，核实可见书签范围、全部图片、作者及原帖链接、R2 结果、权限和失败恢复。
4. 最后安装 `.service.example` / `.timer.example` 示例并确认每日时间。示例尚未启用。

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
