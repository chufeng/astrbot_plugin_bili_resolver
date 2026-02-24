<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_bili_resolver?name=astrbot_plugin_bili_resolver&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_bili_resolver

_✨ bilibili小组件等转链的工具 ✨_

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-chufeng-blue)](https://github.com/chufeng)

</div>

AstrBot 插件 —— 自动解析群聊/私聊中的 B 站链接，返回视频信息摘要。

## 效果示例

群里有人发了一个 B 站链接或小程序卡片，机器人自动回复：

```
https://www.bilibili.com/video/av114556558967080?p=1

标题："终于知道为什么听到某些歌，反派会愣住了。因为...这也是他们的童年啊..."
小标题：TG-2025-05-23-175551094
类型：XX | UP：一罐蠢乃酱 | https://space.bilibili.com/3546772907493433

播放：359.35万 | 弹幕：2350 | 收藏：12.97万
点赞：28.47万 | 硬币：4.07万 | 评论：2422

简介：-
```

同时附带视频封面图。

## 支持的链接格式

| 类型 | 示例 |
|------|------|
| 短链 | `https://b23.tv/xxx` |
| 视频 | `bilibili.com/video/av...` 或 `BV...` |
| 番剧 | `bilibili.com/bangumi/play/ep...` / `ss...` / `md...` |
| 直播间 | `live.bilibili.com/12345` |
| 专栏文章 | `bilibili.com/read/cv...` |
| 动态 | `bilibili.com/opus/...` 或 `t.bilibili.com/...` |
| QQ 小程序卡片 | 分享 B 站内容到 QQ 的卡片消息 |

## 指令

| 指令 | 说明 |
|------|------|
| `/搜视频 关键词` | 搜索 B 站视频，返回第一个结果的解析信息 |

## 安装

**推荐**：在 AstrBot WebUI 的插件管理页面，搜索 `astrbot_plugin_bili_resolver` 一键安装。

手动安装：将插件目录放入 AstrBot 的 `data/plugins/` 目录下，重启或热重载即可。

## 配置

安装后可在 AstrBot WebUI 插件管理面板中修改，无需编辑文件。

| 配置项 | 类型 | 默认值 | 说明 |
|-------|------|-------|------|
| `enable_auto_parse` | bool | `true` | 自动解析开关 |
| `enable_search` | bool | `true` | `/搜视频` 指令开关 |
| `enable_image` | bool | `true` | 回复中是否显示封面图 |
| `group_whitelist_mode` | bool | `false` | 白名单模式（开启=仅列表中的群生效，关闭=黑名单模式） |
| `group_list` | list | `[]` | 群组 ID 列表 |

**白名单模式**：只有列表中的群触发，其他群忽略。
**黑名单模式**（默认）：列表中的群不触发，其他群正常。列表为空则所有群生效。

## 依赖

- Python >= 3.10
- AstrBot
- aiohttp
