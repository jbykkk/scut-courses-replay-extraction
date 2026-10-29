# 项目实现说明

## 概述

本工具从华南理工大学"华园视频"平台（`video.jw.scut.edu.cn`）提取课程回放字幕。华园视频在播放课程时，前端通过 API 明文拉取字幕 JSON 数据，无需逆向加密或破解流媒体协议。

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
| `config.py` | URL、路径、超时等常量 |
| `auth.py` | 登录状态持久化（Playwright `storage_state`） |

**关键实现细节**：

1. **登录**：访问 `video.jw.scut.edu.cn`，自动跳转到学校统一认证（`sso.scut.edu.cn`）扫码。登录成功后 `storage_state` 保存到 `data/auth_state.json`。

2. **字幕拦截**：必须通过 `livingroom` 页面触发字幕 API 请求。Playwright 用 `page.on("response")` 监听所有响应，匹配 URL 含 `search-trans-result` 的请求。

3. **JSON 解析**：API 返回多层嵌套（`body.list[].all_content[]`），`_extract_list()` 递归展开直到找到包含 `BeginSec` 字段的数组。

4. **课程目录**：通过 `page.evaluate()` 直接调用 `/courseapi/v2/course/catalogue?course_id=xxx`，无需加载播放页。

### Chrome 插件（Manifest V3）

**原理**：用户已在课程页面登录 → 插件直接 `fetch` 字幕 API（浏览器自动带 Cookie）→ 解析 JSON → 生成文件下载。

**技术栈**：
- Chrome Extension Manifest V3
- 原生 JavaScript，无构建工具
- JSZip — 批量导出时打包为 ZIP

**文件结构**：
| 文件 | 职责 |
|------|------|
| `extension/manifest.json` | 插件配置，声明 `activeTab`、`downloads` 权限和 `host_permissions` |
| `extension/popup.html` | 弹窗 UI（当前课时 / 全部课时双标签页） |
| `extension/popup.js` | URL 解析、API 调用、格式转换、文件下载 |
| `extension/jszip.min.js` | JSZip 库，用于 ZIP 打包 |

**关键实现细节**：

1. **无需登录逻辑**：插件运行在用户已登录的浏览器会话中，`fetch` 设置 `credentials: "include"` 自动携带 Cookie。

2. **URL 解析**：通过 `chrome.tabs.query` 获取当前标签页 URL，`URLSearchParams` 提取 `sub_id` 和 `course_id`。

3. **批量导出**：调用 `/courseapi/v2/course/catalogue` 获取课程目录，使用 `start_at` 和 `status` 筛选截至当前可回放（`status=6`）的课时，并将 `sub_id` 统一为字符串、去重。未开课、直播中和暂无回放的记录不参与全选或字幕请求。循环获取所选回放字幕，JSZip 打包后通过 `chrome.downloads` 下载。

   字幕接口的 `code=10002`（“未查询到语音数据”）单独记为“暂无字幕”；登录等其他业务错误和 HTTP 错误记为“请求失败”。每次回放的结果显示在界面，ZIP 内附 `字幕导出结果.json`，无可导出字幕时也可单独下载报告。请求有 20 秒超时，对网络错误、408、429 和 5xx 自动重试一次。元信息请求失败不会阻止字幕导出，打包或浏览器下载失败后会恢复操作按钮。

4. **与 CLI 共享逻辑**：JSON 递归解析（`extractSubtitleItems`）和格式转换（`toSrt`、`toTxt`）逻辑一致，JS 版本从 Python 版本移植。

## 两种方式的对比

| | CLI | Chrome 插件 |
|---|---|---|
| 登录 | Playwright 扫码，状态持久化 | 用户自行登录 |
| 字幕获取 | 网络拦截（需加载播放页） | 直接 fetch API |
| 批量导出 | 需逐个打开标签页 | 直接循环 fetch，ZIP 打包 |
| 依赖 | Python + Playwright + Chromium | 无（纯浏览器插件） |
| 适用场景 | 本地脚本自动化 | 日常使用，一键下载 |

## 涉及的 API 端点

| 端点 | 用途 |
|------|------|
| `/courseapi/v3/web-socket/search-trans-result?sub_id=&format=json` | 获取字幕数据 |
| `/courseapi/v2/course/catalogue?course_id=` | 获取课程目录（所有课时 sub_id） |
| `/courseapi/v3/portal-home-setting/get-sub-info?course_id=&sub_id=` | 获取课时信息（标题、教师） |
| `/courseapi/v3/multi-search/get-course-teacher-others?course_id=` | 获取课程名称 |

所有 API 均为 GET 请求，需要登录态（Cookie），返回 JSON。
