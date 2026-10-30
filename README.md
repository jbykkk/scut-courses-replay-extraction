# SCUT Courses Replay Extraction

![](./assets/figures/icon.png)

从华南理工大学“百步梯学堂”中提供的课程回放平台“华园视频”提取**课程回放字幕**，支持 JSON、SRT、纯文本三种格式，可批量导出同一课程字幕并打包为ZIP。浏览器插件和命令行均支持下载回放中的音轨，方便为没有平台字幕的课时另行转写。

本项目会根据课程链接自动选择正确入口：

| 使用环境 | 站点 |
|------|------|
| 校内网络 | `https://video.jw.scut.edu.cn` |
| 校外网络 | `https://video-jw-443.webvpn.scut.edu.cn` |

校外入口由学校 WebVPN 代理校内站点。程序不会绕过 WebVPN 或学校统一认证；使用前仍需由本人正常登录并拥有相应课程的访问权限。

提供两种使用方式：**Chrome/Edge 浏览器插件**和**命令行工具**。

## 免责声明
本项目仅用于个人学习交流与课程复习，使用到的所有内容均来自于课程界面公开信息。
不会绕过学校统一认证，也不会破解视频权限，仅对用户本人已拥有访问权限的课程页面进行字幕提取。

请勿将导出的课程内容用于：
- 公开传播
- 商业用途
- 未经授课教师许可的二次分发

及其他滥用、涉嫌侵害他人隐私或权利的行为。

# 如何使用？

## 方式一：Chrome 插件（推荐）

无需安装任何依赖，在课程页面一键下载字幕。适合日常使用。

### 安装

1. Chrome 网址栏输入 `chrome://extensions/` 打开插件面板或直接点击右上角插件模块进入。
2. 右上角开启 **开发者模式**
![](./assets/figures/extension_panel.png)
3. 点击 **“加载已解压的扩展程序”**
4. 选择本项目的 `extension/` 目录并确定。

安装成功后，浏览器右上角会出现插件图标与名称。

Microsoft Edge 插件安装同理。

### 使用

> **前提**：必须先在当前使用的华园视频入口完成登录。校外使用时要先登录 WebVPN，校内和校外登录状态不能互相替代。

#### 单课时下载

1. 打开任意课程播放页面：校内地址为 `video.jw.scut.edu.cn/livingroom?...`，校外地址为 `video-jw-443.webvpn.scut.edu.cn/livingroom?...`
2. 点击浏览器右上角的插件图标，弹窗显示当前课时信息（课程名、教师、课时标题）
3. 勾选格式（`SRT / TXT / JSON`），点击 **下载字幕**

#### 批量下载

1. 在弹窗中切换到 **全部课时** 标签页
2. 显示截至当前已生成回放的课时（默认全选），顶部显示识别课时数，并在导出检查后显示异常数
3. 取消勾选不需要的回放，选择格式后点击 **下载所选回放字幕 (ZIP)**，下载期间保持弹窗打开
4. 已获取的字幕打包为 ZIP，并附带 `字幕导出结果.json`，逐次记录导出结果及未导出原因

课表中的一行代表一次回放，例如“第 3–4 节”包含两节课。回放次数不等于教学节数；有回放也不一定有字幕。

#### 下载回放音频

在扩展管理页重新加载本项目的 `extension/` 目录后，可以使用以下音频入口：

- **当前课时**：点击“下载本次回放音频 (M4A)”。
- **下载已选课程音频**：在“全部课时”勾选需要的回放，点击按钮批量下载。列表顶部复选框用于全选或全不选；按钮显示已选课时数，没有勾选时禁用。未来课时、直播中和暂无回放的课时不在列表内。
- 完成一次字幕导出后，如果存在“暂无字幕”的课时，会出现“下载暂无字幕课时的音频”按钮，仅处理这些课时，不包含请求失败的课时。

