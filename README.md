# DSQQ — macOS CLI

从 [Jack-Cherish/DSQQ](https://github.com/Jack-Cherish/DSQQ) fork 的终端版本。用 CLI 安装、配置和管理 AstrBot + NapCat，把 Claude / DeepSeek 等模型接到 QQ。

**全程不需要安装器 App、Docker Desktop 或浏览器。** macOS 通过 Colima 运行 Linux 容器；NapCat 使用容器里的 QQ，不修改本机 QQ.app。支持 Apple Silicon / Intel。手机 QQ 扫码仍是登录所必需的。

这是 macOS 上的无界面容器方案，并非 macOS 原生 QQ 协议端。上游 Windows 源码保留在 `app/`、`build/`，用法见 [Windows 文档](README.windows.md)。

## 准备环境

需要 macOS、Python 3.9+、Docker CLI + Compose，以及本机 Linux Docker 引擎。推荐 Colima（4 CPU、6 GB 内存，预留至少 15 GB 空间）：

```bash
brew install python colima docker docker-compose qrencode
mkdir -p "$HOME/.docker/cli-plugins"
test -e "$HOME/.docker/cli-plugins/docker-compose" || \
  ln -s "$(brew --prefix docker-compose)/bin/docker-compose" \
    "$HOME/.docker/cli-plugins/docker-compose"
colima start --profile dsqq --cpu 4 --memory 6 --disk 30
docker --context colima-dsqq info
```

已有本机 Docker 引擎可直接使用。初始化记录 `--context`，以后切换全局 context 不影响本安装。仅支持本机 Unix socket context。

## 快速开始

```bash
git clone --branch macos-cli https://github.com/unholden/DSQQ.git
cd DSQQ
./dsqq doctor --context colima-dsqq
./dsqq init --context colima-dsqq
```

按提示填写主人 QQ、API Base URL、模型名称和 API Key。Key 输入不回显。New API 用网关的 `https://域名/v1`、实际 Claude 模型 ID、网关签发的 Key。

```bash
./dsqq install        # 拉取固定版本镜像、启动、导入人格和插件
./dsqq login          # 在终端显示二维码，用手机 QQ 小号扫码
./dsqq status
./dsqq test-model     # 可选：真实模型请求，可能产生 API 费用
```

终端关闭不停止容器。默认导入人格、表情包及基础插件，**记忆和语音默认关闭**，不强制要求豆包或硅基流动 Key。只启动基础机器人可用 `install --skip-pack`，随后再 `import-pack`。

数据默认保存在 `~/Library/Application Support/DSQQ`。自定义目录用 `DSQQ_HOME`，或把 `--root /path/to/data` 放在子命令之前。支持中文及空格路径。

无人值守初始化（先由秘密管理工具设置 `NEWAPI_KEY`）：

```bash
./dsqq init --context colima-dsqq --non-interactive \
  --owner-qq 你的QQ号 --base-url https://你的网关域名/v1 \
  --model 你的Claude模型ID --api-key-env NEWAPI_KEY
# 也可用 --api-key-file /path/to/key.txt；不提供明文 Key 命令参数。
```

本机 New API 请用 `http://host.docker.internal:端口/v1`；`localhost` 指向 AstrBot 容器自身，CLI 会拒绝这种配置。网关必须监听容器可访问的地址。模型接入使用 OpenAI Chat Completions 兼容接口；订阅 OAuth 的转换、刷新由上游负责。

## 日常命令

```bash
./dsqq up                    # 启动/恢复，不重置设置
./dsqq down                  # 停止并移除本项目容器，保留数据
./dsqq restart
./dsqq status --json          # 未就绪/未登录退出码 1；命令错误 2
./dsqq logs astrbot --tail 100
./dsqq logs napcat --follow   # Ctrl-C 只结束查看日志
./dsqq login --refresh
./dsqq models
./dsqq configure --base-url https://你的网关域名/v1 \
  --model 另一模型ID --api-key-env NEWAPI_KEY --non-interactive

# 可选功能从 SILICONFLOW_API_KEY、DOUBAO_APP_ID、DOUBAO_ACCESS_TOKEN 环境变量读凭证
./dsqq import-pack --memory siliconflow --voice doubao
```

`configure` 先停止 AstrBot、写配置，再恢复原运行状态；保留人格、插件和 MCP，不切回 DeepSeek。`import-pack` 重新应用配置包的人格、插件参数及能力默认值；已有自定义设置时请注意这点。`none` 关闭对应记忆/语音。

下载失败重跑 `install`；启动超时重跑 `up`；导入失败查看数据目录中的 `import.log`，再重跑 `import-pack`。状态检查使用管理接口实际认证结果；QQ 在线不代表模型调用成功。

`down` 后重建容器时，AstrBot 可能重新安装插件的 Python 依赖，需要联网；慢速网络可使用 `up --timeout 600` 延长等待。

## MCP、工具和人格

准备 `search-mcp.json`（替换示例地址、认证信息）：

```json
{
  "transport": "streamable_http",
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"},
  "timeout": 30,
  "sse_read_timeout": 300
}
```

```bash
./dsqq mcp add search --file search-mcp.json
./dsqq mcp list               # 隐藏服务器认证信息
./dsqq mcp test search
./dsqq tools list
./dsqq tools permission 实际工具名 admin
./dsqq persona tools 大肥鱼 --tool 实际工具名 另一个工具名
./dsqq persona tools 大肥鱼 --all
./dsqq persona prompt 大肥鱼 --file persona.md
./dsqq persona list
./dsqq mcp remove search
```

`--tool` 不带名称表示禁用全部工具。人格文件中的 `{OWNER_QQ}` 会替换为主人 QQ；已有聊天可发 `/reset` 重新开始。支持远程 HTTP/SSE 和容器内 stdio MCP；stdio 的程序及路径必须存在于 **AstrBot 容器**。电脑能力默认 `none`。模型经 New API 使用工具时，上游必须完整支持 tool calls 及结果回传。

## 可选：GPT Image 生图工具

```bash
./dsqq image install --model gpt-image-2
./dsqq image test --prompt '一只戴宇航员头盔的蓝色小鱼，白色背景'
```

`image install` 安装随仓库提供的 `astrbot_plugin_dsqq_image`，只重启 AstrBot，保留 QQ 登录和聊天模型；复用 `dsqq_source` 的网关与 Key。更新插件代码后再次执行该命令即可。`image test` 会调用真实生图模型并打印容器内图片路径，仅保存文件，不发送 QQ 消息。

QQ 管理员可发 `/生图 一只戴宇航员头盔的蓝色小鱼`，也可以直接说“帮我画一张……”，让聊天模型调用 `generate_image`。人格若设置了工具白名单，需要将 `generate_image` 加入白名单；默认大肥鱼人格允许全部可用工具。插件会在后台生成并把图片发到当前聊天，一次处理一张；初始仅管理员可用。执行 `dsqq image install --group-members on` 可开放群成员 @机器人生图；`--group-members off` 恢复仅管理员。不传此参数会保留已有设置。开放后，群成员可发“@机器人 帮我画一张……”或“@机器人 /生图 图片描述”，并用“@机器人 /取图”取回自己在当前群生成的图片；群聊仍需通过 AstrBot 会话白名单。

`image install` 将 NapCat 的发送等待基线提高到至少 30 秒，再按文件大小追加上传时间；已有更长配置会保留，其余网络和超时设置不变。若仍未收到发送回执，插件会在后台核对当前会话的消息记录，只接受机器人发送、时间匹配、图片 MD5 和大小匹配、具有服务器序号的新消息；确认成功后静默结束，不自动重发或重新生图。

核验仍未确认时，图片继续保存在本地，仅提示“图片已做好并保存。发送「/取图」可以取回，不会重新生成。”有生图权限的用户可用 `/取图`（群成员需 @机器人）重新发送当前聊天里自己最近生成的图片，不调用生图模型。核验最多查询 7 次（间隔累计 62 秒，每次查询最多 8 秒）；处理中拒绝重复任务。生成失败单独处理。

接口使用 `/v1/images/generations`（支持 URL / base64 返回），单张 1024×1024、最大 20 MB。网关必须支持该接口及模型；模型出现在 `/models` 中不等于生图接口一定可用。默认请求超时 300 秒，不自动重试，避免重复计费。生成图片保存在 `astrbot/data/plugin_data/astrbot_plugin_dsqq_image/`，后续生成时清理超过 7 天的图片。生图开始后重启或重载 AstrBot 会取消本次发送，但上游可能仍完成并计费。

插件配置位于 `astrbot/data/config/astrbot_plugin_dsqq_image_config.json`，不另存 API Key；更换 DSQQ 网关后会跟随使用新配置。图片下载不携带网关凭证，并拒绝内网下载地址。

## 可选：按需语音回复

```bash
./dsqq voice install
# 更换音色，例如中文男声：
./dsqq voice install --voice zh-CN-YunxiNeural
```

使用 Edge TTS，不需要额外 API Key，默认“小晓”中文女声。安装后普通聊天保持文字；只有明确要求语音时才调用 `send_voice`。群成员需要通过已有会话白名单并 @机器人，可说“@机器人 用语音回答：今天心情怎么样”，或使用“@机器人 /语音 大家早上好”。管理员也可在私聊使用。人格若配置了工具白名单，需加入 `send_voice`。

切换到豆包语音合成 2.0：准备一个权限为 `600` 的凭证 JSON 文件，新版填 `{"api_key":"你的豆包语音 API Key"}`；旧版填 `{"app_id":"你的 APP ID","access_token":"你的 Access Token"}`。执行：

```bash
./dsqq voice install --backend doubao --credentials-file /绝对路径/doubao.json
# 指定音色：追加 --voice 音色ID；默认 zh_female_vv_uranus_bigtts
```

豆包使用 [官方 V3 HTTP 接口](https://docs.volcengine.com/docs/doubao-voice/http-chunked-sse-unidirectional-streaming-v3?lang=zh)，资源默认 `seed-tts-2.0`，可用 `--resource-id` 显式指定。凭证复制到安装数据目录，权限 `600`，不写入仓库或输出；没有有效凭证文件时安装命令会在停止服务之前退出。不指定 `--backend` 或 `--voice` 时保留已有设置；切回 Edge 使用 `--backend edge`。真实合成还需要账号已开通对应服务及音色权限。

每次最多 600 字，一次处理一条，语音生成或发送异常时回退到同内容文字，不自动重试。音频在共享数据目录保存 24 小时，后续合成时清理。这里只配置语音输出，不启用语音识别。安装只重启 AstrBot，并关闭全局自动 TTS；保留聊天模型、生图和群聊权限。

## 数据与隔离

```text
DSQQ/
  dsqq.json              CLI 设置、固定镜像版本、随机凭证
  .*session.json         短期管理会话缓存，避免频繁登录限流
  compose.json           Compose 拓扑（不含模型 Key）
  astrbot/data/          配置、数据库、插件、媒体文件
  napcat/config/         NapCat WebUI / OneBot 配置
  napcat/qq/             QQ 登录数据
  napcat/plugins/
  pack/                  配置包副本
  import/                临时导入凭证，用完删除
  import.log             脱敏导入日志
```

- 每个安装有独立 Compose 项目名，不使用固定容器名，不终止其他项目。
- OneBot 只在容器网络互联，不映射宿主机；管理端口仅绑定 `127.0.0.1`。
- 两个容器共享相同 `/AstrBot/data` 路径，供 QQ 发送端读取生成的语音、图片。
- 主机数据根目录 `0700`，CLI 写入的配置/凭证文件 `0600`，原子替换；修改命令使用跨进程文件锁。
- **没有 Keychain/DPAPI 加密**，运行配置含明文 Key，应放在可信本机磁盘。CLI 日志过滤已知安装密钥；第三方插件日志可能含其自身信息。
- 不挂载 Docker socket，不使用 privileged；日志轮转每个服务最多 3 × 10 MB。
- 默认镜像 `soulter/astrbot:v4.29.3`、`mlikiowa/napcat-docker:v4.18.41`。初始化可用 `--astrbot-image` / `--napcat-image` 指定镜像或 digest。插件安装仍依赖上游市场。

```bash
./dsqq uninstall                   # 删除容器，保留数据
./dsqq uninstall --purge --yes      # 永久删除本安装的所有数据
```

不删除镜像、Colima、本机 QQ 或其他项目。共享引擎时用 `dsqq down`，停止 Colima 会影响该虚拟机内所有项目。

## 开发与验证

CLI 只使用 Python 标准库，无需 pip 安装依赖：

```bash
python3 -m unittest discover -s tests_cli -v
python3 dsqq --help
```

CI 在 macOS / Linux、Python 3.9 / 3.12 上跑 CLI 测试。Windows 测试保留在 `tests/`；其中 DPAPI、进程控制等测试需要 Windows。

实际联调范围与限制见 [验证记录](docs/macos-cli-validation.md)。

实现：`dsqq_cli/main.py`（工作流）、`config.py`（配置/容器拓扑）、`runtime.py`（Compose/管理 API）、`state.py`（状态/锁）。复用 `vendor/dafeiyu-config-pack` 导入工具，不依赖 Windows GUI 模块。

QQ 扫码、真实模型回复、语音和外部 MCP 端到端测试需要使用者自己的账号/凭证。不要提交 API Key；请使用 QQ 小号。

参考：[AstrBot Docker](https://docs.astrbot.app/deploy/astrbot/docker.html)、[NapCat Docker](https://github.com/NapNeko/NapCat-Docker)、[AstrBot MCP](https://docs.astrbot.app/use/mcp.html)。
