# 命令行使用与开发

命令行适合接口调试、脚本自动化和开发验证。日常下载也可以使用 [README 中的浏览器插件](../README.md)。以下命令均从项目根目录执行，示例中的课程链接、ID 和路径请替换为自己的内容。

Python 环境使用 uv 管理，要求 Python 3.12 或更新版本。插件开发和统一检查还需要 Node.js；CI 使用 Node.js 24。音频下载的 Python `audio` 命令需要本机 ffmpeg/ffprobe；插件音频下载使用内置 JavaScript 实现。

## 安装

本项目采用 [uv](https://docs.astral.sh/uv/) 统一管理环境。

```bash
uv sync
uv run playwright install chromium
```

## 登录与字幕下载

```bash
# 1. 登录校内站点（默认）
uv run python main.py login

# 校外使用时登录 WebVPN
uv run python main.py login --site external

# 2. 获取单节课字幕（粘贴课程链接）
uv run python main.py get "https://video.jw.scut.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=21"

# 校外链接会被自动识别
uv run python main.py get "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=21"

# 交互式输入（不带参数）
uv run python main.py get

# 3. 批量导出整门课程
uv run python main.py get --all "https://video.jw.scut.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=21"

# 校外整门课程同样会自动选择 WebVPN 入口
uv run python main.py get --all "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=21"
```

## 下载回放音频

音频下载复用 CLI 保存的登录态。浏览器插件的登录状态不会自动提供给 CLI，请先执行上面的 `login` 命令。安装系统工具 ffmpeg（同时提供 ffprobe）：

```bash
# macOS；其他系统使用对应包管理器安装 ffmpeg
brew install ffmpeg

# 从课程播放链接下载单节课音频，站点由链接自动识别
uv run python main.py audio "https://video.jw.scut.edu.cn/livingroom?course_id=12345&sub_id=67890&tenant_code=21"

# 按课时 ID 批量下载；未传链接时默认使用校内登录态
uv run python main.py audio --course-id 12345 --sub-id 67890 67891 67892 67893

# 从插件的导出报告中自动选择 status=no_subtitle 的课时
uv run python main.py audio --from-report /path/to/字幕导出结果.json

# 校外使用同一份报告时，需要明确选择 WebVPN 登录态
uv run python main.py audio --from-report /path/to/字幕导出结果.json --site external

# 只探测课时状态、媒体地址和输出路径，不下载或写清单，也不要求安装 ffmpeg
uv run python main.py audio --course-id 12345 --sub-id 67890 --dry-run
```

默认输出到 `output/audio/<course_id>/<日期>_<sub_id>.m4a`；课时标题没有日期时使用清理后的标题。每门课的 `媒体地址.json` 记录媒体地址、来源字段、站点、本地路径、时长、文件大小与下载时间。平台提供 AAC 音轨时直接复制，不重新编码。

| 参数 | 说明 |
|------|------|
| `--out-dir DIR` | 指定音频根目录，仍按 `course_id` 分目录 |
| `--include-errors` | 处理报告中 `status=error` 的课时，默认仅处理 `no_subtitle` |
| `--force` | 重新下载并替换已有文件；失败时保留原文件 |
| `--stop-on-error` | 首次失败后停止；默认继续处理其他课时 |
| `--headful` | 显示浏览器窗口，便于排查登录状态 |
| `--ffmpeg PATH` | 指定 ffmpeg 可执行文件；ffprobe 需位于 PATH |

已有音频通过 ffprobe 校验后会跳过；损坏文件会报错，可用 `--force` 重下。下载先写入临时文件，成功且校验通过后才替换正式文件。批量任务中任何课时失败，命令都会以非零退出码结束。

当前支持平台的普通 MP4 回放，以及可直接读取的媒体地址；需要媒体 Cookie 的回放和 HLS 暂不支持。ffmpeg 通常仍需读取 MP4 中的视频数据，因此网络下载量会大于最终音频大小。本命令只负责获取音频，语音识别需要另行运行。

## 字幕输出

每次抓取生成三个文件：

| 格式 | 路径 | 说明 |
|------|------|------|
| `.json` | `output/json/<sub_id>.json` | `json` 风格切分的字幕，含 `BeginSec/EndSec/Text` |
| `.srt` | `output/srt/<sub_id>.srt` | 标准字幕格式，含时间轴 |
| `.txt` | `output/txt/<sub_id>.txt` | 纯文本，无时间信息 |

### 字幕数据示例

下面使用示例文字展示格式：

```srt
1
00:00:01,000 --> 00:00:03,000
这是一段示例字幕。
```

对应 TXT 为纯文本，不包含时间信息。

## 开发与验证

### 项目结构

```
├── main.py              # CLI 入口
├── browser.py           # Playwright 浏览器管理与网络拦截
├── subtitle.py          # JSON → SRT/TXT 格式转换
├── audio.py             # 回放媒体选择、音轨提取与媒体清单
├── config.py            # URL 与配置常量
├── auth.py              # 登录状态管理
├── extension/           # Chrome 插件
│   ├── manifest.json
│   ├── popup.html
│   ├── popup.js
│   ├── requests.js       # 字幕和音频共享的登录态请求与页面内回退
│   ├── audio.html        # 独立音频下载页
│   ├── audio-page.mjs
│   ├── mp4-audio.mjs     # 音轨选择读取与兼容回退
│   ├── mp4-audio-range.mjs # 浏览器与 Node 共用的 Range 提取实现
│   ├── vendor/           # 固定版本 MP4Box.js 与许可证
│   └── jszip.min.js
├── output/              # 字幕与音频输出（CLI）
│   ├── audio/           # 按 course_id 归档音频与媒体清单
│   ├── json/
│   ├── srt/
│   └── txt/
└── data/                # 登录状态（CLI）
```

具体实现详见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

贡献流程与检查要求见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

### 本地检查

安装上述 Python 依赖、Playwright Chromium 和 Node.js 后，在项目根目录运行统一检查：

```bash
uv run python scripts/check.py
```

统一检查包含 Python 测试、JavaScript 测试与语法检查、编译检查和 Git 差异格式检查。

测试在无头浏览器中使用离线模拟接口，验证课时筛选、字幕缺失与接口错误的区分、ZIP 内容及下载失败后的恢复，无需登录或访问真实课程。音频测试还覆盖两种站点的登录 Cookie、报告筛选、清单更新和批量失败状态；安装 ffmpeg/ffprobe 后会额外运行本地 HTTP MP4 的实际音轨提取测试，未安装时仅跳过该项。

插件测试使用生成的 MP4 样本验证 M4A 提取、选择读取、兼容回退、下载完成/中断、取消和部分失败重试，并回归单课字幕三种格式与批量 ZIP。样本不包含真实课程内容。

## 已知限制

- **字幕 CLI 需要有头浏览器** — headless 模式下播放器不会触发字幕 API 请求；`audio` 直接请求课时信息，默认使用无头浏览器
- **登录状态会过期** — Cookie 失效后需重新扫码登录对应站点

## 下载后的音频转写

下载与转写分别执行。MacParakeet 的批量 TXT/SRT 操作见 [MacParakeet 使用说明](macparakeet.md)，其他转写方式的本机记录见 [转写方法与实验记录](transcription-notes.md)。

选择下载的 Node.js 探测入口、读取量对比和参数见 [音频选择下载探索](audio-download-exploration.md)；该脚本与插件共享 Range 模块，Python `audio` 命令仍使用 ffmpeg。
