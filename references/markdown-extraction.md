# 全文 Markdown 提取

本功能把已下载的本地 PDF 转成原始全文 Markdown。它和 PDF 下载各自维护恢复状态：下载清单是 `manifest.json`，提取清单是输出目录中的 `markdown-manifest.json`。提取失败只表示该 PDF 的 Markdown 尚未提取成功，不会改变 PDF 下载成功状态，也不会删除 PDF。

## 随下载提取

在下载命令上显式启用提取：

```bash
python "$SKILL_DIR/scripts/download_papers.py" '10.1038/nphys1170' \
  --output ./downloads/papers \
  --to-markdown \
  --md-output ./downloads/papers/markdown \
  --markdown-backend mineru \
  --markdown-timeout 900
```

省略 `--md-output` 时，Markdown 输出到下载目录的 `markdown/` 子目录。PDF 和 Markdown 的结果各自写入对应清单；复用下载目录可恢复 PDF 下载和 Markdown 提取。

## 从已有 PDF 提取

可以直接指定一个或多个本地 PDF：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" \
  ./downloads/papers/paper.pdf \
  --output ./downloads/papers/markdown
```

也可以把下载 manifest 或 InstSci 完成清单作为输入。下载清单读取 `items` 中 `status: downloaded` 的 `path`；InstSci 清单读取 `status: success` 的 `pdf_path`。相对 PDF 路径按输入清单所在目录解析：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" \
  --input-manifest ./downloads/papers/manifest.json \
  --output ./downloads/papers/markdown
```

每篇论文写入独立目录，包含 `full.md` 和 MinerU 返回的 `images/` 资源。提取清单记录输入 PDF、解析 backend、结果状态、Markdown 路径及图片相对路径。已有完整成功结果会跳过；缺少 Markdown 或记录的图片时会重新提取，并写入不覆盖旧目录的新后缀目录。限流、解析失败、缺少 token、扫描件无可提取文本等情况会标为待处理；不会把解析失败误报为 PDF 下载失败。

## Backend 与凭据

默认 backend 为 MinerU，模型默认为 `vlm`，保留服务返回的图片和 Markdown 公式结构。运行时 PDF 会上传到 MinerU 解析，接口依据 [MinerU 官方文档](https://mineru.net/doc/docs/index_en/)。请求需要 `MINERU_API_TOKEN` 已设置在当前进程环境中；脚本不自动读取 `.env`。不要在聊天中索取或粘贴 token，也不要把 token 或 MinerU 签名 URL 写入清单、报告或日志。没有 token 时记录待处理，不发起云端请求。

每篇解析默认最多轮询 900 秒，上传与单次网络请求另有超时。轮询超时后保存 `batch_id`，下次可继续等待同一任务；强制中断或提交后网络异常不保证已有任务可恢复。解析会输出上传和状态进度。

本地解析仅在用户显式选择时运行：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" ./paper.pdf \
  --output ./downloads/papers/markdown --backend local
```

本地模式使用已安装的 PyMuPDF 提取逐页文本，适合简单文本 PDF；不能保证图片、公式、表格或复杂版面的还原。MinerU 失败时不会自动切换到 local。

## 边界与 dry-run

`--dry-run` 只显示计划，不联网、不读取 MinerU token、不创建 PDF 或 Markdown 文件；即使下载命令同时带 `--to-markdown` 也保持只计划。原始 Markdown 只作为全文解析材料，不会生成文献笔记、不写入 Obsidian `Research/`，不调用 DeepSeek 或 Zotero，也不触发 graph 更新。
