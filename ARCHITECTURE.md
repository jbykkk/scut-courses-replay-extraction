# 项目实现说明

## 概述

本工具从华南理工大学“华园视频”平台提取课程回放字幕，同时支持校内站点 `video.jw.scut.edu.cn` 和 WebVPN 校外站点 `video-jw-443.webvpn.scut.edu.cn`。华园视频在播放课程时，前端通过 API 拉取字幕 JSON 数据，无需逆向加密或破解流媒体协议。对于平台没有生成字幕的课时，CLI 和浏览器插件均可单独从普通 MP4 回放提取音轨，供后续 ASR 使用。

## 双站点模型

两个站点使用相同的课程页面和 `/courseapi/...` 相对路径，但 origin 与 Cookie 归属不同。程序把站点配置、原始课程 URL、`course_id`、`sub_id` 和 `tenant_code` 组成 `CourseUrl`，后续请求始终继承输入链接所属的 origin。

课程 URL 使用标准库解析并受域名允许列表约束。单课抓取直接打开原始链接；批量抓取只替换 `sub_id`，继续使用相同 origin、`course_id` 和 `tenant_code`。

## 核心发现

字幕接口：
```
GET /courseapi/v3/web-socket/search-trans-result?sub_id={sub_id}&format=json
```

返回结构：
```json
{
  "code": 0,
  "msg": "获取同传结果成功",
  "total": 1,
  "list": [{
    "all_content": [
      {"BeginSec": 93, "Text": "内容", "TransText": "translation", "EndSec": 97},
      ...
    ],
    "sub_id": "554146",
    "resource_guid": "..."
  }]
}
```

关键字段：`BeginSec`（起始秒数）、`EndSec`（结束秒数）、`Text`（中文文本）、`TransText`（英文翻译）。

## 两种实现方式

### CLI 工具（Python + Playwright）

**原理**：Playwright 启动浏览器 → 扫码登录 → 打开课程播放页 → `page.on("response")` 拦截网络响应 → 匹配 `search-trans-result` URL → 提取 JSON。

**技术栈**：
- `playwright` — 浏览器自动化与网络拦截
- `asyncio` — 异步运行时
- `uv` — 包管理与虚拟环境

**模块职责**：
| 文件 | 职责 |
|------|------|
| `main.py` | CLI 入口，argparse 命令路由 |
| `browser.py` | Playwright 浏览器管理、网络拦截、API 直接调用 |
| `subtitle.py` | JSON → SRT/TXT 格式转换、文件保存 |
| `audio.py` | 缺字幕报告读取、媒体选择、ffmpeg 音轨提取、ffprobe 校验与媒体清单 |
| `config.py` | URL、路径、超时等常量 |
| `auth.py` | 登录状态持久化（Playwright `storage_state`） |

**关键实现细节**：

1. **登录**：`login --site internal|external` 打开指定入口并完成学校认证，默认保持旧版行为使用 `internal`。登录成功后分别保存为 `data/auth_state_internal.json` 和 `data/auth_state_external.json`，避免不同域名的 Cookie 相互覆盖。校内模式会在新文件不存在时兼容读取旧的 `data/auth_state.json`。

2. **字幕拦截**：必须通过 `livingroom` 页面触发字幕 API 请求。Playwright 用 `page.on("response")` 监听所有响应，只接受当前站点 hostname、精确字幕 API path 且查询参数 `sub_id` 与当前任务一致的响应，避免保存页面预加载的其他课时字幕。

3. **JSON 解析**：API 返回多层嵌套（`body.list[].all_content[]`），`extract_subtitle_items()` 递归展开直到找到同时包含 `BeginSec` 和 `Text` 字段的字幕数组。

4. **课程目录**：先打开当前站点 origin，再通过 `page.evaluate()` 调用相对路径 `/courseapi/v2/course/catalogue?course_id=xxx`。浏览器自动把相对路径解析到校内或校外 origin。

5. **回放音频**：`audio` 支持课程链接、`--course-id`/`--sub-id`、插件的 `字幕导出结果.json`；报告默认仅选 `no_subtitle`，`--include-errors` 加入 `error`。所有 ID 在浏览器启动前校验，输入课程和站点冲突时拒绝执行。`browser.get_replay_info()` 在目标 origin 内携带登录 Cookie 请求 `get-sub-info`，超时为 20 秒，并检查 HTTP、业务状态和 JSON 结构。

   媒体选择顺序为 `content.save_playback.contents` → `content.playback.url` → `video_list`（优先 `type=3` 教师画面）→ `playurl`。仅接受 HTTP(S) 地址，当前不处理 HLS 或需要媒体 Cookie 的回放。获取接口所需的登录态由 Playwright 管理；ffmpeg 读取平台返回的可直接访问的媒体 URL。

   ffmpeg 使用 `-map 0:a:0 -vn -c:a copy -movflags +faststart` 提取首个音轨，不转码；网络读取超时为 30 秒。异步子进程避免阻塞浏览器事件循环，取消任务时会终止子进程并清理临时文件。输出按 `course_id` 分目录，日期和 ID 组成文件名；路径解析后检查目录包含关系。下载到同目录的唯一临时文件，ffprobe 校验音轨与时长后原子替换。已有有效文件默认跳过，失败重下不会损坏原文件。

   每门课的 `媒体地址.json` 原子更新，保留其他课时以及跳过文件原有的下载时间。损坏清单报错，不静默覆盖。`--dry-run` 仅请求并显示课时信息，不依赖 ffmpeg 或写本地文件。批量任务默认继续处理后续课时，任何失败使命令退出码为 1。

