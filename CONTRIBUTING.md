# 贡献指南

感谢你愿意改进 llmchat。这个项目刻意保持**小而可控**：一个前端文件、一个后端文件、零 Python 依赖。

## 开发环境

```bash
git clone <repo> && cd llmchat
python3 server.py            # http://127.0.0.1:17001
LLMCHAT_PORT=17101 LLMCHAT_ADMIN=0 python3 server.py   # 换个端口、关掉管理面板调试前端
```

没有构建步骤：改 `index.html` 或 `server.py` 后**刷新浏览器**即可（静态文件按请求读盘）。

## 代码结构

| 位置 | 说明 |
|---|---|
| `index.html` | 全部前端：UI + 状态管理 + 请求逻辑（单文件，内联 `<script>`） |
| `server.py` | 静态服务 + `/__proxy` 同源反代 + `/__admin/*` 管理 API（仅标准库） |
| `vendor/` | marked / DOMPurify / KaTeX，离线可用 |
| `model-params.json` | 运行时生成，**不要提交**（已在 .gitignore） |

## 提交前自检

```bash
python3 -m py_compile server.py        # Python 语法
node --check <(提取的 inline JS)        # 前端语法，CI 会自动跑
LLMCHAT_PORT=17101 python3 server.py    # 起一下看看页面
```

CI（`.github/workflows/ci.yml`）会跑语法检查与冒烟测试，请保证本地同样通过。

## 约定

- 提交信息用 `feat:` / `fix:` / `docs:` / `refactor:` / `chore:` 前缀
- 不提交任何**密钥、token、内网地址、真实模型名路径**
- 管理接口相关的改动必须保持三条安全约束：**同源校验 + 令牌校验 + 反代主机白名单**
- 新增配置项一律用环境变量，并在 README 的配置表里补一行
- 前端不引入构建工具与 npm 依赖（保持离线可用）；确需新增 vendor 库请同时更新 README 的许可列表

## 安全相关

如果发现安全问题（例如绕过令牌、SSRF、命令注入），请**不要开公开 issue**，直接通过仓库主页的邮箱联系维护者。
