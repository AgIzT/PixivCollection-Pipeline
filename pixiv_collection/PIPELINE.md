# PixivCollection 日常爬取上传闭环

目标不是每天从 R2 拉数据，而是把本地归档当作主库持续维护。第一次运行可以从空归档开始；如果已经有旧的 `collection.json`、`images.json` 和 `image/`，也可以直接接着增量维护：

```text
本地旧归档
  -> 扫描 Pixiv 收藏夹全部页
  -> 下载本地没有的新原图
  -> 更新 collection.json
  -> 导出 images.json
  -> 生成 preview / thumbnail
  -> 校验本地归档
  -> 增量上传到 Cloudflare R2
```

## 目录

Windows 工作目录：

```powershell
PixivCollection-Pipeline\pixiv_collection
```

Windows Python 环境：

```powershell
..\.venv\Scripts\python.exe
```

Linux 工作目录：

```bash
/opt/pixiv_collection
```

Linux Python 环境：

```bash
/opt/pixiv_collection/.venv/bin/python
```

核心本地数据：

```text
collection.json        爬虫内部状态，必须长期保留
images.json            展示站读取的数据
image/original/        原图归档
image/preview/         预览图，webp
image/thumbnail/       缩略图，webp
logs/                  运行日志
backups/               每次写 collection/images 前的备份
archive_report.json    最近一次本地校验报告
upload_report.json     最近一次 R2 上传报告
```

R2 bucket `pixiv-images` 根目录对应：

```text
collection.json
images.json
image/original/*
image/preview/*
image/thumbnail/*
```

`PixivCollection` 展示站默认读取 `images.json` 和 `image/*`。`collection.json` 不是前端必须文件，但必须上传保存，因为它是以后继续增量爬取的状态主文件。

## 默认爬取范围

现在默认扫一个足够大的页数：

```text
--public-pages 9999
--private-pages 9999
```

Pixiv API 没有下一页时爬虫自己会停，所以 `9999` 基本等同全量，但不需要额外约定 `0 = 全量` 这种逻辑。

如果只是测试，可以临时限制页数：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --public-pages 2 --private-pages 2 --skip-upload
```

Linux：

```bash
.venv/bin/python pipeline.py --root . --public-pages 2 --private-pages 2 --skip-upload
```

## 第一次配置环境变量

推荐在 `pixiv_collection` 目录复制模板到私有配置文件：

```powershell
Copy-Item .\pipeline.example.env .\pipeline.local.env
```

填入 Pixiv 与 R2 凭据后，`pipeline.py` 会自动读取该文件；它已被 `.gitignore` 忽略。
也可以直接在当前进程中设置环境变量，以下均为需要替换的占位示例。

Windows 打开 PowerShell：

```powershell
cd PixivCollection-Pipeline\pixiv_collection
```

Pixiv：

```powershell
$env:PIXIV_USER_ID="你的 Pixiv 用户 ID"
$env:PIXIV_REFRESH_TOKEN="你的 Pixiv refresh token"
```

Cloudflare R2 S3：

```powershell
$env:CLOUDFLARE_ACCOUNT_ID="你的 Cloudflare Account ID"
$env:R2_BUCKET="pixiv-images"
$env:R2_ENDPOINT_URL="https://你的 Cloudflare Account ID.r2.cloudflarestorage.com"
$env:AWS_ACCESS_KEY_ID="你的 R2 S3 Access Key ID"
$env:AWS_SECRET_ACCESS_KEY="你的 R2 S3 Secret Access Key"
```

这些环境变量只对当前 PowerShell 窗口有效。新开窗口需要重新设置，或者你自己写成本机私有启动脚本。

Linux 可以在 shell 中临时导出变量，也可以使用本目录的私有环境文件 `pipeline.local.env`。`pipeline.py` 会自动读取它：

```bash
cd /opt/pixiv_collection
chmod 600 pipeline.local.env
.venv/bin/python pipeline.py --root . --validate-only
```

## 日常完整运行

默认扫公开收藏和私密收藏最多 9999 页，实际到没有下一页就会停止，然后上传 R2：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root .
```

Linux：

```bash
.venv/bin/python pipeline.py --root .
```

## Linux 自动任务

本机已使用 systemd timer 设置每日自动运行。unit 文件在项目内保留一份：

```text
systemd/pixiv-collection-pipeline.service
systemd/pixiv-collection-pipeline.timer
```

已安装到：

```text
/etc/systemd/system/pixiv-collection-pipeline.service
/etc/systemd/system/pixiv-collection-pipeline.timer
```

当前计划为服务器本地时间每天 08:13 触发，并允许最多 10 分钟随机延迟：

```ini
OnCalendar=*-*-* 08:13:00
RandomizedDelaySec=10min
```

查看下次运行时间：

```bash
systemctl status pixiv-collection-pipeline.timer --no-pager
```

手动立即运行一次完整流程：

```bash
systemctl start pixiv-collection-pipeline.service
```

修改 `systemd/` 下的 unit 文件后，重新安装并加载：

```bash
install -m 0644 systemd/pixiv-collection-pipeline.service /etc/systemd/system/pixiv-collection-pipeline.service
install -m 0644 systemd/pixiv-collection-pipeline.timer /etc/systemd/system/pixiv-collection-pipeline.timer
systemctl daemon-reload
systemctl enable --now pixiv-collection-pipeline.timer
```

完整流程会做：

