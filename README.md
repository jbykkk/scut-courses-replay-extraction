# SCUT 学习助手

把百步梯的课堂记录和电脑里的课件放到一起。上课时记字幕、收提醒，下课后整理笔记；复习时能找到老师的原话，也能按自己的时间安排先学什么。

项目基于 [jbykkk/scut-courses-replay-extraction](https://github.com/jbykkk/scut-courses-replay-extraction) 开发，保留了原有的学校字幕下载功能。

**[下载 Windows 安装包][setup]** · [安装与日常使用][start] · [WorkBuddy 接入][workbuddy] · [反馈问题][issues]

主要使用环境：**Windows 10/11 x64 + Edge**，浏览器扩展也支持 Chrome。学校课程需要有权访问百步梯的账号；只读本地资料时不需要登录学校网站。

## 先选版本

| 版本 | 适合谁 | 包含什么 |
| --- | --- | --- |
| [0.13.0 Windows 预览包][release] | 想先安装使用 | 桌面入口、实时字幕、回放整理、课程笔记、本地资料问答、复习与导出 |
| [最新开发分支][dev] | 想用刚完成的功能 | 上述功能，以及跨课时问答、今日复习清单、WorkBuddy 接入、公开资料核对 |

**0.13.0 安装包尚不包含开发版新增功能。** 当前 GitHub 默认分支也未合入这些改动；使用新功能请按下方“从源码运行”切换到开发分支。[开发进度见 PR #1][pr]。

![学习助手主界面][study-image]

截图来自实际程序，使用演示数据，不包含个人课程、账号或密钥。主界面为 0.13.0，下面的跨课时问答和今日清单截图来自开发版。

## 安装后怎么用

1. 下载 **[SCUT-Study-Setup-0.13.0.exe][setup]**，选择“安装并打开”。安装包自带 Python 和依赖，不需要先安装开发工具。
2. 以后双击桌面或开始菜单里的 **SCUT 学习助手**。本地服务会按需启动，不设置开机自启，也不需要找 CMD 文件。
3. 先添加一份课件，或者输入要处理的课程。不必一次填完课表、考试日期和所有要求。
4. 需要模型解释、生成笔记时，再到设置里保存自己的 API Key。想使用实时字幕，再配置语音模型和 FFmpeg。

安装包目前是未签名的预发布版本。[下载页][release]也提供带运行环境的 ZIP 和 SHA-256 校验文件；ZIP 解压后打开 `SCUT学习助手.exe` 即可。GitHub 自动生成的 `Source code.zip` 是源码包。

### 需要学校课程时

在学习窗口说：**“处理操作系统尚未处理的每一节回放。”**

如果尚未连接学校，任务会等待。点击任务中的“登录百步梯，继续处理”，在学校页面完成登录，再打开对应课程。第一次读取学校课程，需要在 Edge 或 Chrome 中加载项目的 `extension` 文件夹；连接面板会显示目录和步骤。[详细安装步骤][start]

扩展能够读取课程后，任务会继续：有学校字幕就导入，没有字幕就提取音轨、本地转写，再自动生成笔记。已有结果会跳过。保持对应学校页面打开，供扩展读取课时目录和视频地址。

<details>
<summary>查看学校连接与任务等待界面</summary>

![学校连接和登录入口][school-image]

![等待学校连接的课程任务][task-image]

</details>

### 上课时

在课程播放页点击浏览器工具栏里的扩展图标，再点 **开始实时字幕**。这次点击用于授予当前标签页的声音捕获权限，打开页面本身不会自动录音。

可以同时记录多节课，也可以在直播转写时整理回放。课堂实况显示连续字幕和上下文，重要事项会提醒并保存。下课时点 **结束录音并保存**，剩余笔记在后台继续处理。

**关闭控制面板或学习窗口不会结束录音；关闭学校播放页会中断该页的声音采集。** 想回来查看，重新点扩展图标或桌面入口即可。

## 下课后和复习时

| 想做的事 | 怎么开始 |
| --- | --- |
| 找作业、测验、评分要求和课程安排 | 打开“课堂记录”，按课程查看重要事项，展开原文和时间段 |
| 对照课件、题目和课堂讲解 | 添加文件、截图或本地路径，再问具体问题 |
| 补上没听懂的内容 | 选定课程和主题，让它结合已有资料解释、出一道练习 |
| 定期回顾 | 将主题加入复习，结合之后的作答继续调整 |
| 带走整理结果 | 导出笔记，或要求生成 PDF、Markdown 格式的复习资料与题目 |
| 离开电脑后接收课堂事项 | 在设置中配对手机提醒端，允许通知并开启后台监听 |

资料支持 PDF、PPTX、DOCX、TXT、Markdown 和图片。给出文件夹时会先列出可读文件供选择；原文件只读。图片理解需要支持图片输入的模型。

手机端提供 [Android APK][android]，配对方式见 [手机端说明][mobile]。收到实时提醒需要电脑端继续处理课堂、手机能够访问中继并保持后台监听；电脑正在转写，不代表手机已经连接成功。

### 开发版：带出处的跨课时问答

> 操作系统老师在哪讲过条件变量？给我原句和回看位置。

从已导入的多个课时里找相关讲解，回答附字幕原句、课时、时间段和回看入口。只有相关铺垫、没有直接讲解时，会说明检索到的范围。尚未对齐视频的直播录音保留独立计时，不把录音时间当作视频位置。

<details>
<summary>查看跨课时问答示例</summary>

![跨课时查找课堂原句和回看位置][search-image]

</details>

### 开发版：今日复习清单

> 今晚只有30分钟，先学什么？

结合已确认的作业期限、到期复习和最近作答，给出时间预算内的安排。没有足够记录时先做短诊断，不编造薄弱点、截止日期或考试时间。清单中的一段作业时间只表示推进一部分，不代表整项任务已经完成。

<details>
<summary>查看 30 分钟复习清单</summary>

![按时间预算安排任务和复习][review-image]

</details>

### 开发版：用公开资料核对

> 查公开教材，核对条件变量为什么要在 while 循环里等待。

先找课堂原话，再检索公开教材或论文线索，对照前提、术语和适用范围。当前接入 Crossref、Europe PMC 和部分公开教材入口；论文往往只能取得摘要，回答会说明实际读到的范围。

公开检索只发送概念词。读教材时按问题选取相关段落，保留页码和来源，减少逐页调用。转载同一篇论文不算多个独立来源；检索结果和模型解释不会自动覆盖字幕，也不会自动成为已确认知识。外部资料不能替老师决定考试范围。

## 在 WorkBuddy 里使用（开发版）

WorkBuddy 与学习网页共用本机资料索引。**查已有资料时，直接打开 WorkBuddy 即可，不必先开学习网页或登录百步梯。** 回看学校视频时再去浏览器登录。

先通过开发版学习助手导入课程或资料，然后在项目目录运行一次：

```powershell
.\.venv\Scripts\python.exe scripts\prepare_workbuddy.py --install
```

在 WorkBuddy 的 **专家·技能·连接器 → 连接器 → 自定义连接器** 中检查 `scut-study`，按提示保存配置并确认连接。当前连接器为 **0.4.0，16 个工具**。更新后若仍显示旧数量，重新连接该连接器。新对话输入 `/` 选择 `scut-study`，也可以直接说“使用 SCUT 学习助手”。

可以直接开始一个学习目标：

> 从头讲神经科学。概念旁给英文，把模型之间的联系讲清楚，每次先讲一部分再问我一道题。

> 接着上次的进度。我还没分清那两个模型，先别给答案。

学习会话记住实际讲到的主题、你的回答和下一步问题，换对话时可以接续；明确给出的自评会影响今日复习排序。课件、课堂原句和外部参考保留各自出处，来源更新后会提示重新核对。不会因为“读过了”就把一项记成已掌握，也不会把文字提到的板书当作已经看过的图。

工具负责检索与记录，由 WorkBuddy 自己的模型组织回答，不再重复调用本地 DeepSeek。课堂出处、今日复习和公开教材核对有 WorkBuddy 5.9.3 的历史对话测试；0.4.0 新增学习会话的本轮检查范围见[接入与验证记录][workbuddy]。

当前连接器提供查询、排程、公开资料取证和学习进度记录；录音控制、批量处理、文件导出和任务状态修改继续在学习助手里操作。固定专家团和公开 Agent 链接尚未发布。[设计与实现](planning/WORKBUDDY_AGENT.md) · [完整接入说明与工具列表][workbuddy]

## 模型和个人数据

| 功能 | 需要配置什么 |
| --- | --- |
| 查看已保存的课堂记录 | 本地服务，桌面入口会自动启动 |
| 导入学校已有字幕 | 已登录的学校页面与扩展；不需要语音模型 |
| 本地实时转写、缺字幕回放转写 | Whisper 模型文件、FFmpeg，以及 CPU 或兼容 GPU |
| 学习网页的问答、生成笔记与课堂分析 | 自己的模型 API Key，支持 DeepSeek 和其他兼容服务 |
| 图片识读 | 支持图片输入的模型 |
| WorkBuddy 中查询和回答 | 登录 WorkBuddy，并连接本机 MCP；使用 WorkBuddy 自身的模型 |

安装包不含语音模型权重。已有 PotPlayer / Faster-Whisper 环境时，可以在设置中选择现有文件，无需移动。设置会显示 Key 是否已保存，并提供连接检测；输入框留空不会清除已有 Key。

- **安装版数据**：`%LOCALAPPDATA%\SCUTClassroomAssistant\data`，包含笔记、录音缓存、资料索引和运行记录。
- **源码版数据**：项目的 `.local/`；也可通过 `SCUT_ASSISTANT_HOME` 指定目录。
- **API Key**：单独保存在 `%LOCALAPPDATA%\SCUTClassroomAssistant\credentials.json`，使用当前 Windows 账户加密，与代码分开。

使用模型时，相关文字片段或所选图片会发给配置的模型服务；使用 WorkBuddy 时，工具取回的相关片段会交给 WorkBuddy 的模型。学校 Cookie 留在浏览器。手机提醒经过加密中继传送，发布包不含个人设置、课程内容、密钥或模型权重。

识别和分析都可能出错。出处能帮助回查，不能单独证明内容正确；重要日期、课堂要求和有争议的概念仍需核对原文。助手不会替学生签到、答题或提交作业。

## 查看它是怎么工作的

学习网页的 **运行详情** 可以查看系统提示词、工具参数与结果、引用片段、耗时和用量。WorkBuddy 发起的 MCP 调用也会留下本机记录；这里不展示模型内部的思考过程。

<details>
<summary>查看运行详情</summary>

![提示词、工具调用和耗时记录][runtime-image]

</details>

## 从源码运行

最新学习助手功能目前位于 `codex/student-agent-foundation` 分支。需要 Git、[uv](https://docs.astral.sh/uv/) 和 Windows；回归检查另需 Node.js。

```powershell
git clone --branch codex/student-agent-foundation --single-branch https://github.com/PoesisQ/bbt_SCUT_admin.git
cd bbt_SCUT_admin
uv sync --extra asr --extra study --python 3.12
# 首次创建桌面与开始菜单入口，以后从图标打开
.\install-desktop.ps1
```

仅调试服务时运行 `uv run --extra asr --extra study python -m assistant_service`，本地页面为 `http://127.0.0.1:8765/study.html`。停止服务前先结束录音和处理任务，再运行 `stop-assistant.ps1`。更新源码后重新启动服务，并在浏览器扩展管理页重新加载扩展。

<details>
<summary>检查、打包与原版字幕命令</summary>

```powershell
# Python、JavaScript 回归及格式检查
uv run --extra study python scripts/check.py
# 浏览器扩展 ZIP
uv run python scripts/package_extension.py
# Windows x64 安装包
uv run --extra study python scripts/build_windows.py
```

Windows 包使用 Python 嵌入式运行环境，依赖见 `requirements-windows.lock`；包内附第三方许可和 `THIRD_PARTY.json`。构建步骤见[发行说明][releasing]。

原命令行字幕下载功能仍可使用：

```powershell
uv run playwright install chromium
uv run main.py login
uv run main.py get "课程链接"
uv run main.py get --all "课程链接"
```

</details>

<details>
<summary>常见问题</summary>

- **本地已连接，但课程读不到**：本地服务、浏览器扩展和学校登录是三个状态。打开任务对应的课程播放页，确认已登录且能够播放。
- **字幕已经有了，笔记分析失败**：检查模型配置后继续生成笔记，不必重新转写整节课。
- **关闭窗口后，怎么回去**：重新打开桌面入口或点扩展图标即可；不要为了打开面板而重新加载正在录音的扩展。
- **手机已配对，但收不到提醒**：配对只表示保存了连接信息。检查后台监听、通知权限和网络；VPN 也可能影响中继连接。
- **WorkBuddy 查不到一门课**：先在学习助手里导入并同步资料；它只能检索已建立索引的内容。

反馈问题时请附版本、操作步骤和错误文字，不要提交 API Key、学校 Cookie 或个人数据库。

</details>

[安装与日常使用][start] · [WorkBuddy 接入][workbuddy] · [更新记录][changelog] · [系统设计][design] · [网站接口记录][site] · [验证记录][validation] · [MIT License](LICENSE)

[setup]: https://github.com/PoesisQ/bbt_SCUT_admin/releases/download/v0.13.0/SCUT-Study-Setup-0.13.0.exe
[release]: https://github.com/PoesisQ/bbt_SCUT_admin/releases/tag/v0.13.0
[android]: https://github.com/PoesisQ/bbt_SCUT_admin/releases/download/v0.10.7/SCUT-Classroom-Alerts-v0.10.7.apk
[issues]: https://github.com/PoesisQ/bbt_SCUT_admin/issues
[dev]: https://github.com/PoesisQ/bbt_SCUT_admin/tree/codex/student-agent-foundation
[pr]: https://github.com/PoesisQ/bbt_SCUT_admin/pull/1
[start]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/manual/START.md
[workbuddy]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/workbuddy/README.md
[mobile]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/mobile/README.md
[changelog]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/CHANGELOG.md
[design]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/planning/STUDY_TECHNICAL.md
[site]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/SITE_FINDINGS.md
[validation]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/VALIDATION.md
[releasing]: https://github.com/PoesisQ/bbt_SCUT_admin/blob/codex/student-agent-foundation/manual/RELEASING.md
[study-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/02-study.png
[school-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/03-school.png
[task-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/05-task.png
[search-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/05-classroom-search.png
[review-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/06-today-review.png
[runtime-image]: https://raw.githubusercontent.com/PoesisQ/bbt_SCUT_admin/59ac3f008901249b98858e4ce0f3bd99373101cc/manual/images/04-runtime.png
