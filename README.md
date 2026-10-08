# 论文下载使用说明

本技能按 DOI、论文页面或 PDF 地址下载论文 PDF，支持批量输入、机构订阅和可选的全文 Markdown 提取。结果保存在指定目录并写入恢复清单；默认不生成笔记、不写入 Obsidian 研究目录，也不调用 Zotero。

GitHub 源码仓库：<https://github.com/neowangzc/download-papers>。仓库中的安装包为 `dist/download-papers.skill`，校验文件为 `dist/download-papers.skill.sha256`。包内已包含 InstSci 运行源码与原始 MIT 许可；Python 依赖和浏览器程序在首次使用时安装，不包含账号密码、登录状态或论文文件。

## 在 Codex 中调用

安装后可用自然语言调用，例如：

```text
$download-papers 把 DOI 10.1093/sf/soag145 下载到 ./downloads/papers。
```

也可说明要用机构订阅、出版社和自己的订阅机构；机构入口不会由普通下载自动触发。

## 安装技能

技能包以 ZIP 格式分发。解压后应有顶层目录 `download-papers/`（含 `SKILL.md`、`scripts/`、`references/` 和 `vendor/instsci/`），将其放入 Codex 技能目录：

```text
~/.codex/skills/download-papers/
```

若设置 `CODEX_HOME`，则使用：

```text
$CODEX_HOME/skills/download-papers/
```

不要把 ZIP 文件本身当作技能目录；已有同名安装时先备份或移走旧目录。

## Python 环境与依赖

需要 Python 3.10+。优先使用现有可运行环境；缺少依赖时只补装对应功能的包。

如果没有合适的环境，可在技能目录中创建独立环境：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/download-papers"
uv venv "$SKILL_DIR/.venv" --python 3.10
source "$SKILL_DIR/.venv/bin/activate"
```

普通 HTTP 下载只需要基础依赖。需要出版社动态页面时再安装 Playwright 与 Chromium：

```bash
uv pip install -r "$SKILL_DIR/requirements.txt"
uv pip install playwright
python -m playwright install chromium
```

机构引擎源码已经包含在 `vendor/instsci/`。在已激活的 Python 环境中安装其项目依赖：

```bash
uv pip install "$SKILL_DIR/vendor/instsci"
```

Playwright 与 InstSci CloakBrowser 使用独立浏览器；后者会在首次启动时准备自己的运行文件。

机构依赖中已包含 PyMuPDF。如果仅使用普通下载和本地 Markdown，可单独运行 `uv pip install 'pymupdf>=1.23.0'`。

若安装位置不同，请将 `SKILL_DIR` 改成实际目录。下文假定它已设置且 Python 环境已准备好。

## 下载论文

### 单篇 DOI

```bash
python "$SKILL_DIR/scripts/download_papers.py" \
  '10.1093/sf/soag145' \
  --output ./downloads/papers
```

DOI 可直接输入，也可用 `doi:` 或 `https://doi.org/...` 形式。普通下载依次尝试 Unpaywall、Sci-Hub、出版社公开候选，取得 PDF 后停止。

### 批量下载

建立 UTF-8 文本文件，每行写一个 DOI、doi.org 链接、论文页面 URL 或 PDF URL；空行和以 `#` 开头的注释行会被忽略。

```text
10.1093/sf/soag145
https://doi.org/10.1038/nphys1170
# 也可以放论文页面或 PDF 直链
```

然后运行：

```bash
python "$SKILL_DIR/scripts/download_papers.py" \
  --input ./dois.txt \
  --output ./downloads/papers
```

重复输入会去重。复用同一输出目录可跳过已有文件并继续未完成项。

### Unpaywall 联系邮箱

Unpaywall 需要联系邮箱。可以在命令中传入：

```bash
python "$SKILL_DIR/scripts/download_papers.py" \
  '10.1093/sf/soag145' --email 'you@example.edu' \
  --output ./downloads/papers
```

也可以在当前进程环境中设置 `UNPAYWALL_EMAIL`。没有设置邮箱时，脚本跳过 Unpaywall，继续尝试 Sci-Hub 和出版社公开来源；邮箱不是普通下载的必填项。脚本不会从 `.env` 自动读取邮箱。

### 禁用 Sci-Hub

如果本次不想查询 Sci-Hub，可加 `--no-scihub`：

```bash
python "$SKILL_DIR/scripts/download_papers.py" \
  '10.1093/sf/soag145' --no-scihub \
  --output ./downloads/papers
```

直接 URL 会沿用该地址，不猜测 DOI，也不自动转入机构路线。普通下载不会替你完成登录或验证码。

## 机构订阅下载

机构下载是单独入口，不会由普通下载失败自动触发。准备 DOI 清单，并指定出版社、自己的订阅机构和输出目录：

```bash
python "$SKILL_DIR/scripts/download_institutional.py" \
  --input ./dois.txt \
  --publisher oxfordacademic \
  --institution 'Tohoku University' \
  --output ./downloads/papers/institutional
```

`Tohoku University` 仅为示例，请替换成你有订阅权限的机构。出版社标识须为引擎支持值；本例为 Oxford Academic。

机构入口默认先用专用 profile 无头运行，以复用此前登录会话。若检测到需要 SSO 登录、MFA 或交互验证，会为相关 DOI 接续打开可见浏览器窗口；请由你本人完成登录或验证。你也可以用 `--visible` 从开始就显示窗口。

专用 profile 固定保存在 `~/.local/share/download-papers/institution-profile`。登录会话可以在仍有效时复用，但会过期，也可能因学校策略、出版社或验证流程而要求重新登录。此 profile 与个人 Chrome、其他浏览器和 Codex 应用内浏览器相互独立。

