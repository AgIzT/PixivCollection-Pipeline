# [PixivCollection](https://github.com/orilights/PixivCollection) 项目相关脚本

## 教程

简要的使用教程

1. 爬取图片（推荐使用 [Powerful Pixiv Downloader](https://github.com/xuejianxianzun/PixivBatchDownloader) 浏览器拓展）并按照 `[pid]_p[part].[ext]` 命名，放置于 `image/original` 目录下
   1. 如图片较少可跳过此步，直接使用本脚本爬取图片
2. 下载或克隆本项目，安装 Python （开发环境为 Python 3.11；本目录已在 Ubuntu 22.04 / Python 3.10 下验证）与相关依赖（pip install -r requirements.txt）
3. 获取 Pixiv 账号的 refresh token，具体步骤请参照 [Pixiv OAuth Flow](https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362) 或 [Pixiv OAuth Flow (with Selenium)](https://gist.github.com/upbit/6edda27cb1644e94183291109b8a5fde)
4. 将 `USER_ID` 和 `REFRESH_TOKEN` 填入 `example.py` 的对应位置
5. 执行 `python example.py`

## Linux 快速启动

本机当前工作目录：

```bash
cd /opt/pixiv_collection
```

已创建本地虚拟环境：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

`pipeline.py` 会自动读取当前目录的 `pipeline.local.env`。该文件保存 Pixiv 与 R2 密钥，建议只允许当前用户读取：

```bash
chmod 600 pipeline.local.env
```

只做本地归档校验：

```bash
.venv/bin/python pipeline.py --root . --validate-only
```

爬取并更新本地，但不上传 R2：

```bash
.venv/bin/python pipeline.py --root . --skip-upload
```

不访问 Pixiv，合并 pending 后检查归档并试算 R2 上传计划：

```bash
.venv/bin/python pipeline.py --root . --skip-crawl --dry-run-upload
```

完整运行并上传：

```bash
.venv/bin/python pipeline.py --root .
```

原有 `example.py` 示例默认会进行以下操作（完整 pipeline 的扫描范围见 `PIPELINE.md`）：

1. 检查 refresh token 是否可用
2. 读取 `collection.json` 中的数据
3. 获取用户公开收藏图片和不公开收藏图片各自的前两页，下载不存在于本地的图片
4. 检测本地文件变动
5. 获取新增图片的信息
6. 在 `image/preview` 目录下生成 WebP 格式预览图（尺寸不大于 2000*2000，质量 80）
7. 在 `image/thumbnail` 目录下生成 WebP 格式缩略图（尺寸不大于 500*1000，质量 70）
8. 将数据保存到 `collection.json` 文件
9. 导出可用于 PixivCollection 的数据到 `images.json` 文件

## 数据格式

以下为数据格式的 TS 定义，数据以 JSON 数组保存

```ts
interface Tag {
  name: string
  translated_name: string | null
}

interface Image {
  id: number
  part: number
  title: string
  size: [number, number]
  ext: string
  author: {
    id: number
    name: string
    account: string
  }
  tags: Tag[]
  created_at: string
  sanity_level: number
  x_restrict: number
  bookmark: number
  view: number
  dominant_color: string
}
```

## 感谢

[upbit/pixivpy](https://github.com/upbit/pixivpy)

[xuejianxianzun/PixivBatchDownloader](https://github.com/xuejianxianzun/PixivBatchDownloader)

[fengsp/color-thief-py](https://github.com/fengsp/color-thief-py)
