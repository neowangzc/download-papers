# 安装与运行环境

`.skill` 是 ZIP 格式的技能包，顶层目录为 `download-papers/`。解压后将该目录放到 `${CODEX_HOME:-$HOME/.codex}/skills/`，或使用目标应用的技能导入功能。已有同名目录时先保留旧版本，不覆盖用户配置。后续命令中的 `SKILL_DIR` 指向实际安装位置。

包内包含说明、Python 脚本、基础依赖清单和完整 InstSci 运行源码。Python 环境、浏览器二进制、PDF、运行日志、登录 profile、账号密码和 API token 不随包提供。安装包不会迁移机构登录状态；在另一台机器上需首次登录。

## 基础下载

使用 Python 3.10+。优先复用用户当前虚拟环境；新环境可按下例创建：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/download-papers"
uv venv "$SKILL_DIR/.venv" --python 3.10
source "$SKILL_DIR/.venv/bin/activate"
uv pip install -r "$SKILL_DIR/requirements.txt"
python "$SKILL_DIR/scripts/download_papers.py" --help
```

不要为已有可用环境重复安装。`requests` 支持 Unpaywall、Sci-Hub 和出版社的 HTTP 下载。Unpaywall 联系邮箱可通过 `--email` 或环境变量配置；未配置时跳过它。配置详情见[下载来源](download-sources.md)。

| 模式 | 额外依赖或配置 |
| --- | --- |
| 出版社动态页面 | `uv pip install playwright`，然后 `python -m playwright install chromium` |
| 机构订阅 | 内置 InstSci 的 Python 依赖与专用 Chromium，见下节 |
| MinerU 全文 Markdown | 当前进程预先设置 `MINERU_API_TOKEN`；提取器使用 Python 标准库 HTTP 客户端 |
| 显式本地 Markdown | `uv pip install 'pymupdf>=1.23.0'`；仅简单逐页文本 |

`--http-only` 禁用普通下载器的动态浏览器。普通下载无需 InstSci 或 MinerU。各模式不会自动读取 `.env`，也不要求配置 Zotero 或笔记生成模型。

## 内置机构引擎

`vendor/instsci/` 已包含带 `publisher_batch` 与 `cloakbrowser_compat` 的实测运行源码，包括 Oxford 论文 PDF 捕获适配。入口默认按技能位置找到内置源码，不依赖原来的 worktree 或另一个同名包。安装包内引擎声明的依赖，然后检查输入：

```bash
uv pip install "$SKILL_DIR/vendor/instsci"
python "$SKILL_DIR/scripts/download_institutional.py" \
  --input ./dois.txt \
  --publisher oxfordacademic --institution '用户自己的订阅机构' \
  --output ./downloads/papers/institutional --dry-run
```

内置 `pyproject.toml` 声明其 Python 依赖，包括 `cloakbrowser` 和 `pymupdf`。首次实际启动时 CloakBrowser 按当前平台下载专用 Chromium，不等同于 Playwright 的普通 Chromium。默认浏览器缓存为 `~/.cache/download-papers/cloakbrowser`，不会写进技能目录；已有缓存可通过 `INSTSCI_CLOAKBROWSER_CACHE_DIR` 指定。若已有 `CLOAKBROWSER_CACHE_DIR`，上游优先使用它。只指定专用浏览器二进制缓存，不得将个人浏览器 profile 当作缓存目录。

需要替换内置引擎时才使用 `--engine-dir /absolute/path/to/compatible/source`。兼容源码需提供 `instsci.config.Config`、`publisher_batch.PaperRecord` / `PublisherBatchDownloader`、`publisher_profiles.get_publisher_profile`、`cloakbrowser_compat.prepare_cloakbrowser_runtime`，以及批量引擎的登录交接、挑战等待和 Oxford PDF 捕获方法。它使用内部扩展接口；引擎升级后应先小批量确认兼容性。原始 MIT 许可和源码快照说明保存在 `vendor/instsci/LICENSE` 与 `vendor/instsci/UPSTREAM.md`。

`--dry-run` 会导入引擎并检查输入、恢复数量，但不启动浏览器，也不能验证浏览器二进制、登录或订阅权限。专用会话目录固定为 `~/.local/share/download-papers/institution-profile`，独立于技能安装目录；更新此 skill 不需要清除该目录。实际登录和恢复流程见[机构下载接续](institutional-access.md)。
