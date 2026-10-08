---
name: download-papers
description: Download research-paper PDFs from DOIs, article or PDF URLs, and batch lists using Unpaywall, Sci-Hub, publisher pages, or an institutional session; optionally extract raw full-text Markdown. Use for standalone acquisition; journal backfills and Zotero synchronization belong to sync-journals-to-zotero.
---

# Download Papers

面向用户的安装、命令示例与常见问题见[使用说明](README.md)。

下载用户指定的论文，交付本地 PDF 和逐篇结果清单；用户需要时，可额外提取原始全文 Markdown。支持 DOI、doi.org 链接、论文页面、PDF 直链及每行一个输入的文本清单。仅有标题时，先通过公开学术来源核对标题、作者、年份并找到 DOI 或原始论文链接；不要把标题直接交给脚本，也不要以首个模糊匹配代替用户指定的论文。

## 默认执行

使用 `scripts/download_papers.py`，路径相对于本 skill。优先使用用户指定目录；未指定时用当前工作目录下的 `downloads/papers/`，不要写入 Obsidian 的 `Research/`。沿用同一输出目录可恢复任务。只在用户要求时导入 Zotero、生成笔记或扩大论文范围。

首次安装或换机器时，先看[环境与依赖](references/setup.md)。使用已有的 Python 3.10+ 环境；以下 `SKILL_DIR` 应指向实际安装目录：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/download-papers"
python "$SKILL_DIR/scripts/download_papers.py" '10.1038/nphys1170' --output ./downloads/papers
python "$SKILL_DIR/scripts/download_papers.py" --input ./dois.txt --output ./downloads/papers
```

这些是调用示例，不要执行示例 DOI 来代替用户的论文。普通 HTTP 下载只依赖 `requests`；动态页面另需 Playwright 与其 Chromium。InstSci 仅用于机构下载，MinerU 仅用于用户要求的全文提取；它们不是普通下载的前置条件。

DOI 下载按用户指定的默认顺序执行：**Unpaywall → Sci-Hub → Crossref 提供的出版社公开页面或 DOI 落地页**。取得 PDF 即停止后续来源。直接文章/PDF URL 沿用该地址，不从任意网址猜测 DOI。详细配置和来源记录见 [下载来源](references/download-sources.md)。

有用户提供的联系邮箱时，用 `--email EMAIL`、已有 `UNPAYWALL_EMAIL` 或兼容的 `PAPER_DOWNLOAD_EMAIL` 查询 Unpaywall。没有邮箱就记录跳过并继续 Sci-Hub；用户已经选择暂不配置时，不反复询问、不编造邮箱、不读取 `.env` 或其他应用配置。仓储稿、预印本与正式出版稿可能不同，应按清单中的版本信息报告；Sci-Hub 来源的版本默认未知。

正常出版社动态页面使用独立、临时的无头 Chromium；`--http-only` 禁用浏览器。Sci-Hub 使用 HTTP 获取页面和明确的 PDF 链接，不启动浏览器或轮换镜像。遇到 HTTP 401/403/429 或验证码时停止该候选，不换身份、代理或浏览器绕过；保留原因并继续后续来源。没有成功文件的输入记录为待处理。

## 机构订阅下载

前述来源未取得 PDF，且用户要求使用机构订阅时，读取[机构下载接续](references/institutional-access.md)，使用 `scripts/download_institutional.py`。用户明确要求测试机构登录或复用已有机构会话时，可以直接进入此模式。

机构入口默认使用包内 `vendor/instsci/` 引擎，无需原机器的源码目录；首次使用按[环境与依赖](references/setup.md)安装其 Python 依赖。默认通过专用 profile 无头执行；遇到 SSO、MFA 或挑战页面时，用同一 profile 可见重启，由用户完成交互。可见阶段完成相关批次，下次命令重新从无头开始。普通网络错误不触发可见登录。

保留专用浏览器原生的密码保存与自动填充能力，由用户在浏览器中选择保存；会话过期后可能仍需重新登录或 MFA。代理不读取密码、个人浏览器或钥匙串，也不自动提交登录表单。不要用通用下载器或应用内浏览器的 `downloadMedia` 替代 Oxford 等出版社的专用 PDF 捕获逻辑。

## 可选全文 Markdown

只有用户要求提取全文时才启用。下载入口可以在下载 PDF 后接着调用 MinerU；也可以对已有本地 PDF 单独提取。完整命令、输出清单和恢复规则见[全文 Markdown 提取](references/markdown-extraction.md)。

```bash
python "$SKILL_DIR/scripts/download_papers.py" '10.1038/nphys1170' \
  --output ./downloads/papers --to-markdown \
  --md-output ./downloads/papers/markdown \
  --markdown-backend mineru --markdown-timeout 900

python "$SKILL_DIR/scripts/extract_markdown.py" \
  --input-manifest ./downloads/papers/manifest.json \
  --output ./downloads/papers/markdown
```

下载与提取分别维护 `manifest.json` 和 `markdown-manifest.json`。提取失败不会撤销或改写已成功的 PDF 结果。原始全文 Markdown 只保存解析结果，不生成笔记、不写入 `Research/`，也不调用 DeepSeek、Zotero 或 graph。

默认 MinerU backend 使用 `vlm`，保留服务返回的图片和公式资源。进程环境必须预先设置 `MINERU_API_TOKEN`；脚本不读取 `.env`，也不要在聊天中索取或粘贴 token。不会自动降级到本地解析。用户显式选择 `--backend local` 或 `--markdown-backend local` 时，PyMuPDF 仅提取简单逐页文本，图片和公式布局不保证。

## 范围、恢复与报告

- `--dry-run` 仅规范化输入并显示计划，不联网、不创建输出文件；与 `--to-markdown` 同用时也只计划下载和提取，不请求 MinerU、不读取 token、不写 PDF 或 Markdown。用户已明确给出论文与目录时可直接执行，不强制额外确认。
- 输入按 DOI 或 URL 去重。成功结果逐篇保存到输出目录的 `manifest.json`；相同输入且已下载文件仍存在时跳过，缺失文件可重新尝试。不要同时启动两个任务写同一输出目录。
- PDF 仅做文件签名检查，防止把 HTML 错误页面保存成 PDF。不默认做全文识别、页数、哈希或论文内容审计。`downloaded` 表示取得 PDF 文件，不表示已独立核实正文与目标论文一致；身份依据及来源见清单。
- 完成时报告本次请求总数、下载成功、已有跳过和仍待处理的数量，给出输出目录及 `manifest.json` 链接。退出码 2 表示存在待处理项，不是全部失败。逐条说明阻塞原因，避免把“没有找到公开版本”说成“这篇论文不存在”或“出版社不支持下载”。
- 这是独立下载入口；不读取或更新期刊同步的 `state.jsonl`，不操作 Zotero collection，不触发 Obsidian 笔记或建图。用户另外要求这些操作时再使用对应 skill。
