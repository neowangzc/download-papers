# 机构下载接续

仅在用户要求使用机构订阅时进入此流程。使用 `scripts/download_institutional.py` 复用 InstSci 出版社下载引擎；无需运行期刊同步，不写入 Zotero。用户明确要求测试机构登录时，可以直接走这条路线。

## 默认无头，按需显示登录窗口

入口固定使用下载 skill 的专用 profile：`~/.local/share/download-papers/institution-profile`。浏览器自行在该目录保存持久会话数据，后续任务复用。无头和可见启动均使用 `--restore-last-session`，保留 `Sessions` 等恢复信息，不调用底层引擎的 `_purge_session_restore`；后者会妨碍会话 cookie 跨重启恢复。不导出 Cookie，不读取个人 Chrome 或其他应用的会话。不要并发启动多个使用同一 profile 的任务。

1. 默认无头运行，先尝试复用已有机构登录。
2. 遇到机构登录、MFA 或人机验证时，结束该无头阶段并关闭浏览器上下文。
3. 用同一专用 profile 启动可见窗口，仅继续需要交互的 DOI。由用户完成登录或验证；不要自动解答验证码。
4. 可见阶段沿用同一活动上下文完成相关批次，下次命令重新从无头开始。当前实现不会在用户刚完成登录时立即隐藏窗口，以免再次重启丢失仅存在于内存中的会话。

持久 profile 能复用仍有效的登录状态，但学校 MFA 策略、跨出版社、服务端过期或只在当前进程有效的会话仍可能要求再次登录。应用内浏览器与此下载浏览器是不同会话，不能直接搬运登录状态。

## 保存账号密码

脚本启用专用 profile 的密码保存功能，并移除自动化默认的 `--password-store=basic` 与 `--use-mock-keychain`，让 Chromium 使用操作系统支持的原生密码存储。是否保存由用户在浏览器中选择；分发包不包含任何用户的登录状态或保存的账号密码。此调整只作用于下载进程，不改写共享依赖或个人浏览器配置，也不启用云同步。

用户首次在可见窗口登录后，点击浏览器自身的“保存密码”。之后登录状态过期时仍需重新登录，但账号密码可由专用浏览器自动填充；用户确认登录并完成学校要求的 MFA。首次登录成功后默认留出 30 秒供用户保存密码，`--password-save-wait 0` 可取消等候。若操作系统要求授权 Chromium 使用安全存储，由用户处理系统提示。

保存账号密码和保存登录会话是两项不同能力：前者避免反复手输，后者减少重新登录。代理不读取、导出或记录密码，不把密码写入脚本、环境变量、下载清单或报告；不要在聊天中索取密码，也不自动提交登录表单。不得声称密码已经保存，除非用户确认或浏览器保存操作已有证据。

该包装脚本覆盖底层引擎的浏览器启动及交互交接方法，未改写底层引擎。用户要求的无头模式适用于正常下载；需要用户操作时窗口保持可见。普通网络错误不触发可见重试。不要把单篇结果扩大成整个出版社支持或不支持的结论。

## 调用

先按[环境与依赖](setup.md)准备运行环境。默认源码路径是本 skill 内的 `vendor/instsci/`，已包含经本机实测的引擎和出版社适配修改；`--engine-dir` 仅用于显式替换兼容引擎。执行前读取引擎中的 `instsci/data/institutional_identity_policy.json`，确认本次使用的是用户自己的订阅机构；若使用外部引擎，也遵循其适用的 `AGENTS.md`。当前会话已确认的机构可以复用，无需再次询问。不要自动安装其他同名包或读取其他配置中的凭据。

按出版社分组准备 DOI 清单，一行一个 DOI 或 doi.org 链接。使用当前确认的出版社标识和机构名：

```bash
python "$SKILL_DIR/scripts/download_institutional.py" --input /absolute/path/dois.txt --publisher oxfordacademic --institution '用户自己的订阅机构' --output /absolute/path/downloads/papers/institutional-run
```

`--visible` 从一开始显示窗口；`--dry-run` 检查输入及可续跑数量，不启动浏览器、不写文件。`--login-timeout` 默认 180 秒，`--pdf-timeout` 默认 45 秒。需要登录时及时告知用户实际出现窗口的位置；超时后保留待处理状态，不反复重试身份验证。若更换本机引擎源码位置，使用 `--engine-dir` 指向已确认的源码目录。

同出版社多篇 DOI 在同一上下文中顺序处理，以复用登录。Oxford 继续使用已有 `_capture_oxford_pdf_direct`，从文章 `citation_doi` 和 `citation_pdf_url` 捕获 PDF；不要替换为应用内浏览器 `downloadMedia` 或只等待通用下载事件的点击逻辑。

## 恢复与输出

复用相同 `--output` 即可续跑，成功记录对应的 PDF 仍存在时跳过。原始阶段结果保存在 `stages/<run-id>/headless/` 或 `visible/`；最终文件在 `complete/pdfs/`，清单在 `complete/manifest.json`。文件名冲突时另建文件，不覆盖已有 PDF；损坏清单会停止并保留原文件。无头阶段在显示登录窗口前保存 checkpoint，子集运行保留同目录的其他历史记录。

`success` 表示底层引擎返回成功、PDF 存在且通过其 DOI 匹配检查；`unverified` 表示取得文件但匹配未确认；`missing` 表示仍待处理。退出码 2 表示本次有未完成项，1 表示运行错误。不要手工把失败改成成功，或将该清单混写成普通下载器的 `items` 格式。

需要全文 Markdown 时，在机构下载后对成功清单调用提取器：

```bash
python "$SKILL_DIR/scripts/extract_markdown.py" --input-manifest /absolute/path/downloads/papers/institutional-run/complete/manifest.json --output /absolute/path/downloads/papers/markdown
```

提取器只读取成功 PDF，按 MinerU 路线生成全文与图片，不生成笔记。详见[全文 Markdown 提取](markdown-extraction.md)。

持久上下文机制参考 [Playwright 官方文档](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)。
密码相关启动参数参考 [Chrome 官方工具参数说明](https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md)。
