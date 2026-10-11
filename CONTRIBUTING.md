# 贡献与验证

保持 Python + FastAPI + SQLite + 单页 HTML 的小型结构。

~~~sh
uv sync --locked
uv run python -m unittest discover -s tests -v
node --test tests/web.test.cjs
git diff --check
~~~

修改机制请添加组合测试，特别是同轮多事件、跨分支读取和记忆与事实不同步。测试用临时目录，不修改真实数据库，不依赖真实模型或本机密钥。

页面回归检查需要 Node.js 22+，不需要安装 npm 依赖。改 UI 还应检查两个页签、运行与取消、错误和历史编辑。模型与用户内容应正确转义。

不提交密钥、运行数据库或 runs/ 原始记录。贡献者须有权提交内容；代码按 MIT，素材按其单独许可。模型实验请注明版本、样本量和验证边界。
