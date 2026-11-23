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

<p align="center"><a href="#安装">安装</a> · <a href="#工具">工具</a> · <a href="#排查问题">排查问题</a> · <a href="#安全">安全</a></p>

一个 MCP 服务，对连着 Mac 的 iPhone 截图、读屏幕文字、点按、滑动、打字、按按钮、启动 App、读写剪贴板。手机上不用装东西，不需要 Xcode，不用越狱。多个 Agent 和脚本可以同时连着用：它们轮流操作，不会点到彼此的屏幕上。

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

```sh
uv tool install phonectl-mcp
phonectl install-service      # 开机自启服务，地址 http://127.0.0.1:47321/mcp
phonectl doctor               # 检查数据线、信任、开发者模式、磁盘镜像和服务
phonectl setup claude-code    # 或者：codex、gemini（桌面版）、deepseek-harness
```

按 `setup` 的提示让 Agent 重新加载，再让它截一张手机屏幕试试。`setup` 把服务和令牌加进 Agent 的 MCP 配置，装上 `phonectl-scripts` skill，并给 Claude Code、Codex 和 Gemini 装一个钩子：Agent 这一轮结束就交还手机（Codex 会请你批准一次）。改动的文件先备份为 `<文件>.phonectl.bak`。

也可以把下面这段话发给你的编程 Agent：

> 按 https://github.com/jiefeng-ai/phonectl-mcp/blob/main/README.zh-CN.md#安装 安装 phonectl-mcp，用 `phonectl setup` 把你自己接上。运行 `phonectl doctor`，直到全部通过。需要我在手机上操作时，一步步告诉我，等我做完再继续。最后告诉我怎么重启你，让手机工具加载进来。

<details>
<summary>其他 MCP 客户端（Cursor 等支持 streamable HTTP 的客户端）</summary>

手动加入服务。这样 Agent 能操作手机，但没有那个 skill：它写的脚本不知道要把手机让给别的 Agent。

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

</details>

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

**脚本。** 固定的一串步骤写成脚本，比让模型一步一个来回更快、更省。脚本是同一个服务的又一个 MCP 客户端（同一个地址、同一个令牌），不要直接连手机。用 `phonectl setup` 接入的 Agent 会从 `phonectl-scripts` skill 读到完整的规矩。

`phonectl hold 30m` 让 Agent 在你自己用手机时让开，`phonectl hold off` 结束。

## 原理

服务通过苹果的开发者服务控制手机，用的是 [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)。点按和按键走的是 Xcode 投屏同一条通道，所以需要 iOS 27。`read_text` 在 Mac 上用苹果的 Vision 框架识别屏幕文字。手机上只会挂载苹果自己的开发者磁盘镜像，由服务自动完成：没有 WebDriverAgent，不装 App，不越狱。

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
| `PHONECTL_IDLE` | `90` | 没有表示做完的客户端，连续这么多秒不调用、又有人排队时，算它用完了 |
| `PHONECTL_WAIT` | `50` | 调用最多排队等几秒，之后返回 `WAITING`；要短于各客户端自己的工具超时 |
| `PHONECTL_MAX_HOLD` | `1800` | 一个客户端连续占用这么久以后，让排队的客户端先用 |
| `PHONECTL_LOG` | `WARNING` | 日志级别（服务为 `INFO`，会记下每次调用来自哪个客户端），日志在 `~/Library/Logs/phonectl*.log`；一个日志超过 10 MB 时，旧内容移到 `<文件名>.1`，替换更早的那份 |

</details>

## 升级和卸载

```sh
uv tool upgrade phonectl-mcp && phonectl restart && phonectl update
```

每个版本改了什么：[Releases](https://github.com/jiefeng-ai/phonectl-mcp/releases)。

卸载时，先对接过的每个 Agent 运行 `phonectl setup --remove <agent>`，再执行：

```sh
phonectl uninstall-service
uv tool uninstall phonectl-mcp
rm -r ~/.config/phonectl
```

## 协议

MIT
