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

工作目录：

```powershell
PixivCollection-Pipeline\pixiv_collection
```

Python 环境：

```powershell
PixivCollection-Pipeline\.venv\Scripts\python.exe
```

核心本地数据：

```text
collection.json        爬虫内部状态，必须长期保留
images.json            展示站读取的数据
image\original\        原图归档
image\preview\         预览图，webp
image\thumbnail\       缩略图，webp
logs\                  运行日志
backups\               每次写 collection/images 前的备份
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

## 第一次配置

打开 PowerShell：

```powershell
cd PixivCollection-Pipeline\pixiv_collection
```

推荐复制模板到本地私有配置文件：

```powershell
Copy-Item .\pipeline.example.env .\pipeline.local.env
notepad .\pipeline.local.env
```

填入：

```text
PIXIV_USER_ID=你的 Pixiv 用户 ID
PIXIV_REFRESH_TOKEN=你的 Pixiv refresh token
CLOUDFLARE_ACCOUNT_ID=你的 Cloudflare Account ID
R2_BUCKET=pixiv-images
R2_ENDPOINT_URL=https://你的 Cloudflare Account ID.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=你的 R2 S3 Access Key ID
AWS_SECRET_ACCESS_KEY=你的 R2 S3 Secret Access Key
```

也可以不用文件，直接在当前 PowerShell 窗口设置环境变量。

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

环境变量只对当前 PowerShell 窗口有效。`pipeline.local.env` 会被脚本自动读取，更适合日常使用。这个文件已经被 `.gitignore` 忽略，不要提交。

## 日常完整运行

默认扫公开收藏和私密收藏最多 9999 页，实际到没有下一页就会停止，然后上传 R2：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root .
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

只爬取和更新本地，不上传：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-upload
```

只上传当前本地结果，不访问 Pixiv：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl
```

只看上传计划，不实际上传：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl --dry-run-upload
```

临时只扫前几页：

```powershell
..\.venv\Scripts\python.exe pipeline.py --root . --public-pages 5 --private-pages 5
```

## 上传策略

脚本是增量上传，不做远端删除：

```text
本地有，R2 没有              上传
本地和 R2 同名但大小不同     上传覆盖
本地和 R2 同名且大小相同     跳过
R2 有，本地没有              保留远端，不删除
```

不默认删除远端对象，是为了避免误删历史图。

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
logs\pipeline_*.log    爬虫详细日志
backups\时间戳\        运行前的 collection/images 备份
```

## 安全提醒

- `collection.json` 是本地继续增量爬取的主状态文件，不要删。
- `image\original` 是原图历史归档，不要随便清理。
- `PIXIV_REFRESH_TOKEN` 和 `AWS_SECRET_ACCESS_KEY` 泄露后要轮换。
- R2 bucket 当前就是 `pixiv-images` 根目录，不需要 `--r2-prefix`。
