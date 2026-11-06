# 构建和发布

日常开发走分支与 PR；发布使用明确的版本标签。版本号同时维护 `pyproject.toml`、`extension/manifest.json`、服务健康响应、桌面状态接口及 `scripts/Installer.cs`，再运行 `uv lock`。不要把 `.local`、浏览器配置或模型文件放进打包目录。

## 构建

在 Windows x64 / Python 3.12 中执行：

```powershell
uv sync --extra asr --extra study
uv run --extra study python scripts/check.py
uv run python scripts/package_extension.py
uv run --extra study python scripts/build_windows.py
```

构建脚本只收集允许的程序文件，下载 Python 官方嵌入式包并核对发布校验值，按 `requirements-windows.lock` 安装依赖。依赖升级时用 `uv pip compile requirements-windows.in --python-version 3.12 --python-platform windows --generate-hashes -o requirements-windows.lock` 更新清单后重新验证。

运行库的许可文件随包保留，Python 下载来源和包清单写入 `THIRD_PARTY.json`。不复制开发机器的 `.venv`，避免绝对路径和个人配置混入。Windows 包不包含旧命令行工具所需的 Playwright 浏览器；该工具从源码使用。

## 验证与截图

使用空数据目录和包内 `runtime/python.exe` 启动服务，验证健康接口、学习页、配置读写和持久化。检查冷启动、重复点击入口、扩展断开后的状态、任务刷新后仍保留原课程。浏览器登录本身由使用者完成。

公开截图使用隔离数据库和示例安装路径，不放真实课程文字、个人目录、Key 或学校账号。截图使用实际渲染结果，不用设计稿替代。

## 发行资产

- `SCUT-Study-Setup-<version>.exe`：普通用户安装包。
- `SCUT-Study-<version>-windows-x64.zip`：解压版，包含相同运行环境。
- `scut-classroom-assistant-<version>.zip`：单独的浏览器扩展。
- `SHA256SUMS.txt`：上述文件的 SHA-256。

先创建草稿 Release，上传并检查文件名、大小和哈希，再发布。功能仍在迭代时标记为预发布，不把未验证的更新标为稳定版。发布不等同于合并 PR。