音频任务会打开独立下载页，并逐节保存到浏览器下载目录中的 `audio_<course_id>/`。可以关闭插件弹窗，请在任务结束前保留下载页；页面显示读取进度和保存结果，支持取消、重试未完成的课时，以及下载包含媒体地址、时长和文件大小的结果报告。已完成的课时不会在同一次任务重试时重复下载；重新创建任务时，浏览器会自动重命名同名文件。

插件内置 MP4Box.js，在浏览器内将回放 AAC 音轨重新封装为真正的 M4A 文件，不重新编码，也不需要安装 ffmpeg。通常仍需读取整段视频数据。当前支持这两个华园视频站点上的普通 MP4/AAC 回放，单个媒体文件上限为 1 GB；HLS、其他音频编码以及更大的文件需另行处理。

字幕导出期间音频入口暂时禁用，避免新标签页关闭弹窗而中断字幕导出。字幕格式选择、ZIP 内容和导出报告继续沿用原有流程。

#### 输出格式

| 格式 | 说明 |
|------|------|
| `.srt` | 标准字幕文件，含时间轴，可直接用于视频播放器 |
| `.txt` | 纯文本，无时间信息，便于阅读和搜索 |
| `.json` | 以 `json` 风格切分的字幕，含 `BeginSec / EndSec / Text` 字段 |

#### 常见问题

**弹窗显示"请先打开课程播放页面"**
当前标签页不是华园视频的课程播放页，请先打开一个课程页面。

**下载失败或显示 HTTP 错误**
登录状态可能已过期，请在当前使用的华园视频入口重新登录后重试。校外入口需要重新登录 WebVPN。

**部分课时没有字幕**
新录制的课程可能尚未生成字幕，或平台没有该课时的语音数据。插件会区分“暂无字幕”和“请求失败”，在列表和结果明细中显示原因；即使全部课时都没有字幕，也可以单独下载结果报告。平台返回“未查询到语音数据”时，可以点击插件的音频按钮，或用命令行的 `audio --from-report` 下载回放音频，再交给本地语音识别工具转写。

**批量列表没有显示未来课时**
这是预期行为：批量下载仅列出已生成回放的课时，避免把未开课、直播中或暂无回放的课时当作字幕导出失败。

**下载被浏览器拦截**
Chrome 可能阻止了自动下载，检查地址栏右侧是否有下载被阻止的提示，点击允许/保留即可。

---

## 方式二：命令行工具

适合接口调试、自动化脚本等场景，基于 Python + Playwright。日常使用推荐方式一的浏览器插件。

### 安装

