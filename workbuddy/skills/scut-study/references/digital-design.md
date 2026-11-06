# 数字系统设计：解题与验证

适用：数字电路、布尔式、时序约束、VHDL及位宽运算。先读取本课证据，不由此文件推定教师范围或任何实际试题。

## 选择最小检查

- 布尔化简：给变量顺序与原式，逐步写定律。调用 `verify_boolean_design(variables, original, candidate)`，1–6变量、最多64种二值输入；乘积显式写 `*` 或 `and`。结果不等价时先看返回反例；一致不等于最简、无毛刺或时序合格。工具采用布尔代数优先级，不是VHDL语法解释器。
- SOP是sum-of-products/积之和/与或式，POS是product-of-sums/和之积/或与式。一般形式与canonical minterm/maxterm形式分别解释；prime implicant和essential prime implicant也分别讲。
- 数值：先位宽、signed/unsigned、取值范围，再算术。类型转换、数值转换、resize分别检查；扩展规则由目标类型决定，截断后重新解释。不要把carry等同于signed overflow。
- 时序：先画launch/capture及skew正方向；setup用最慢路径，hold用最快路径，说明是否包含clock-to-Q。逻辑真值表不会证明无hazard，最短延迟也不能套到setup式子。
- HDL：先声明与完整process，再逐事件看旧值/新值、delta、敏感表与if范围。组合process遗漏赋值可能推断latch；时钟进程遗漏else可能是enable，不能只看关键字判断。并发零延迟反馈的第一轮delta不等于最终稳态。受限integer赋值即使后面覆盖，也可能先越界。
- VHDL版本：明确93/2008；`process(all)`等扩展不自动让std_logic_vector成为数值类型。不同逻辑运算符混用须显式括号，不把布尔代数优先级照搬到VHDL。

## 教学顺序

同一功能串起需求→真值表→布尔式→化简→电路→延迟，再连接entity/architecture→类型/对象→并发/process→实际存储与组合硬件。让学生先预测一个反例或下一拍，再解释；能背术语、认出别名、推导与手写模块分开检查。

PPT语法骨架可能省略context clause，截图还可能有笔误。先看原图，再分清原文与可运行修正版。没有编译/仿真工具权限时给手工追踪表与明确边界，不能宣称已运行；本MCP不提供任意代码执行。穷举工具不能验证模拟X/Z、物理延迟、HDL语法或综合资源。
