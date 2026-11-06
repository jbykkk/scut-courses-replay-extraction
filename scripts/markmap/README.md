# 离线 Markmap 资源

应用运行时直接使用 `extension/markmap-vendor.min.js` 与 `markmap-vendor.css`，不需要 Node.js、npm 或 CDN。Markmap 的资源自动加载与数学公式等外部资源插件未启用；Markdown 中的公式以文本展示。

修改依赖后，在此目录执行：

```powershell
npm ci --ignore-scripts
npm run build
```

提交 `package-lock.json`、生成的 JS/CSS、`extension/MARKMAP-LICENSES.txt` 和 `extension/markmap-vendor.json`。安装包和扩展包会一并包含这些文件。资源清单记录版本与 SHA-256；许可证文件保留依赖的版权和许可声明。
