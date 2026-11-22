<p align="center"><a href="https://github.com/jiefeng-ai/phonectl-mcp/blob/main/README.md">English</a> · 中文</p>

<h1 align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/jiefeng-ai/phonectl-mcp/main/docs/logo-dark.svg?v=5"><img src="https://raw.githubusercontent.com/jiefeng-ai/phonectl-mcp/main/docs/logo-light.svg?v=5" height="54" align="middle" alt=""></picture> phonectl-mcp</h1>

<p align="center"><b>让 AI agent 从 Mac 上看见并操作一台真实的 iPhone，插线或走 Wi-Fi 都行。</b></p>

<p align="center">
  <a href="https://pypi.org/project/phonectl-mcp/"><img src="https://img.shields.io/pypi/v/phonectl-mcp?logo=pypi&logoColor=white" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/macOS-only-000?logo=apple&logoColor=white" alt="macOS only">
  <img src="https://img.shields.io/badge/iOS-27%2B-000?logo=ios&logoColor=white" alt="iOS 27+">
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-server-000?logo=modelcontextprotocol&logoColor=white" alt="MCP server"></a>
  <a href="https://github.com/jiefeng-ai/phonectl-mcp/actions/workflows/test.yml"><img src="https://github.com/jiefeng-ai/phonectl-mcp/actions/workflows/test.yml/badge.svg?branch=main" alt="Tests"></a>
  <a href="https://github.com/jiefeng-ai/phonectl-mcp/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
</p>

<p align="center"><a href="#安装">安装</a> · <a href="#工具">工具</a> · <a href="#多个-agent-共用一台手机">共用</a> · <a href="#脚本">脚本</a> · <a href="#排查问题">排查问题</a> · <a href="#安全">安全</a></p>

一个 MCP 服务，对连着 Mac 的 iPhone 截图、读屏幕文字、点按、滑动、打字、按按钮、启动 App、读写剪贴板。手机上不用装东西。

<table align="center">
  <tr>
    <td align="center" width="50%"><img src="https://raw.githubusercontent.com/jiefeng-ai/phonectl-mcp/main/docs/demo-rednote.gif?v=2" width="260" alt="AI agent 通过 phonectl-mcp 在小红书做一篇图文：文字卡片、模板、贴纸、滤镜、标题和正文"></td>
    <td align="center" width="50%"><img src="https://raw.githubusercontent.com/jiefeng-ai/phonectl-mcp/main/docs/demo.gif?v=5" width="260" alt="脚本通过 phonectl-mcp 在 BOSS 直聘里投递一个岗位：用 read_text 读屏、用 tap 点按"></td>
  </tr>
  <tr>
    <td align="center"><sub>AI agent 在小红书做一篇图文：文字卡片、模板、贴纸、滤镜、正文，停在<b>发布</b>前。已加速。</sub></td>
    <td align="center"><sub>脚本在招聘 App 里投递一个岗位：<code>read_text</code> 找按钮，<code>tap</code> 点下去。2 倍速。</sub></td>
  </tr>
</table>

## 需要什么

