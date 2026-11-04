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

## 2026-11-04：sg31 图片发送回执误报修复

实际部署使用 macOS arm64、Python 3.14.7、现有 `colima-memory-rosetta` context；只更新 DSQQ 的 AstrBot 插件和 NapCat 发送等待配置，保留 QQ 登录。

### 证据与根因

NapCat v4.18.41 的发送等待时间为 `min(baseTimeout + bytes / 1024 / uploadSpeedKBps * 1000, maxTimeout)`。原配置的基线为 10 秒，估算速度为 256 KiB/s。两次发送日志与 QQ 服务器消息记录对应如下：

| 图片大小 | 原等待预算 | 服务器记录对应的发送耗时 | 结果 |
| --- | --- | --- | --- |
| 1,686,984 bytes | 16.44 秒 | 约 27 秒 | 用户已收到，调用先超时 |
| 2,806,109 bytes | 20.70 秒 | 约 32 秒 | 用户已收到，调用先超时 |

QQ 历史记录中的图片文件名 MD5 与本地生成文件完全一致，服务器消息序号分别为 14、18。此次故障的直接原因是等待预算小于实际发送耗时；仅改变报错文案不能解决。

上游同类问题及维护者建议见 [NapCatQQ issue #1815](https://github.com/NapNeko/NapCatQQ/issues/1815)。

### 修复与验证

- 插件 v0.1.2：发送基线至少 30 秒；通过 NapCat 配置接口写入并读回确认，其他超时参数保持原值，已有更长基线不降低。
- 超时后按当前会话、发送者、开始时间、图片 MD5/大小、新消息 ID 和服务器序号核验；已发送则静默完成。历史查询失败不自动重复发送。
- 图片及会话收据保存在本地，管理员 `/取图` 复用当前聊天的原图，不调用模型；路径越界和跨会话取图均拒绝。
- 46 项测试在本机 Python 3.9 和 sg31 Python 3.14 通过，覆盖误报回归、延迟消息、历史接口不可用、旧消息/待发消息排除、取消、管理员权限和取图。
- 在 sg31 已安装的 AstrBot 容器内，以真实 `aiocqhttp.ActionFailed` 异常、两张已有图片和只读取得的真实历史记录回放插件流程：两次均确认成功，通知次数为零；发送和模型调用均替换为 mock，真实 QQ 发送次数和生成次数均为零。
- 安装后 AstrBot 管理接口正常、QQ 在线。遵照用户“已收到，修正误报”的要求，没有另发测试图片；实际后续发送耗时尚未重新测量。

## 2026-11-04：Qwen3-TTS 本地按需语音

- sg31：Apple M4、16 GB 内存、macOS 26.5.1；原生 Python 3.12、MLX Audio 0.6.4、MLX 0.32.4。
- 模型：`mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit`，当前音色 Vivian，中文；这是模型内置音色，没有使用豆包权重或参考音频克隆。
- 首轮短句生成耗时 5.19 秒，音频 4.72 秒、24 kHz，MLX 峰值分配约 4.39 GB（不是进程常驻内存指标）。
- 独立虚拟环境和当前安装专用 LaunchAgent；登录自启，模型本地加载且设置 `HF_HUB_OFFLINE=1`。通过私有共享文件队列连接 AstrBot，不新增网络监听端口。
- 69 项测试在本机和 sg31 通过，覆盖共享队列请求/结果、工作进程不可用、超时和取消不重试、请求过期、非法音色与文本边界。
- 从已安装的 AstrBot 插件发起真实本地合成，只有 QQ `event.send` 替换为 mock：生成 5.68 秒 WAV，权限 600，NapCat 容器可以读取；真实 QQ 发送次数为零。试听文件另保存到本次工作区 outputs。
- 验证 QQ 在线、原生语音 ready、生图/语音工具均启用且成员可用；全局自动 TTS 关闭，原有两个群白名单保留。
- 运维：`voice local-install` 安装/恢复；`voice install --backend local` 切换；`voice local-stop` 停止并取消登录自启；`uninstall` 同时移除本安装的 LaunchAgent。`down` 仅停止容器。