本项目采用 [uv](https://docs.astral.sh/uv/) 统一管理环境。

```bash
uv sync
uv run playwright install chromium
```

### 使用

```bash
# 1. 登录校内站点（默认）
uv run python main.py login

# 校外使用时登录 WebVPN
uv run python main.py login --site external

# 2. 获取单节课字幕（粘贴课程链接）
uv run python main.py get "https://video.jw.scut.edu.cn/livingroom?course_id=64762&sub_id=554146&tenant_code=21"

# 校外链接会被自动识别
uv run python main.py get "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=64762&sub_id=554146&tenant_code=21"

# 交互式输入（不带参数）
uv run python main.py get
请粘贴课程链接（回车确认）：
> https://video.jw.scut.edu.cn/livingroom?course_id=64762&sub_id=554146&tenant_code=21

# 3. 批量导出整门课程
uv run python main.py get --all "https://video.jw.scut.edu.cn/livingroom?course_id=64762&sub_id=554146&tenant_code=21"

# 校外整门课程同样会自动选择 WebVPN 入口
uv run python main.py get --all "https://video-jw-443.webvpn.scut.edu.cn/livingroom?course_id=64762&sub_id=554146&tenant_code=21"
```

### 下载回放音频

音频下载复用 CLI 保存的登录态。浏览器插件的登录状态不会自动提供给 CLI，请先执行上面的 `login` 命令。安装系统工具 ffmpeg（同时提供 ffprobe）：

```bash
# macOS；其他系统使用对应包管理器安装 ffmpeg
brew install ffmpeg

# 从课程播放链接下载单节课音频，站点由链接自动识别
uv run python main.py audio "https://video.jw.scut.edu.cn/livingroom?course_id=81478&sub_id=686078&tenant_code=21"

# 按课时 ID 批量下载；未传链接时默认使用校内登录态
uv run python main.py audio --course-id 81478 --sub-id 686078 686034 686082 686038

# 从插件的导出报告中自动选择 status=no_subtitle 的课时
uv run python main.py audio --from-report /path/to/字幕导出结果.json

# 校外使用同一份报告时，需要明确选择 WebVPN 登录态
uv run python main.py audio --from-report /path/to/字幕导出结果.json --site external

# 只探测课时状态、媒体地址和输出路径，不下载或写清单，也不要求安装 ffmpeg
uv run python main.py audio --course-id 81478 --sub-id 686078 --dry-run
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

### 字幕输出

每次抓取生成三个文件：

| 格式 | 路径 | 说明 |
|------|------|------|
| `.json` | `output/json/<sub_id>.json` | `json` 风格切分的字幕，含 `BeginSec/EndSec/Text` |
| `.srt` | `output/srt/<sub_id>.srt` | 标准字幕格式，含时间轴 |
| `.txt` | `output/txt/<sub_id>.txt` | 纯文本，无时间信息 |

### 字幕数据示例

**.srt：**
```
7
00:01:33,000 --> 00:01:37,000
刚刚呢我们正好讲完了我们的这个第一节的内容
```

**.txt：**
```
嗯嗯好好好好好
感谢大家冒雨前来上课。
那今天是咱们最后一次课，啊然后因为今天咱们也是两节
```

---

## 音频转写（可选）

下载后的音频可以交给自行选择的语音识别工具处理。macOS 用户可选择 MacParakeet，使用其内置 CLI 批量生成 TXT 或 SRT；操作步骤和模型配置见 [MacParakeet 使用说明](docs/macparakeet.md)，本机实测与其他转写方式的记录见 [转写方法与实验记录](docs/transcription-notes.md)。

## 项目结构

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
│   ├── mp4-audio.mjs     # 浏览器内 AAC 音轨提取
│   ├── vendor/           # 固定版本 MP4Box.js 与许可证
│   └── jszip.min.js
├── output/              # 字幕与音频输出（CLI）
│   ├── audio/           # 按 course_id 归档音频与媒体清单
│   ├── json/
│   ├── srt/
│   └── txt/
└── data/                # 登录状态（CLI）
```

具体实现详见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

贡献流程与检查要求见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

### 插件回归测试

安装上述 Python 依赖和 Playwright Chromium 后运行：

```bash
uv run python -m unittest discover -s tests -v
```

测试在无头浏览器中使用离线模拟接口，验证课时筛选、字幕缺失与接口错误的区分、ZIP 内容及下载失败后的恢复，无需登录或访问真实课程。音频测试还覆盖两种站点的登录 Cookie、报告筛选、清单更新和批量失败状态；安装 ffmpeg/ffprobe 后会额外运行本地 HTTP MP4 的实际音轨提取测试，未安装时仅跳过该项。

插件测试使用生成的 MP4 样本验证 M4A 提取、下载完成/中断、取消和部分失败重试，并回归单课字幕三种格式与批量 ZIP。样本不包含真实课程内容。

## 已知限制

- **字幕 CLI 需要有头浏览器** — headless 模式下播放器不会触发字幕 API 请求；`audio` 直接请求课时信息，默认使用无头浏览器
- **登录状态会过期** — Cookie 失效后需重新扫码登录对应站点

## 权限说明

插件仅在 `video.jw.scut.edu.cn` 和 `video-jw-443.webvpn.scut.edu.cn` 页面运行，用于读取当前课程页面信息与字幕接口响应。

不会上传任何课程数据，也不会收集用户账号信息。
