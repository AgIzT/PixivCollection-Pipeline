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

## 需要准备

- Python 3.10+，推荐 Python 3.11
- Pixiv 用户 ID
- Pixiv refresh token
- Cloudflare R2 bucket
- 具有读取、写入、列出对象权限的 R2 S3 API key

Pixiv refresh token 可以参考原项目文档中提到的 [Pixiv OAuth Flow](https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362) 或 [Pixiv OAuth Flow (with Selenium)](https://gist.github.com/upbit/6edda27cb1644e94183291109b8a5fde) 获取。

R2 S3 API key 可在 Cloudflare R2 的 API token 页面创建，权限至少需要允许读取、写入和列出目标 bucket 中的对象。

R2 bucket 可以使用任意名称，默认示例为 `pixiv-images`。脚本会把文件发布到 bucket 根目录：

```text
collection.json
images.json
image/original/*
image/preview/*
image/thumbnail/*
```

## 目录

```text
pixiv_collection/
├── collection.py      # PixivCollection 爬虫核心
├── pipeline.py        # 爬取、校验、上传 R2 的闭环入口
├── PIPELINE.md        # 详细运行说明
├── pipeline.example.env
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

克隆仓库：

```powershell
git clone https://github.com/AgIzT/PixivCollection-Pipeline.git
cd PixivCollection-Pipeline
```

创建 Python 环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\pixiv_collection\requirements.txt
```

复制配置模板：

```powershell
Copy-Item .\pixiv_collection\pipeline.example.env .\pixiv_collection\pipeline.local.env
```

编辑 `pixiv_collection\pipeline.local.env`，填入 Pixiv 与 R2 凭据：

```text
PIXIV_USER_ID=你的 Pixiv 用户 ID
PIXIV_REFRESH_TOKEN=你的 Pixiv refresh token
CLOUDFLARE_ACCOUNT_ID=你的 Cloudflare Account ID
R2_BUCKET=pixiv-images
R2_ENDPOINT_URL=https://你的AccountID.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=你的 R2 S3 Access Key ID
AWS_SECRET_ACCESS_KEY=你的 R2 S3 Secret Access Key
```

完整运行。第一次运行会从空的本地归档开始，下载收藏夹原图、生成 `collection.json` / `images.json` / 预览图 / 缩略图，并上传到 R2：

```powershell
.\.venv\Scripts\python.exe .\pixiv_collection\pipeline.py --root .\pixiv_collection
```

只检查本地数据：

```powershell
.\.venv\Scripts\python.exe .\pixiv_collection\pipeline.py --root .\pixiv_collection --validate-only
```

只上传当前本地结果到 R2：

```powershell
.\.venv\Scripts\python.exe .\pixiv_collection\pipeline.py --root .\pixiv_collection --skip-crawl
```

先看上传计划，不实际上传：

```powershell
.\.venv\Scripts\python.exe .\pixiv_collection\pipeline.py --root .\pixiv_collection --skip-crawl --dry-run-upload
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

## 来源与许可

核心爬虫脚本来自 [orilights/python_scripts](https://github.com/orilights/python_scripts) 的 `pixiv_collection`，遵循 MIT License。本仓库保留原始授权声明，并在此基础上加入 R2 上传、归档校验和运行文档。

更多说明见 [NOTICE.md](NOTICE.md)。
