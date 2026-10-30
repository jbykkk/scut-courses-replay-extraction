# 转写方法与实验记录

本文整理已有课程音频的本机实验，供选择下载后的转写流程参考。项目不绑定某个语音识别软件；其他系统或模型可以使用各自的工具处理下载好的音频。

## 实验范围

记录日期为 2026-10-30，机器为 Apple M5、10 核、16 GB 内存、macOS 27.0.1。短样本取自课堂录音的第 600–720 秒，转换为 16 kHz 单声道 WAV，时长 120 秒。

本次记录涉及两种实现：

- MacParakeet 0.8.7 / CLI 4.4.0，WhisperKit Core ML，Whisper Large v3 Turbo（632 MB）。该流程已完成四节各约 95 分钟的长音频转写。
- `openai-whisper 20250625` / `torch 2.12.1`，`small` 模型，比较 CPU 与 MPS；结论来自短样本。

速度与结果随硬件、模型、音频和参数变化。下表保留本次配置下的测量，不作为其他设备的速度承诺。

## MacParakeet 的参数影响

| 场景 | 记录耗时 | 结果形式 |
|---|---|---|
| 120 秒样本，说话人识别开启并输出逐词时间戳 | 约 2 分 05 秒 | 带表头、说话人和时间戳 |
| 120 秒样本，`--speaker-detection off --format transcript` | 约 8 秒 | 干净纯文本 |
| 30 秒与 60 秒两个样本连续转写 | 合计约 5.5 秒 | 干净纯文本 |
| 四节各约 95 分钟的课程 | 合计约 13 分钟，每节约 3–4 分钟 | 每段音频一个 TXT |

最后一项为本次实际批量记录。默认参数与关闭说话人识别的对照同时改变了输出格式，因此不能只把耗时差异归因于某一个参数。操作步骤与模型 ID 见 [MacParakeet 使用说明](macparakeet.md)。

## openai-whisper 的 CPU / MPS 对照

同一 120 秒样本使用 `small` 模型，默认解码参数下的记录如下：

| 配置 | 观察 |
|---|---|
| CPU fp32 | 27 段、1227 字符，重复结果稳定；约 45 秒，包含加载 |
| MPS fp32 | 多次运行得到 9–30 段、455–1099 字符，偶发失败 |
| MPS fp16 | 出现 `-inf` / `NaN` logits 并报错 |

另一次实验固定贪心解码并关闭分段阈值，使用以下参数：

```python
temperature=(0.0,)
compression_ratio_threshold=None
logprob_threshold=None
no_speech_threshold=None
condition_on_previous_text=False
```

该样本中 CPU 与四次 MPS 运行都得到 27 段、1242 字符，文本哈希一致。这些观察提示，解码配置可能影响本次差异；这组样本不足以定位底层原因或代表其他 MPS 版本。

当时的操作选择是：使用 CPU fp32 复现 `openai-whisper small`；完整课程批量转写采用已验证的 MacParakeet / WhisperKit 配置。

### CPU 命令示例

以下命令需要在**已安装 `openai-whisper` 的 Python 环境**中运行；本项目的 Python 依赖只包含下载所需的 Playwright。这里不额外安装或接入转写引擎。

```bash
python -m whisper "./output/audio/12345/lesson.m4a" \
  --model small \
  --device cpu --fp16 False \
  --language en \
  --output_format txt \
  --output_dir "./output/transcripts/12345"
```

需要时间轴时，可把 `--output_format txt` 改为 `srt`。课程术语可以按实际内容设置提示词，例如：

```bash
python -m whisper "./output/audio/12345/lesson.m4a" \
  --model small --device cpu --fp16 False --language en \
  --initial_prompt "VHDL, FPGA, testbench, flip-flop, combinational logic, sequential logic" \
  --output_format srt --output_dir "./output/transcripts/12345"
```

## 其他实现与尚未验证的部分

原记录还将 `mlx-whisper`、`whisper.cpp`、`faster-whisper` 列为候选。这些工具以及其他本地或在线语音识别服务均可按自己的流程接收音频，本项目不要求使用 MacParakeet。

本次没有对这些候选工具做同条件速度或质量对比，也没有完成 `openai-whisper medium` / `large-v3` 的同样实验。需要比较时，可先使用同一个短样本，检查专业术语、漏句、时间轴和耗时，再决定是否处理长音频。