1. 备份旧 `collection.json` 和 `images.json`
2. 登录 Pixiv API
3. 扫描公开收藏全部页
4. 扫描私密收藏全部页
5. 下载本地没有的新原图
6. 检测本地原图变化
7. 获取新增作品元数据
8. 生成缺失的 `image\preview`
9. 生成缺失的 `image\thumbnail`
10. 清理没有原图支撑的元数据
11. 保存新的 `collection.json`
12. 导出新的 `images.json`
13. 校验本地数据完整性
14. 增量上传到 R2

## 常用模式

只校验本地，不爬取、不上传：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --validate-only
```

```bash
.venv/bin/python pipeline.py --root . --validate-only
```

只爬取和更新本地，不上传：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-upload
```

```bash
.venv/bin/python pipeline.py --root . --skip-upload
```

不访问 Pixiv，先合并 pending，再上传本地结果（只上传现有归档需另加 `--skip-ingest`）：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl
```

```bash
.venv/bin/python pipeline.py --root . --skip-crawl
```

只看上传计划，不实际上传：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl --dry-run-upload
```

```bash
.venv/bin/python pipeline.py --root . --skip-crawl --dry-run-upload
```

临时只扫前几页：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --public-pages 5 --private-pages 5
```

```bash
.venv/bin/python pipeline.py --root . --public-pages 5 --private-pages 5
```

## 上传策略

脚本是增量上传，不做远端删除：

```text
本地有，R2 没有              上传
本地和 R2 同名但大小不同     上传覆盖
图片与 R2 同名且大小相同     跳过
collection/images.json     每次重传，刷新元数据及响应头
R2 有，本地没有              保留远端，不删除
```

不默认删除远端对象，是为了避免误删历史图。

元数据 JSON 使用 `Cache-Control: no-cache`，图片使用一年长缓存和 `immutable`。
JSON 即使大小没有变化也会重传，避免内容或缓存响应头更新被按大小跳过。

## 重复异常文件处理

如果 `image\original` 里出现带 hash 的旧文件名，同时规范文件名已经存在，例如：

```text
119993252-xxxx_p0.jpg
119993252_p0.jpg
```

爬虫会跳过这个重复异常名称文件，不覆盖、不删除规范文件。正常状态下校验报告应显示：

```text
problems: none
```

## 输出报告

每次运行后看：

```text
archive_report.json    本地数据是否完整
upload_report.json     R2 上传了什么、跳过了什么、是否有错误
logs/pipeline_*.log    爬虫详细日志
backups/时间戳/        运行前的 collection/images 备份
```

## 多来源入库（X / B站等）

Pixiv 之外的来源不走爬虫，由外部（油猴脚本 → 接收端）投递到 `pending/` 队列，
pipeline 在 **crawl 之后、validate 之前**合并进本地归档，之后复用既有的校验与
R2 增量上传，不新开上传通道。

### pending 布局

```text
pending/<item>.json        入库清单
pending/<item>/<n>.<ext>   清单里引用的原图
pending/consumed/<item>/   合并成功后归档至此
pending/failed/<item>/     合并失败移到此处，保留现场供排查
```

清单字段（`source_id` 必填，是幂等的唯一键）：

```json
{
  "source": "bilibili",
  "source_id": "1029384756102938475",
  "source_url": "https://t.bilibili.com/1029384756102938475",
  "title": "作品标题",
  "caption": "动态正文全文",
  "tags": ["标签1", "标签2"],
  "created_at": "2026-08-05T10:00:00+08:00",
  "collected_at": "2026-08-05T11:00:00+08:00",
  "x_restrict": 0,
  "bookmark": 42,
  "author": {
    "source_uid": "12345",
    "name": "作者名",
    "account": "handle",
    "url": "https://space.bilibili.com/12345"
  },
  "files": [
    { "file": "0.png", "origin_url": "https://i0.hdslb.com/bfs/new_dyn/a.png" }
  ]
}
```

### 为什么不直接用平台原始 id

X 的 tweet id 与 B 站动态 id 都是 snowflake，量级约 `1.9e18`，超过 JavaScript
`Number.MAX_SAFE_INTEGER`（`9.007e15`），前端读进去就会精度丢失。因此平台原始 id
只作字符串存进 `source_id`，内部另发一个从 `9e8` 起的递增 id（Pixiv 现役 illust id
约 `1.5e8`，作者 id 上限约 `1e8`，留足余量）。

`collection.json` 因此新增两个顶层键：

```text
sources    "{source}:{source_id}" -> 内部 id 的幂等注册表
counters   合成 id 游标
```

旧档案没有这两个键时按空处理，不需要迁移。

### 排序变更

`export()` 原先按 `id` 倒序。合成 id 恒大于 Pixiv id，沿用会让新来源永久占据首屏，
故改为按 `created_at` 倒序，再按内部 id 和分P 序号稳定排序。
旧档案没有多来源字段时仍按 Pixiv 处理，不需要迁移。

### 手动触发一次

```bash
.venv/bin/python pipeline.py --root . --ingest-only
```

`--ingest-only` 跳过爬虫，只合并 pending 并走完校验与上传。
调试时再加 `--skip-upload`。完全不想跑入库用 `--skip-ingest`。

结果见 `ingest_report.json`。

## 安全提醒

- `collection.json` 是本地继续增量爬取的主状态文件，不要删。
- `image\original` 是原图历史归档，不要随便清理。
- `PIXIV_REFRESH_TOKEN` 和 `AWS_SECRET_ACCESS_KEY` 泄露后要轮换。
- R2 bucket 当前就是 `pixiv-images` 根目录，不需要 `--r2-prefix`。