- 一台装了 [uv](https://docs.astral.sh/uv/) 的 Mac
- 一台 **iOS 27 或以上**的 iPhone，打开「开发者模式」
- 一根 USB 数据线，首次设置时要用；之后走 Wi-Fi 也行

已在 iPhone 17 Pro（iOS 27.2）+ macOS 27 上测试。

## 安装

### 让 agent 帮你装

把下面这段话发给你的编程 agent（Claude Code、Codex、Cursor 等）：

> 按 https://github.com/jiefeng-ai/phonectl-mcp#install-by-hand 安装 phonectl-mcp，并把它加进你的 MCP 配置。运行 `phonectl doctor`，直到全部通过。需要我在手机上操作时，一步步告诉我，等我做完再继续。最后告诉我怎么重启你，让手机工具加载进来。

重启之后，让 agent 截一张手机屏幕试试。

### 手动安装

三条都要执行：

```sh
uv tool install phonectl-mcp
phonectl install-service   # 开机自启服务，地址 http://127.0.0.1:47321/mcp
phonectl doctor            # 检查数据线、信任、开发者模式、磁盘镜像和服务
```

`install-service` 还会在 `~/.config/phonectl/token` 生成访问令牌。然后连接你用的 Agent，每个 Agent 一条命令：

```sh
phonectl setup claude-code        # 或者：codex、gemini（桌面版）、deepseek-harness
```

`setup` 会把服务连同令牌加进这个 Agent 的 MCP 配置，装上 `phonectl-scripts` skill（和别的 Agent 共用手机的脚本要守的规矩：排队、在两个工作单元之间让出手机、被打断后能接着做），并给 Claude Code 和 Gemini 装上一个钩子：Agent 这一轮结束时马上交还手机。第一次改动某个文件之前，它会把原文件保存为 `<文件>.phonectl.bak`，改完列出改了什么。只输入 `phonectl setup` 会列出它认识的 Agent；`phonectl setup --remove <agent>` 把这些都撤掉。升级 phonectl 之后，`phonectl update` 会更新所有这样装过的 Agent 里的 skill（`doctor` 会提示哪个落后了）；你手动改过的 skill 不会被覆盖。

**其他客户端手动配置**（`~/.cursor/mcp.json` 等支持 streamable HTTP 的客户端）。这样配置的 Agent 能操作手机，但没有那个 skill：它写的脚本不知道要把手机让给别的 Agent。

```json
{
  "mcpServers": {
    "phone": {
      "url": "http://127.0.0.1:47321/mcp",
      "headers": { "Authorization": "Bearer <~/.config/phonectl/token 的内容>" }
    }
  }
}
```

重启客户端让它加载服务，再让 agent 截一张手机屏幕试试。

用数据线设置过一次后，手机和 Mac 在同一个 Wi-Fi 下就可以不插线。

## 工具

坐标单位是 iOS 的 point，和返回的截图像素一一对应。

| 工具 | 作用 |
| --- | --- |
| `screenshot` | 截图 |
| `read_text` | 以 JSON 返回屏幕文字和位置，比看图省得多 |
| `tap_text`、`wait_text` | 按屏幕上的文字点按；等文字出现或消失 |
| `tap`、`long_press`、`swipe` | 点按、长按、滑动。往下翻要向上滑 |
| `type_text` | 往当前输入框打字，经剪贴板粘贴，不受手机输入法影响；提交要另按 `press_key('enter')`。`method="keys"` 逐键输入，用于禁止粘贴的输入框 |
| `press_key` | 按键或组合键，如 `enter`、`cmd+a` |
| `press_button` | `home`、`lock`、`volume-up`、`volume-down`、`mute`、`siri` |
| `launch_app`、`list_apps` | 启动 App（带 `url` 时把一个链接交给 App 打开），查 bundle id |
| `get_clipboard`、`set_clipboard` | 读写手机剪贴板 |
| `status` | 服务、手机连接状态，以及谁在用手机 |

操作手机的工具会在操作后返回一张截图（传 `screenshot=False` 则不返回）。手机不确认点按是否生效，请看这张截图判断。

## 多个 Agent 共用一台手机

连到服务的客户端共用这台手机，轮流使用：一个客户端开始用手机后，就一直占着，直到它停顿的时间超过它本轮最长停顿的 3 倍（15 到 90 秒），或者它这一轮结束（见下）。只看过屏幕、没有改变过屏幕的客户端，最后一次调用后只占着手机 15 秒。这期间别的客户端的调用会排队等待，不会落到对方的屏幕上。一次调用最多等 50 秒，比各个 Agent 自己的工具超时都短；等不到就返回 `WAITING`，什么都没做，这个客户端的排队位置保留 2 分钟，再调用一次就从原位接着等。Agent 会把 `WAITING` 当作再调用一次的提示，而不是失败。`status` 会显示谁在用手机、谁在排队。

- 服务按进程区分客户端：一个 Claude Code 会话和它的子 Agent 算一个客户端。Agent 运行的脚本算作这个 Agent 本人，不用等它，也不会把手机从它手里拿走；从终端直接运行的脚本是一个单独的客户端。一个进程里跑几个互不相干的任务时，每个任务发送请求头 `X-Phonectl-Client: <名字>`。客户端的进程一退出，手机马上空出来，起了名字的客户端也一样。
- 只有在别人改变过屏幕之后、发出操作的这个进程又看过屏幕，它的点按、滑动、打字、按键才会发出（这里 Agent 运行的脚本算单独的进程：脚本看了屏幕不等于 Agent 看过，脚本的点按也会让 Agent 需要重新看）。否则这次调用返回错误 `NOT DONE`，什么都不发；先看一眼屏幕，再决定要不要重做。服务启动后每个进程的第一次这类操作也是如此，因为服务不知道之前谁用过手机。`tap_text` 也守这条规矩，因为它找到的文字可能在别人的 App 里；但如果调用方之后自己用 `launch_app` 打开过它的 App，就照常执行。`launch_app`、`press_button` 不依赖旧坐标，总是照常执行。Agent 第一次启动的进程，算作看过这个 Agent 最近看到的画面（每次调用都新开一个进程的工具，会在这一次看屏、下一次点按）；服务没法知道 Agent 是从它的哪个进程读到屏幕内容的。
- 长时间运行的脚本，在两个工作单元之间调用 `POST /phonectl/yield`（同一个令牌）让出手机：只有在有人排队时才交出去。不让出的话，别人要等它停顿，最长等 30 分钟。
- Agent 这一轮结束时，它的钩子可以调用 `POST /phonectl/done` 告诉服务，手机马上空出来，不用等它停顿；`phonectl setup` 会给 Claude Code 和 Gemini 装上这个钩子。
- 服务看不到你本人在用手机：`phonectl hold 30m` 让各个 Agent 让开，`phonectl hold off` 结束。

## 脚本

步骤固定的流程不需要模型在每一步之间思考。写成脚本后，Agent 只需要运行一条命令，不再每一步都和模型往返一次。脚本就是这个服务的又一个 MCP 客户端，用哪个 MCP 库都行。用官方 Python SDK 的写法：

```python
# /// script
# dependencies = ["mcp>=2.3,<3"]
# ///
import asyncio, json
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

async def main():
    token = (Path.home() / ".config/phonectl/token").read_text().strip()
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with streamable_http_client("http://127.0.0.1:47321/mcp", http_client=http) as (read, write, *_):
        async with ClientSession(read, write) as phone:
            await phone.initialize()
            for tool, args in [("launch_app", {"bundle_id": "com.apple.Preferences", "restart": True}),
                               ("tap_text", {"text": "无线局域网", "timeout_s": 5, "settle_ms": 1000}),
                               ("read_text", {})]:
                args = {**args, "screenshot": False} if tool != "read_text" else args
                result = await phone.call_tool(tool, args)
                while result.is_error and result.content[0].text.startswith("WAITING"):
                    result = await phone.call_tool(tool, args)  # another client has the phone: same place in line
                if result.is_error:  # NOT DONE、手机断开：停下，不要往下走
                    return f"{tool}: {result.content[0].text}"
            return [line["t"] for line in json.loads(result.content[0].text)]

print(asyncio.run(main()))
```

用 `uv run script.py` 运行。它的调用和 Agent 一样共用手机：别人在用时排队等待；被拒绝的调用返回错误结果；脚本一退出，手机马上空出来。

## 排查问题

先跑 `phonectl doctor`，它会告诉你断在哪一段。

| 现象 | 处理 |
| --- | --- |
| 找不到 iPhone | 换能传数据的线，解锁手机后重插 |
| 提示 Mac 未被信任 | 解锁手机，重插线，点「信任」 |
| 找不到「开发者模式」 | 运行 `phonectl doctor --fix`，「设置 › 隐私与安全性」里会出现开关 |
| 磁盘镜像安装失败 | 服务第一次连上手机时会从 GitHub 下载并安装开发者磁盘镜像；检查网络或代理，再运行 `phonectl restart` |
| 点了没反应 | 需要 iOS 27 及以上；保持屏幕亮着、不锁屏 |
| Wi-Fi 连接断开 | 手机锁屏后会断断续续离开网络，一次可达几分钟。解锁后等几秒 |
| 客户端连不上 | `phonectl restart`，再看 `~/Library/Logs/phonectl.error.log`。返回 401 说明客户端的令牌和 `~/.config/phonectl/token` 不一致 |

## 安全

- 服务只监听 `127.0.0.1`，不带令牌的请求一律拒绝。
- agent 能做你手指能做的任何事：发消息、付款、删数据。请给它范围小的指令，并看着屏幕。使用风险自负，自动操作第三方 App 可能违反其用户协议。

## 配置

<details>
<summary>环境变量（设好后重新运行 <code>phonectl install-service</code>；<code>restart</code> 沿用旧值）</summary>

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `PHONECTL_UDID` | 第一台连上的 iPhone | 连了多台手机时用哪一台 |
| `PHONECTL_PORT` | `47321` | HTTP 端口（只监听 127.0.0.1） |
| `PHONECTL_TOKEN_FILE` | `~/.config/phonectl/token` | 令牌文件位置 |
| `PHONECTL_SCALE` | `3` | 每 point 几个像素；iPhone SE、XR、11 填 2。只影响截图大小 |
| `PHONECTL_OCR_LANGS` | `zh-Hans,en-US` | `read_text` 识别语言，按优先级 |
| `PHONECTL_SETTLE` | `0.6` | 操作后等几秒再截图；单次调用可用 `settle_ms` 覆盖 |
| `PHONECTL_TOUCH_IDLE` | `30` | 空闲几秒后关闭输入通道（重开约 0.6 秒） |
| `PHONECTL_KEY_IDLE` | `5` | 几秒没按键后断开虚拟键盘，屏幕键盘随之回来 |
| `PHONECTL_CALL_TIMEOUT` | `15` | 等手机响应几秒算失败（不含手势耗时） |
| `PHONECTL_IDLE` | `90` | 客户端停顿多久一定算用完了手机；节奏快的客户端按它自己的节奏判断，最短 15 秒 |
| `PHONECTL_WAIT` | `50` | 调用最多排队等几秒，之后返回 `WAITING`；要短于各客户端自己的工具超时 |
| `PHONECTL_MAX_HOLD` | `1800` | 一个客户端连续占用这么久以后，让排队的客户端先用 |
| `PHONECTL_LOG` | `WARNING` | 日志级别（服务为 `INFO`，会记下每次调用来自哪个客户端），日志在 `~/Library/Logs/phonectl*.log`；一个日志超过 10 MB 时，旧内容移到 `<文件名>.1`，替换更早的那份 |

</details>

其他命令：`phonectl restart`（比如 `uv tool upgrade phonectl-mcp` 升级之后）。卸载：`phonectl uninstall-service`、`uv tool uninstall phonectl-mcp`，再删掉 `~/.config/phonectl`。

## 协议

MIT
