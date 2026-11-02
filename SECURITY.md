# 安全策略

## 报告漏洞

请**不要**通过公开 issue 报告安全问题。请发送邮件到仓库主页显示的维护者邮箱，或使用 GitHub 的
[Private vulnerability reporting](../../security/advisories/new)。我们会在 7 天内回复。

## 威胁模型与既定约束

llmchat 的设计假设是**单机单用户、仅本机访问**。默认配置下的安全边界：

1. **只监听 `127.0.0.1`**。若改成 `LLMCHAT_HOST=0.0.0.0` 暴露到网络，请自行加反代鉴权（如 Basic Auth + TLS），本项目**没有账号体系**。
2. **管理接口 `/__admin/*`** 需要 `X-Admin-Token`（每次启动随机生成）+ Origin/Referer 同源校验，且**不返回通配 CORS 头**。
3. **反向代理 `/__proxy`** 有目标主机白名单（`LLMCHAT_PROXY_ALLOW`，默认仅本机），用于规避 SSRF。
4. **服务启停**通过一条 sudoers 白名单调用 `systemctl <action> ollama`，参数来自固定枚举（`start|stop|restart`），不接受任意命令；不需要该能力请设 `LLMCHAT_SERVICE_CONTROL=0`。
5. **模型参数**保存进 `model-params.json`，写入前做类型白名单校验。

## 不在范围内

- 已被攻陷的本机账户（能改代码/配置就无所谓应用层校验）
- Ollama 自身的安全问题（请上报给 Ollama 项目）
- 故意把服务暴露到公网且未加鉴权的部署方式
