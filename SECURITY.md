# Security Policy

## Supported versions

项目仍处于早期阶段，安全修复只面向 `main` 分支的最新版本。

## Reporting a vulnerability

请不要为未修复漏洞创建公开 Issue。请使用 GitHub 仓库的 **Security → Report a vulnerability** 私下报告，并包含复现步骤、影响范围和建议修复方式。维护者会尽快确认。

## Secrets and local data

- 不要把 API Key 写入 Issue、日志、截图、`.env` 示例或数据库。
- 网页中提交的模型密钥只保存在当前服务进程内存中；进程退出后失效。
- SQLite 数据库可能包含全文、引用、人物关系和修改历史。分享前请按敏感创作资料处理。
- 若密钥曾出现在聊天、提交或截图中，请立即到提供商控制台撤销并轮换。

Creative Claw 默认绑定 `127.0.0.1`。如果改为 `0.0.0.0` 或部署到公网，请在前方配置认证、TLS、请求大小限制与网络访问控制。
