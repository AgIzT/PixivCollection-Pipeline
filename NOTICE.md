# Attribution

This repository packages a PixivCollection crawler and a personal publishing pipeline for Cloudflare R2.

## Upstream

The core crawler scripts under `pixiv_collection/` are derived from:

- [orilights/python_scripts](https://github.com/orilights/python_scripts), `pixiv_collection`

The upstream project is licensed under the MIT License. Its original copyright notice is preserved in `LICENSE`.

## Changes In This Repository

This repository adds and maintains:

- `pixiv_collection/pipeline.py`
- Cloudflare R2 incremental upload support
- local archive validation reports
- full-page bookmark scanning defaults
- recovery-oriented duplicate filename handling
- setup and operation documentation

The R2 publishing workflow is intentionally kept here as a personal deployment pipeline rather than proposed upstream, because it depends on a specific PixivCollection + Cloudflare R2 hosting setup.