### Chrome 插件（Manifest V3）

**原理**：用户已在课程页面登录 → 插件从当前标签页提取 origin → 构造同站点 API → 直接 `fetch`；遇到网络、认证或跨域限制时在课程标签页内回退请求一次 → 解析 JSON → 生成文件下载。

**技术栈**：
- Chrome Extension Manifest V3
- 原生 JavaScript，无构建工具
- JSZip — 批量导出时打包为 ZIP

**文件结构**：
| 文件 | 职责 |
|------|------|
| `extension/manifest.json` | 声明 `activeTab`、`downloads`、`scripting` 和两个站点权限 |
| `extension/popup.html` | 弹窗 UI（当前课时 / 全部课时双标签页） |
| `extension/core.js` | URL 解析、动态 API、字幕解析和格式转换纯函数 |
| `extension/popup.js` | 字幕界面、字幕文件下载和音频任务入口 |
| `extension/requests.js` | 字幕与音频共享的 JSON 请求、超时重试和课程页面内回退 |
| `extension/audio-core.js` | 音频任务参数验证、媒体选择、文件名和 WebVPN 媒体地址处理 |
| `extension/audio.html` / `audio-page.mjs` | 独立音频任务页面、逐节下载、取消、重试与结果报告 |
| `extension/mp4-audio.mjs` | 流式读取 MP4、选择 AAC 音轨并封装为 M4A |
| `extension/audio-download.mjs` | 跟踪 Chrome 下载完成/中断，管理 Blob 生命周期 |
| `extension/vendor/` | MP4Box.js 2.5.0 的固定 ESM 发布包、来源校验信息与 BSD 许可证 |
| `extension/jszip.min.js` | JSZip 库，用于 ZIP 打包 |

**关键实现细节**：

1. **无需保存登录凭证**：插件使用当前浏览器会话，`fetch` 设置 `credentials: "include"`。若弹窗上下文受到 Cookie/CORS 限制，则通过 `chrome.scripting.executeScript` 在已验证的课程标签页中重试。

2. **URL 解析**：通过 `chrome.tabs.query` 获取当前标签页 URL，验证 HTTPS、允许域名和 `/livingroom` 路径，再用 `URLSearchParams` 提取参数。API 根地址取 `URL.origin`，不写死某个站点。

3. **批量导出**：调用 `/courseapi/v2/course/catalogue` 获取课程目录，使用 `start_at` 和 `status` 筛选截至当前可回放（`status=6`）的课时，并将 `sub_id` 统一为字符串、去重。未开课、直播中和暂无回放的记录不参与全选或字幕请求。循环获取所选回放字幕，JSZip 打包后通过 `chrome.downloads` 下载。

   字幕接口的 `code=10002`（“未查询到语音数据”）单独记为“暂无字幕”；登录等其他业务错误和 HTTP 错误记为“请求失败”。每次回放的结果显示在界面，ZIP 内附 `字幕导出结果.json`，无可导出字幕时也可单独下载报告。请求有 20 秒超时，对网络错误、408、429 和 5xx 自动重试一次。元信息请求失败不会阻止字幕导出，打包或浏览器下载失败后会恢复操作按钮。

4. **与 CLI 共享逻辑**：JSON 递归解析（`extractSubtitleItems`）和格式转换（`toSrt`、`toTxt`）逻辑一致。两端读取 `tests/fixtures/` 中同一组 URL 与字幕转换向量，防止实现再次分叉。SRT 要求有效开始时间，缺失结束时间使用 `+5s`；TXT 只依赖非空文本。