会话复用与浏览器保存密码是两项功能。是否保存由你决定；脚本保留浏览器原生保存与自动填充功能，不会读取或导出密码，也不保证之后自动填入。首次可见登录后默认留出 30 秒供你点击浏览器的“保存密码”；已经保存时可加 `--password-save-wait 0` 取消等待。MFA、验证码由你本人完成。

机构输出和普通下载输出使用不同目录结构与清单格式。不要把机构清单当作普通下载器的 `manifest.json` 使用；需要提取 Markdown 时，直接把机构完成清单传给提取脚本即可。

## 提取全文 Markdown

### 与普通下载联动

在普通下载命令上加入 `--to-markdown`。Markdown 默认使用 MinerU，输出到 PDF 输出目录中的 `markdown/` 子目录：

```bash
python "$SKILL_DIR/scripts/download_papers.py" \
  '10.1093/sf/soag145' \
  --output ./downloads/papers \
  --to-markdown
```

MinerU 会将 PDF 上传到其解析服务，需要在当前进程环境中预先设置 `MINERU_API_TOKEN`。脚本不会从 `.env` 加载 token；不要将 token 写入仓库或发送到聊天。没有 token 时，提取会记录为待处理；不会自动切换到本地解析。

可用 `--md-output` 指定其他 Markdown 目录，也可用 `--markdown-backend local` 选择本地逐页文本提取。该模式需 PyMuPDF，适合简单文本 PDF，不保证还原图片、公式、表格或复杂版面；MinerU 会保留服务返回的图片与公式结构。

### 对已有 PDF 单独提取

直接传入一个或多个本地 PDF。下例中的 `paper.pdf` 需替换为实际文件名：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" \
  ./downloads/papers/paper.pdf \
  --output ./downloads/papers/markdown
```

也可以读取普通下载清单或机构下载完成清单。相对 PDF 路径按清单文件所在目录解析：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" \
  --input-manifest ./downloads/papers/manifest.json \
  --output ./downloads/papers/markdown
```

机构清单示例：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" \
  --input-manifest ./downloads/papers/institutional/complete/manifest.json \
  --output ./downloads/papers/markdown
```

每篇论文写入独立目录，通常含 `full.md` 与 `images/`。这是原始解析结果，不生成笔记、不写入 Obsidian 研究目录，也不调用 Zotero、DeepSeek 或 graph。

## 输出、恢复与退出码

普通下载的 PDF 直接放在 `--output` 根目录，清单为 `manifest.json`（没有 `pdfs/` 子目录）。联动 Markdown 另有 `markdown/` 与 `markdown-manifest.json`。

机构下载把最终 PDF 放在 `OUTPUT/complete/pdfs/`，清单放在 `OUTPUT/complete/manifest.json`；中间阶段结果保存在 `OUTPUT/stages/`。全文提取的 Markdown 清单在提取输出目录中的 `markdown-manifest.json`。

恢复时沿用相同输入和输出目录。各入口会跳过仍存在的成功结果；不要并发写入同一目录。

退出码含义按入口区分：

| 入口 | 退出码 0 | 退出码 1 | 退出码 2 |
| --- | --- | --- | --- |
| 普通下载 | 输入均已下载或已有可用文件 | 通常不使用 | 有下载待处理、无效输入或联动 Markdown 失败；清单错误也可能为 2 |
| 机构下载 | 请求的 DOI 均为已验证成功 | 输入、依赖或运行错误 | 仍有 missing 项；未验证匹配的文件也不计为成功 |
| 独立 Markdown 提取 | 输入均已提取或已有完整结果 | 有提取待处理 | 输入清单错误或参数错误 |

一体化普通下载即使 PDF 已成功，只要 Markdown 提取有失败，整体命令也会返回 2。查看输出 JSON 中的 `markdown` 部分和 `markdown-manifest.json`，可以区分 PDF 与提取状态；提取失败不会撤销 PDF。

## 常见问题

**没有 Unpaywall 邮箱还能下载吗？** 可以。Unpaywall 会被跳过，脚本继续尝试 Sci-Hub 和出版社公开候选。之后仍未获得文件时，可按需单独选择机构订阅入口。

**普通下载失败后会自动弹出机构登录吗？** 不会。普通下载器与机构下载器是不同入口。需要机构订阅时，明确运行 `download_institutional.py` 并指定机构。

**为何机构模式打开了浏览器？** 无头阶段发现需要身份验证或交互挑战时，脚本会为相关 DOI 显示专用浏览器。由你完成登录、MFA 或验证码。

**`--to-markdown` 提示缺少 token？** 在启动脚本的同一终端进程环境中设置 `MINERU_API_TOKEN`，或者显式选择 `--markdown-backend local` 并安装 PyMuPDF。

**退出码为 2 是不是全部失败？** 不一定。它表示存在待处理项，或联动提取未完成。检查对应清单中的逐篇状态；已经成功取得的 PDF 会保留。

**PDF 下载成功就代表论文身份已经核实吗？** 不一定。普通下载器检查 PDF 文件签名，不等于独立确认全文与目标论文完全一致。机构清单也会区分已验证与未验证的结果。

## 源码维护与重新打包

GitHub 源码仓库额外提供 `tests/` 和 `tools/package_skill.py`；这些开发文件不放入 `.skill` 安装包。克隆仓库后，在仓库根目录、已激活的虚拟环境中运行：

```bash
uv pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python tools/package_skill.py
```

打包脚本按固定文件范围生成 `dist/download-papers.skill` 和 SHA-256 校验文件，排除浏览器二进制、虚拟环境、缓存和运行数据。更新技能时保留 `~/.local/share/download-papers/institution-profile`，不需要重新初始化登录目录；换机器安装则需首次登录。
