# 下载来源配置

DOI 按 **Unpaywall → Sci-Hub → 出版社公开候选** 顺序处理，前一步成功后不请求下一步。三种来源都未成功时保留 `needs_attention`，机构订阅接续使用单独的 InstSci 入口。直接 URL 不进入 DOI 来源链。

## Unpaywall

- `--email EMAIL` 优先于环境变量；环境变量依次读取 `UNPAYWALL_EMAIL`、`PAPER_DOWNLOAD_EMAIL`。
- 邮箱作为 Unpaywall API 的联系参数发送。无邮箱不调用该 API，结果写入 `unpaywall_lookup_skipped:no_email`。
- 优先尝试 `best_oa_location`，然后去重后的其他 `oa_locations`，保留 `version` 和 `host_type`。一次论文最多六个候选。
- 未查询、查询失败、没有开放版本与 PDF 下载失败是不同情况，按实际状态报告。

## Sci-Hub

- 默认在 Unpaywall 之后尝试；`--no-scihub` 可以关闭。只用于明确 DOI，不能把标题或任意网页地址传给它。
- `--scihub-url` 优先于 `SCIHUB_BASE_URL`；默认 `https://sci-hub.mk`，来自下述参考项目当时的首个镜像配置。域名可变，默认值不代表已确认长期在线。
- 一次运行只使用配置的单个站点，不自动扫描或轮换镜像。支持页面里明确的 PDF iframe、embed/object、下载链接及静态下载按钮地址；不执行页面按钮脚本。
- HTTP 拒绝、验证码或站点不可用记入候选结果，不启动无头浏览器绕过。没有找到 PDF 不等于该论文不存在。
- `source=scihub` 单独记录，版本未知，不归类为 Unpaywall 开放版本。只检查 PDF 文件签名，DOI 查询依据不等于独立核实正文身份。

```bash
# 默认按 Unpaywall → Sci-Hub → publisher；邮箱可省略
python "$SKILL_DIR/scripts/download_papers.py" --input ./dois.txt \
  --output ./downloads/papers --email "$UNPAYWALL_EMAIL"

# 本次关闭 Sci-Hub
python "$SKILL_DIR/scripts/download_papers.py" --input ./dois.txt \
  --output ./downloads/papers --no-scihub

# 使用用户指定的站点，仅替换本次参数
python "$SKILL_DIR/scripts/download_papers.py" --input ./dois.txt \
  --output ./downloads/papers --scihub-url "$SCIHUB_BASE_URL"
```

沿用已有输出目录时，已成功的论文会跳过，不会仅为比较来源重复下载。新增来源的验证须与已有机构下载结果区分。

## 参考实现

用户指定的 [paper-download-mcp](https://github.com/Oxidane-bot/paper-download-mcp) 采用多来源下载。本 skill 参考其来源职责与静态 PDF 地址解析方式，使用自身下载器和清单，不要求安装整个 MCP，也不修改 Codex 全局 MCP 配置。

审查版本：`9427dfee720ba2bcedd37856d383855de46e1d3e`。

- [Sci-Hub 来源](https://github.com/Oxidane-bot/paper-download-mcp/blob/9427dfee720ba2bcedd37856d383855de46e1d3e/src/paper_download_mcp/scihub_core/sources/scihub_source.py)
- [镜像配置](https://github.com/Oxidane-bot/paper-download-mcp/blob/9427dfee720ba2bcedd37856d383855de46e1d3e/src/paper_download_mcp/scihub_core/config/mirrors.py)
- [Unpaywall API](https://unpaywall.org/products/api)

上游 MCP 启动要求邮箱，且含其他来源和镜像回退；本 skill 依照用户顺序独立实现，无邮箱仍可使用其他来源。