5. **浏览器内音频下载**：弹窗提供当前课时、已选回放和本次报告中的 `no_subtitle` 课时三个入口，将 origin、课程名称、课程/课时 ID 和来源标签页 ID 传给独立的 `audio.html` 标签页。批量下载统一按复选框中的已选课时执行，按钮显示已选数量；列表顶部复选框控制全选/全不选，没有单独绕过勾选状态的全部下载入口。字幕导出期间所有音频入口禁用。参数在音频页再次验证，API 使用与字幕完全相同的 `requests.js`。媒体优先级与 CLI 一致；仅允许已有两个受信站点的 HTTPS 媒体，校外任务会把元信息中残留的校内绝对媒体地址映射到 WebVPN origin，不新增站点权限。

   当前使用的是同时包含画面与声音的 MP4 地址。插件边读取网络数据边解析，但会读取完整 MP4；仅将音轨保存为 M4A，不将视频另存到磁盘。音轨直接复制并重新封装，不解码或重新编码，因此不会引入额外音质损失。插件内置 MP4Box.js，不依赖本机 ffmpeg、ffprobe 或其他转换软件；Python CLI 使用本机 ffmpeg/ffprobe 实现提取与校验。当前没有接入独立音频源或 HTTP Range 音轨选择下载，最终文件较小不代表网络传输量也较小。

   音频页串行处理课时，使用内置 MP4Box.js 保留并解析 MP4 数据（兼容 moov 位于文件末尾），只选择 AAC 音轨并生成音频初始化段与媒体片段。每 1000 个样本输出一段并释放样本副本，结束时检查已输出样本数与轨道总样本数一致，再生成带 M4A 容器标识、MIME 为 `audio/x-m4a` 的 Blob，避免 Chrome 自动改用 `.mp4` 扩展名。此过程只封装，不转码。读取媒体时有 30 秒空闲超时和 1 GB 上限；取消操作中止 fetch 并清理解析状态。MP4 数据和生成的音频会占用浏览器内存，任务页关闭会中止尚未完成的提取。

   每次音频任务固定使用一个 `audio_<课程名称>_<下载时间>/` 目录，时间取任务开始时的本地时间，格式为 `YYYY-MM-DD_HH-mm-ss`。课程名称优先取弹窗参数，其次取课时元信息，缺失时使用课程 ID；名称先清理非法路径字符。同批次的音频、重试输出和 JSON 报告共用目录。

   `audio-download.mjs` 通过 `chrome.downloads.onChanged` 和 `search` 确认文件保存完成或中断，完成后才释放 Blob URL。失败课时保留原因并可重试，已成功保存的课时不重复执行；用户可保存包含成功媒体清单和逐课结果的 JSON。字幕下载过程中的音频入口会禁用，防止新标签页关闭弹窗；字幕格式转换、ZIP 和字幕报告逻辑不变。插件无须运行本地 CLI，也不会加载远程脚本。

6. **格式选项与课程名称**：当前课时和全部课时均使用 `.srt`、`.txt`、`.json` 标签，默认仅勾选 `.txt`；选项通过 `flex-wrap` 横向排列，空间不足时换行。课程名优先读取当前课时的 `data.course_title`，其次兼容备用课程接口的 `data` / `result.data` 对象或数组，忽略空白与非字符串值。获取到的名称用于弹窗、完整名称提示、字幕 ZIP 名称、导出报告及音频任务参数；元信息失败不会阻止字幕下载。

7. **音频转写边界**：下载流程仅提取平台字幕或回放音轨，不自动执行语音识别。MacParakeet 是可选的外部转写工具，其 macOS 使用方式见 [docs/macparakeet.md](docs/macparakeet.md)；其他系统可自行选择 ASR 工具。

### 音频选择下载实验

`scripts/audio-range.mjs` 是独立的 Node.js 原型，通过 MP4 索引定位 AAC 样本，并使用 HTTP 多范围响应选择读取音轨。当前插件仍使用上述完整 MP4 读取流程。真实课程的响应体读取量从约 366 MB 降到 62 MB，AAC 数据与已归档音频一致；实现、取舍、复现命令和未验证场景见 [音频选择下载探索](docs/audio-download-exploration.md)。原型测试位于 `tests/audio-range.test.mjs`，由统一检查执行。

## 输入与文件路径安全

课程 URL 仅接受受信任 HTTPS 域名、标准端口和 `/livingroom` 路径，拒绝用户名、密码、重复关键参数及非数字 ID。CLI 写文件前会再次验证 `sub_id`、清理课程名，并对三个最终路径调用 `resolve()` 后执行目录包含关系检查。扩展对 API 参数和 ZIP entry 中的 ID 执行同样的二次验证。

## 两种方式的对比

| | CLI | Chrome 插件 |
|---|---|---|
| 登录 | Playwright 登录，校内外状态分开持久化 | 用户在对应站点自行登录 |
| 字幕获取 | 网络拦截（需加载播放页） | 直接 fetch API |
| 批量导出 | 需逐个打开标签页 | 直接循环 fetch，ZIP 打包 |
| 依赖 | Python + Playwright + Chromium；音频提取需 ffmpeg/ffprobe | Chrome/Edge；内置 JSZip 和 MP4Box.js，无需外部转换软件 |
| 适用场景 | 本地脚本自动化 | 日常使用，一键下载 |

## 涉及的 API 端点

| 端点 | 用途 |
|------|------|
| `/courseapi/v3/web-socket/search-trans-result?sub_id=&format=json` | 获取字幕数据 |
| `/courseapi/v2/course/catalogue?course_id=` | 获取课程目录（所有课时 sub_id） |
| `/courseapi/v3/portal-home-setting/get-sub-info?course_id=&sub_id=` | 获取课时信息（标题、教师） |
| `/courseapi/v3/multi-search/get-course-teacher-others?course_id=` | 获取课程名称 |

所有 API 均为 GET 请求，需要登录态（Cookie），返回 JSON。
