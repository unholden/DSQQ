# macOS CLI 验证记录

日期：2026-11-03。

## 环境

- macOS Apple Silicon，Python 3.9.6，Colima 的本机 Linux arm64 Docker 引擎。
- AstrBot `soulter/astrbot:v4.29.3`。
- NapCat `mlikiowa/napcat-docker:v4.18.41`。
- 独立临时数据目录与 Compose 项目；没有使用或修改已有 QQ 实例。

## 已验证

- 23 项单元测试在 macOS Python 3.9 和 Linux 容器 Python 3.12 中通过。
- Compose 配置校验通过，两个真实容器启动，管理接口认证成功。
- NapCat 生成登录二维码，CLI 在终端渲染；未扫描或登录真实账号。
- 使用临时 OpenAI 兼容服务，通过 AstrBot 的模型测试接口完成请求并返回 `available`。没有调用付费模型。
- 容器内 stdio MCP 的添加、连接测试、列表查询成功；实际发现 `echo` 工具。
- 修改工具管理员权限、人格工具白名单成功。
- 原配置包完整导入成功：大肥鱼人格、8 个插件、两套表情资源和全局设置全部报告成功。导入后仍使用原有 `dsqq/chat` 模型；默认记忆和语音关闭。
- 执行 `down` / `up` 重建容器后，MCP 连接、人格工具白名单、大肥鱼人格和模型配置保留；记忆插件仍关闭，模型请求再次成功。
- 通过 CLI 更新人格提示词成功，并核对了主人 QQ 占位符替换。
- AstrBot 配置的 UTF-8 BOM 兼容、管理会话复用、凭证脱敏、导入失败清理、中文和空格路径、项目隔离及删除保护由测试覆盖。

## 尚需使用者凭证的验证

- 手机 QQ 扫码后的真实收发消息与 OneBot 业务链路。
- 真实 New API / Claude 服务及 tool calls 完整往返。
- 可选硅基流动记忆、豆包语音和外部 MCP 服务。

Intel Mac 未做硬件实测；默认两种镜像的 manifest 均提供 amd64 与 arm64。CI 配置覆盖 macOS / Linux、Python 3.9 / 3.12，远端执行结果以 GitHub Actions 为准。
