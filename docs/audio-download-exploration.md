# 音频选择下载探索

日期：2026-10-31。状态：可运行的 Node.js 实验原型；插件当前仍使用既有的完整 MP4 读取流程。

## 结论

真实课程回放支持 HTTP 单范围和多范围响应。对于一段约 95 分钟、366.34 MB 的普通 MP4，原型只读取索引和音轨所在范围，以 61.94 MB 响应体数据完成提取，减少约 83.09%。ffprobe 确认输出仅含 AAC 音轨；与之前归档的音频比较，AAC 数据逐字节一致，时长均为 5690.7285 秒。

在已登录的课程页中检查了课时元信息：`content.save_playback`、`content.playback`、`content.file_list`、`video_list`、`segment_video_list` 和 `playurl`。该课时提供 MP4、JPG、WebP 文件，没有发现独立音频地址；这只说明本次检查的课时，不能据此断言平台所有课程都没有音频源。

## 原理与取舍

MP4 索引记录每个音频样本在原文件中的字节位置和长度。先用少量 Range 请求定位并读取 `moov` 索引，再按音频范围读取数据，即可跳过大部分画面数据。MP4Box.js 的 `appendBuffer` 接收原文件中的 `fileStart`，支持稀疏提供数据；本地测试覆盖 `moov` 位于末尾的文件，真实回放覆盖 `moov` 位于头部的文件。

这段回放有 266,754 个 AAC 样本，合并相邻样本后仍有约 142,415 个不连续片段。逐段发请求开销很大，因此实验在一个请求里指定最多 128 个范围，接收 `multipart/byteranges`，并使用 4 个并发请求。响应体按 Content-Range 的长度解析，不能按二进制数据中偶然出现的文本分隔。

```mermaid
flowchart LR
    A[读取 MP4 索引] --> B[取得 AAC 样本的偏移和长度]
    B --> C[每批最多 128 个字节范围]
    C --> D[HTTP multipart/byteranges]
    D --> E[将各片段放回原文件偏移]
    E --> F[MP4Box.js 封装 M4A]
```

同一回放的两次测量如下。MB 使用十进制单位；响应体数据包括 MP4 索引、额外合并的数据和 multipart 封装，不包括 HTTP 头与 TLS 开销。

| 策略 | 响应体数据 | 相对完整 MP4 节省 | 请求数（含索引） | 本次耗时 |
|---|---:|---:|---:|---:|
| 完整读取 MP4（当前插件） | 366.34 MB | — | 1 | 未重新计时 |
| 精确音频范围，多范围批量请求 | 61.94 MB | 83.09% | 1165 | 50.10 s |
| 合并相距不超过 4 KiB 的范围 | 153.66 MB | 58.05% | 86 | 75.98 s |

两种实验输出均为 47.94 MB 的 M4A，AAC 有效数据为 43,648,017 字节；容器大小不同于纯音轨数据大小。索引读取为 3,407,872 字节、52 次请求，已计入表中。

本次合并范围减少了请求数，却读取了更多画面数据，实测也更慢，因此原型默认使用精确范围。每种策略只测一次，网络、缓存和服务器负载会影响耗时；表中完整读取的流量按文件大小计算，未重新下载整段 MP4 做速度对照，不能据此保证实验在所有网络下更快。

## 复现

使用本项目已有的 Node.js 环境，无额外 npm 安装。脚本复用仓库中固定版本的 MP4Box.js，不需要 ffmpeg 完成提取。

```bash
# 仅获取索引，输出可选策略的字节量和请求数
node scripts/audio-range.mjs \
  --manifest /path/to/媒体地址.json --sub-id 12345

# 实际提取，默认每批 128 个范围、4 个并发、精确音频范围
node scripts/audio-range.mjs \
  --manifest /path/to/媒体地址.json --sub-id 12345 \
  --output ./output/experiments/audio-range/sample.m4a

# 对比合并附近范围的策略；请使用新的输出文件名
node scripts/audio-range.mjs \
  --manifest /path/to/媒体地址.json --sub-id 12345 \
  --gap-bytes 4096 --output ./output/experiments/audio-range/coalesced.m4a

node --test tests/audio-range.test.mjs
```

清单需要 `items` 数组，每项包含 `sub_id` 和 `media_url`，兼容本项目的音频结果报告。脚本不打印媒体 URL；输出文件已经存在时直接退出，不覆盖。`--batch-size` 可设为 1–128，`--concurrency` 为 1–8，`--gap-bytes` 为 0–65536。省略 `--output` 时只探测索引。

## 已验证范围与下一步

- 真实校内媒体：单范围、多范围、完整音轨提取、时长、AAC 数据一致性。课程页面同源 `fetch` 也成功收到多范围 HTTP 206 响应。
- 本地自动测试：二进制 multipart、稀疏索引、单范围和多范围提取、范围合并、缺失片段、资源校验标识变化、HTTP 200、超出读取预算及取消。
- 原型只支持索引完整的普通 MP4/AAC。碎片化 MP4、HLS 和其他音频编码明确报错。索引探测有 16 MiB 预算，请求超时为 30 秒；校验 Content-Range 和音频样本完整性，在有强 ETag 或 Last-Modified 时发送 If-Range。服务器返回 HTTP 200 时停止读取，原型不自动回退为全量下载。
- WebVPN 和完整扩展上下文尚未验证。原型使用 Node Buffer 解析 multipart，接入插件时需改成浏览器可用的 Uint8Array 实现，验证权限、代理行为、取消与进度，并设计失败后回退既有下载路径的策略。
- 建议下一步先优化索引读取的请求次数，再覆盖更多真实回放和 WebVPN。确认兼容性后，将精确多范围读取作为插件音频下载的一种策略；字幕下载可继续使用既有模块。

## 参考

- [HTTP Semantics：206 Partial Content 与多范围响应](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.7)
- [HTTP Semantics：multipart/byteranges](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.6)
- [MP4Box.js：appendBuffer 与原文件偏移](https://github.com/gpac/mp4box.js#appendbufferdata)
- 样本偏移和长度的实现依据为仓库固定版本 `extension/vendor/styp-BV5G3FS-.mjs` 中的 `getTrackSamplesInfo` / `buildTrakSampleLists`。
