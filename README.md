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
- R2 图片上传采用批量列对象后本地比对，只上传缺失或大小变化的图片
- 默认不删除 R2 上本地没有的对象，避免误删历史归档
- 支持 X、B站等来源通过接收器投递到 `pending/`，与 Pixiv 归档合并后统一上传
- 多来源使用幂等注册表和合成 ID，保留来源链接并按发布时间导出
- 元数据 JSON 每次上传刷新；图片按大小增量上传，并设置不同的缓存策略
- 提供每日采集任务和本地接收器的 systemd 配置示例

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
├── ingest.py          # 合并多来源 pending 队列
├── ingest_schema.py   # 清单校验与磁盘水位约定
├── receiver.py        # 标准库 HTTP 接收器
├── systemd/           # 采集任务、定时器与接收器服务示例
├── PIPELINE.md        # 详细运行说明
├── pipeline.example.env
├── ingest.example.env
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
pixiv_collection/ingest.local.env
pixiv_collection/pending/
pixiv_collection/ingest_report.json
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

跳过 Pixiv 爬取，合并 pending 后上传本地结果到 R2（仅上传现有归档需另加 `--skip-ingest`）：

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

## 多来源接收器

`receiver.py` 使用标准库，默认仅监听 `127.0.0.1:8788`，需要通过已有反向代理或 Tunnel 提供公网入口。
将 `ingest.example.env` 复制为私有的 `ingest.local.env`，把 `INGEST_TOKEN` 替换为自行生成的强随机值。
使用 systemd 示例时，凭据通过 `EnvironmentFile` 加载；直接启动时需要先将该变量载入进程环境。
生产接收器必须设置该变量：当前源码在没有 token 时仅告警，接口会没有鉴权。

接口包括无需鉴权的 `GET /health`，以及需要 Bearer 鉴权的 `POST /ingest` 和 `GET /exists`。
接收器将图片和清单写入 `pending/`；每日 pipeline 会合并队列，也可通过 `--ingest-only` 单独合并后校验并上传。
`--skip-ingest` 可跳过队列合并。`--skip-crawl` 仍会合并队列，只有同时添加 `--skip-ingest` 才会仅处理现有归档。

`systemd/` 是配置示例，默认运行目录为 `/opt/pixiv_collection`，接收器用户为 `pixivingest`。
安装前需准备该目录、虚拟环境、私有配置、服务用户及适当的队列读写权限。
同步这个仓库不会自动安装 unit、重启服务或触发采集上传。

## 详细文档

完整运行说明见：

[pixiv_collection/PIPELINE.md](pixiv_collection/PIPELINE.md)

## 安全

真实的 Pixiv token、R2 key、本地图片、日志和生成的 JSON 都不应该提交到仓库。`.gitignore` 已经忽略这些文件。

## 来源与许可

核心爬虫脚本来自 [orilights/python_scripts](https://github.com/orilights/python_scripts) 的 `pixiv_collection`，遵循 MIT License。本仓库保留原始授权声明，并在此基础上加入 R2 上传、归档校验和运行文档。

更多说明见 [NOTICE.md](NOTICE.md)。
