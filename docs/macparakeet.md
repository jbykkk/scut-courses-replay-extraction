# 使用 MacParakeet 转写课程音频

本项目负责下载平台已有的字幕和回放音频。下载完成后，可以自行选择语音识别工具生成文字或字幕；这里记录 macOS 上使用 MacParakeet 的一种已验证流程。

本文整理自 2026-10-30 的课程音频转写记录。实测环境为 Apple M5、16 GB 内存、MacParakeet 0.8.7、`macparakeet-cli 4.4.0`。MacParakeet 面向 Apple Silicon Mac，安装入口与系统要求见[官方项目](https://github.com/moona3k/macparakeet)。下面的参数已与本机 CLI 帮助核对，升级后以 `--help` 和 `models list` 为准。

## 1. 准备音频与 CLI

先使用本项目的浏览器插件或 `audio` 命令下载音频。默认位置：

- 浏览器插件：浏览器下载目录下的 `audio_<课程名称>_<下载时间>/`，时间格式为 `YYYY-MM-DD_HH-mm-ss`。
- 命令行：项目下的 `output/audio/<course_id>/`。

下文命令从本项目根目录执行，使用示例课程 ID `12345`；请替换为自己的课程和实际文件名。

安装 MacParakeet App 后，可直接使用 App 附带的 CLI，无需在 GUI 中逐个导入文件：

```bash
CLI="/Applications/MacParakeet.app/Contents/MacOS/macparakeet-cli"

"$CLI" --version
"$CLI" transcribe --help
"$CLI" models list
```

如果 App 安装在其他位置，请调整 `CLI`。

## 2. 选择记录中使用的模型

MacParakeet 是应用名称；本次转写使用它的 **Whisper 引擎**，模型为 **Whisper Large v3 Turbo**，由 WhisperKit Core ML 运行。后面的命令均显式设置 `--engine whisper`。

| 项目 | 实测配置 |
|---|---|
| 模型 ID | `whisper-large-v3-v20240930-turbo-632MB` |
| 显示名称 | Whisper Large v3 Turbo |
| 标称大小 | 632 MB |
| 引擎参数 | `--engine whisper` |
| 模型根目录 | `~/Library/Application Support/MacParakeet/models/` |

先用 `models list` 确认当前版本提供的 ID。以下下载和选择命令使用上表中的历史 ID：

```bash
CLI="/Applications/MacParakeet.app/Contents/MacOS/macparakeet-cli"

"$CLI" models list
"$CLI" models download whisper-large-v3-v20240930-turbo-632MB
"$CLI" models select whisper-large-v3-v20240930-turbo-632MB
"$CLI" models status
```

模型首次下载需要联网。`models select` 会更新 App 与 CLI 共用的默认模型；已有该模型时可直接确认选择与安装状态。本次记录的状态包含 `Whisper model downloaded: Yes`。

CLI 4.4.0 的 `transcribe` 默认引擎为 `parakeet`。指定 `--engine whisper` 可以复现本文使用的引擎；`app-default` 则跟随保存的应用设置。

## 3. 转写单个文件

以下为英文课程的纯文本示例；语言提示需根据音频调整，例如中文使用 `zh`，需要自动识别时可尝试 `auto`。

```bash
CLI="/Applications/MacParakeet.app/Contents/MacOS/macparakeet-cli"

"$CLI" transcribe \
  --engine whisper \
  --language en \
  --format transcript \
  --mode raw \
  --speaker-detection off \
  --no-history \
  --output-dir "./output/transcripts/12345" \
  "./output/audio/12345/lesson.m4a"
```

输入 `lesson.m4a` 会在输出目录生成同名的 `lesson.txt`。

## 4. 批量转写

CLI 可以接收文件夹，也可以显式传入多个文件。指定 `--output-dir` 后逐个写入结果，每个输入对应一个文件：

```bash
CLI="/Applications/MacParakeet.app/Contents/MacOS/macparakeet-cli"

# 转写 CLI 下载的一门课程音频
"$CLI" transcribe \
  --engine whisper --language en \
  --format transcript --mode raw \
  --speaker-detection off --no-history \
  --output-dir "./output/transcripts/12345" \
  "./output/audio/12345"

# 转写浏览器插件下载的音频；请按实际下载目录调整路径
"$CLI" transcribe \
  --engine whisper --language en \
  --format transcript --mode raw \
  --speaker-detection off --no-history \
  --output-dir "./output/transcripts/12345" \
  "$HOME/Downloads/audio_数字系统设计_2026-10-31_15-30-00"
```

只处理部分文件时，将最后的目录参数替换为需要的文件路径，例如：

```bash
CLI="/Applications/MacParakeet.app/Contents/MacOS/macparakeet-cli"

"$CLI" transcribe \
  --engine whisper --language en \
  --format transcript --mode raw \
  --speaker-detection off --no-history \
  --output-dir "./output/transcripts/12345" \
  "./output/audio/12345/lesson1.m4a" \
  "./output/audio/12345/lesson2.m4a"
```

## 5. 生成 SRT 和选择参数

需要时间轴时，把 `--format transcript` 改为 `--format srt`，输出文件后缀为 `.srt`。本机 CLI 还列出了 `vtt`、`json`、`dapt` 等格式。

| 参数 | 本次使用方式与影响 |
|---|---|
| `--format transcript` | 干净纯文本，无文件信息表头、说话人标签和逐词时间戳 |
| `--format text` | 带文件信息、说话人标签和逐词时间戳，输出更长 |
| `--format srt` | 带时间轴的字幕文件 |
| `--language en` | 本次英文课程的语言提示，按音频语言调整 |
| `--mode raw` | 保留口语和填充词；`clean` 会清理文本，`app-default` 跟随设置 |
| `--speaker-detection off` | 本次单人授课关闭说话人分离；需要区分多人时可改为 `on` |
| `--no-history` | 不将本次完成的转写写入 App 历史记录 |
| `--output-dir` | 为每个输入保存一个同名结果文件 |

## 6. 实测与结果校对

关闭说话人识别并使用 `transcript` 后，120 秒样本约用 8 秒；四节各约 95 分钟的课程合计约用 13 分钟。完整配置、对照数据和适用范围见[转写方法与实验记录](transcription-notes.md)。

专业术语仍需校对，例如历史结果中出现过 `VHDL` 被识别为 `VHDR`、`concurrent` 被拆成 `co-current`。复习时可对照课件和原音频修正。

这一步是下载完成后的独立操作。插件和本项目 CLI 不会自动安装 MacParakeet、下载模型或执行转写。
