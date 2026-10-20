# 更新日志

## v1.0.4

- 修复 QQ 官方机器人（qq_official）下 B 站小程序 / ark 卡片无法识别的问题
  - QQ 官方 API 的卡片消息正文只有「[卡片消息] 小程序 / 摘要 / source」摘要文本，
    链接可能在 `ark_data.fields` 里；插件现在会读取 AstrBot 消息对象
    `raw_message.raw_data` 中的 `ark_data`（以及 `msg_elements` 里的引用卡片）
  - 实测部分 B 站小程序卡片只下发 title / preview / source，没有 `jump_url`：
    此时按卡片标题搜索兜底，并对查询词做规范化、对结果做标题相似度校验，
    相似度低于 0.85 不回复，避免推送无关视频
  - 搜索接口补上 Referer / Origin，并在触发风控（返回 v_voucher）时自动重试一次
- 识别链接时排除封面 / CDN 图片地址，避免把 `i0.hdslb.com` 封面当成分享链接
- 新增配置 `enable_card_title_search`：卡片没有直链时按标题搜索（默认开启）

## v1.0.3

- 新增视频解析排版风格选择：原始格式 / 简洁风格 / 自定义
- 自定义模式支持变量：`${标题}` `${UP主}` `${UP主链接}` `${简介}` `${封面}` `${点赞}` `${投币}` `${收藏}` `${转发}` `${观看}` `${弹幕数量}` `${评论}` `${链接}` `${发布时间}` `${类型}` `${BV号}` `${时长}` `${版权}`

**原始格式**（默认）：

```
https://www.bilibili.com/video/BVxxxxxxxxxx

标题："视频标题"
类型：分区 | UP：UP主名称 | https://space.bilibili.com/xxx

播放：xxx万 | 弹幕：xxx | 收藏：xxx
点赞：xxx | 硬币：xxx | 评论：xxx

简介：-
```

**简洁风格**：

```
🎬 标题：视频标题
👤 UP主：UP主名称
📝 简介：-
[封面图]
👍 点赞：xxx 🪙 投币：xxx
❤️ 收藏：xxx 🔄 转发：xxx
👀 观看：xxx 💬 弹幕：xxx
```

## v1.0.2

- 更新插件介绍
- 修复多段文字被拆成多条消息发送的问题
