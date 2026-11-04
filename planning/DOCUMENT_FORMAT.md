# 学习资料的数学与代码格式

对应 EDU-12、EDU-13。此文件是 `study-markdown-1` 格式约定。公开示例均为自编，不含课程原文。配套实现位于 `assistant_service/study_agent/document_format.py`、`math_renderer.py` 与 `exports.py`；Skill 的 `references/document-format.md` 同步本文件。

## 从文字稿到打印稿

正文先讲清一个因果过程，再保留一份 UTF-8 Markdown 作为可修改源稿。数学和代码在解析时分开成块，不经过普通正文的下划线、HTML或换行替换。MD和PDF使用同一份内容；不要分别生成两版后再猜它们是否一致。

数学排版与知识核验是两回事。`u_{\infty}` 能正确渲染，只说明语法通过；物理含义、符号定义、单位、适用条件、中间计算仍须对照来源与独立计算。语言程序也须分别说明是语法片段、已分析程序、已仿真情景，还是经过综合/硬件验证。

## 数学的约定

行内使用 `$u_{\infty}$` 或 `\(u_{\infty}\)`；主稿优先统一为前者。块公式把 `$$` 各放在独立一行，也可用独立行的 `\[` / `\]`。上下标用花括号分组，例如 `$t_{\mathrm{su}}$`、`$2^{n-1}$`。分式用 `\frac{...}{...}`，函数名用 `\sin`、`\log` 等；普通说明和单位使用 `\text{...}` 或 `\mathrm{...}`。不要用正文中的 `u_inf`、`2^n-1` 冒充已排版数学。

```text
The steady membrane potential is $u_{\infty}=RI$.

$$
\begin{aligned}
\tau &= RC,\\
u(t) &= u_{\infty}+(u_0-u_{\infty})e^{-t/\tau}.
\end{aligned}
$$
```

长推导用 `aligned` 分行，条件用 `cases`，不把宽公式缩到无法阅读。正文金额的美元符号写 `\$`。表格外的数学竖线可写 `$|x|$`；表格单元格内优先 `\lvert x\rvert` 或 `\mid`。块公式不可放在普通段落中间。

JSON中的反斜杠需多转义一层：JSON源码 `"$u_{\\infty}$"` 解码后应成为 Markdown `$u_{\infty}$`。`aligned` 换行在最终 Markdown 里是两个反斜杠，在JSON源码里是四个。不要让 `\frac` 的开头变成换页符、`\bar` 变成退格符或 `\right` 变成回车。检查器拒绝控制字符和常见重复转义，不静默猜测修复。

## 代码的约定

完整代码使用注明语言的围栏，如 `vhdl`、`python`、`c`。程序标识符和运算符用行内代码：`q_next`、`<=`、`:=`、`&`。保留空格缩进、字面下划线、引号与符号；不要把代码里的 `$` 当公式，也不要把 `<=` 转成排版箭头后写回源稿。打印用代码行宜控制在80–90字符，过长时在语言允许处手动断行，不修改表达式含义。

原课件代码如有错，原文与修正版分开。可运行完整代码注明版本、库和检查范围；非法反例要就地标明，不计入“全部编译通过”。

## 实际检查与失败处理

```powershell
python scripts/check_study_document.py source.md --render-check
python scripts/check_study_document.py source.md --preview preview.html --pdf print.pdf
```

解析检查会报告源文件行号，包括未闭合公式/围栏、空公式、控制字符和常见重复转义；未标记的下划线只是警告，不能自动判定它是数学还是程序。实际渲染检查使用本地 KaTeX 0.18.7、内嵌字体和临时无头 Edge/Chrome/Chromium，不打开用户配置，不联网加载公式或执行资料中的HTML。

含数学或代码的PDF走浏览器排版；数学实际解析失败、公式超出版心或缺少排版组件时抛错，不退回显示原始LaTeX。Markdown含数学的应用导出也先实际渲染。一次多格式导出先在临时目录全部生成成功，再发布结果，避免PDF失败却留下可被误认成完成的MD文件。普通无数学/代码PDF保留轻量路径。

本实现是明确的Markdown子集，支持标题、段落、引用、简单列表/表格、加粗、行内代码和围栏；不承诺任意CommonMark插件、远程图片、原始HTML或完整LaTeX宏包。公式渲染禁用信任扩展并限制展开；需要新增语法先加实际兼容检查。MD阅读器也必须启用数学渲染；纯文本预览看见定界符并不代表源稿错误。

自动通过后仍需抽看实际PDF：分式/上下标、希腊字母、负号、长式、跨页、代码缩进与换行、表格边界。确认最新输出，避免检查的是旧文件。报告已检查的范围，不把排版成功等同于知识正确或学生已经理解。

KaTeX来源：[官方版本](https://katex.org/versions)、[API](https://katex.org/docs/api)、[选项与安全](https://katex.org/docs/security)。随代码保存MIT许可证与固定版本资源；取自官方npm包并核验包完整性，不运行包安装脚本。
