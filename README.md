# PixivCollection Pipeline

这个仓库保留的是 [PixivCollection](https://github.com/AgIzT/PixivCollection) 展示站配套的本地爬虫与发布脚本。

它的目标很简单：把 Pixiv 收藏夹里的作品持续归档到本地，并把展示站需要的静态文件增量上传到 Cloudflare R2。

```text
Pixiv 收藏夹
  -> 本地原图归档
  -> collection.json / images.json
  -> preview / thumbnail
  -> Cloudflare R2
  -> PixivCollection 静态展示站
```

## 特点

- 基于已有 `collection.json` 继续增量维护，不会因为 Pixiv 作者删除作品而丢掉本地历史数据
- 默认扫描公开收藏和私密收藏最多 `9999` 页，Pixiv 没有下一页时自动停止
- 只下载本地缺失的原图
- 自动生成展示站需要的 `preview` 和 `thumbnail`
- 上传 R2 前会校验本地归档完整性
- R2 上传采用批量列对象后本地比对，只上传缺失或大小变化的文件
- 默认不删除 R2 上本地没有的对象，避免误删历史归档

## 目录

```text
pixiv_collection/
├── collection.py      # PixivCollection 爬虫核心
├── pipeline.py        # 爬取、校验、上传 R2 的闭环入口
├── PIPELINE.md        # 详细运行说明
├── requirements.txt   # Python 依赖
├── colorthief.py
└── example.py
```

本地运行后会生成但不会提交：

```text
pixiv_collection/collection.json
pixiv_collection/images.json
pixiv_collection/image/
pixiv_collection/logs/
pixiv_collection/backups/
pixiv_collection/pipeline.local.env
```

## 快速开始

安装依赖：

```powershell
cd D:\program\PixivCollection-Pipeline
python -m venv .venv
D:\program\PixivCollection-Pipeline\.venv\Scripts\python.exe -m pip install -r pixiv_collection\requirements.txt
```

进入项目目录：

```powershell
cd D:\program\PixivCollection-Pipeline\pixiv_collection
```

设置 Pixiv 与 R2 凭据，可以使用环境变量，也可以复制 `pipeline.example.env` 为本地私有文件 `pipeline.local.env`：

```text
PIXIV_USER_ID=你的 Pixiv 用户 ID
PIXIV_REFRESH_TOKEN=你的 Pixiv refresh token
CLOUDFLARE_ACCOUNT_ID=你的 Cloudflare Account ID
R2_BUCKET=pixiv-images
R2_ENDPOINT_URL=https://你的AccountID.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=你的 R2 S3 Access Key ID
AWS_SECRET_ACCESS_KEY=你的 R2 S3 Secret Access Key
```

完整运行：

```powershell
D:\program\PixivCollection-Pipeline\.venv\Scripts\python.exe pipeline.py --root .
```

只检查本地数据：

```powershell
D:\program\PixivCollection-Pipeline\.venv\Scripts\python.exe pipeline.py --root . --validate-only
```

只上传当前本地结果到 R2：

```powershell
D:\program\PixivCollection-Pipeline\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl
```

先看上传计划，不实际上传：

```powershell
D:\program\PixivCollection-Pipeline\.venv\Scripts\python.exe pipeline.py --root . --skip-crawl --dry-run-upload
```

## R2 对象结构

R2 bucket 根目录应保持：

```text
collection.json
images.json
image/original/*
image/preview/*
image/thumbnail/*
```

展示站主要读取 `images.json` 与 `image/*`。`collection.json` 也会上传保存，用来支撑以后继续增量爬取。

## 详细文档

完整运行说明见：

[pixiv_collection/PIPELINE.md](pixiv_collection/PIPELINE.md)

## 安全

真实的 Pixiv token、R2 key、本地图片、日志和生成的 JSON 都不应该提交到仓库。`.gitignore` 已经忽略这些文件。
