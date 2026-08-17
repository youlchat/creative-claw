# Contributing to Creative Claw

感谢你帮助改进 Creative Claw。提交代码即表示你同意按本仓库的 MIT License 发布贡献。

## 开发环境

需要 Python 3.11 或更高版本：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

提交前请同时运行：

```bash
python -m compileall -q creative_claw examples tests
node --check creative_claw/web/app.js
```

没有 Node.js 时可以跳过最后一项，并在 PR 中注明。

## Pull Request 原则

- 一个 PR 尽量只解决一个问题，并说明用户可观察到的变化。
- 对存储、账本、OHLC 聚合、审批边界的修改必须带回归测试。
- 不提交数据库、生成的 Office 文件、API Key、私人文稿或本地绝对路径。
- UI 修改请附前后截图；API 修改请给出请求和响应示例。

## Clean-room 与许可证边界

可以研究其他产品的公开行为与交互，但不要复制 GPL 或其他不兼容许可证项目的源码、测试、资源、提示词或独特文本。若新增依赖，请在 PR 中写明其许可证，并同步更新 `LICENSES.md`。
