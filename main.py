import re
import json
import urllib.parse
from pathlib import PurePosixPath
from typing import List, Optional, Set, Union

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from . import analysis_bilibili
from .analysis_bilibili import b23_extract, bili_keyword, search_bili_by_title

TEMPLATE_PRESET_EMOJI = (
    "🎬 标题：${标题}\n"
    "👤 UP主：${UP主}\n"
    "📝 简介：${简介}\n"
    "${封面}\n"
    "👍 点赞：${点赞} 🪙 投币：${投币}\n"
    "❤️ 收藏：${收藏} 🔄 转发：${转发}\n"
    "👀 观看：${观看} 💬 弹幕：${弹幕数量}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.69"
}

DEFAULT_TIMEOUT = ClientTimeout(total=15)

BILI_PATTERN = re.compile(
    r"(b23\.tv)|(bili(22|23|33|2233)\.cn)|(\.bilibili\.com)"
    r"|(\b(av|cv)(\d+))|\b(BV([a-zA-Z0-9]{10})+)"
    r"|(\[\[QQ小程序\]哔哩哔哩\])|(QQ小程序&amp;#93;哔哩哔哩)"
    r"|(QQ小程序&#93;哔哩哔哩)",
    re.I,
)

IMAGE_SUFFIXES: Set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".jfif", ".webp",
}

# 允许的 bilibili 相关域名后缀
_ALLOWED_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "bilivideo.com",
    "bilivideo.cn",
    "bilivideo.net",
    "hdslb.com",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bili33.cn",
)


def _is_allowed_domain(url: str) -> bool:
    """检查 URL 的域名是否在 bilibili 白名单内"""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        host = host.lower().rstrip(".")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_DOMAINS
        )
    except Exception:
        return False


def _find_qqdocurl(data: dict) -> str:
    """从已解析的 JSON dict 中查找 bilibili 相关的 qqdocurl"""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    for _key, val in meta.items():
        if isinstance(val, dict):
            url = val.get("qqdocurl", "") or val.get("url", "")
            if url and _is_allowed_domain(url):
                return url
    return ""


def _try_parse_json(text: str) -> str:
    """尝试从 JSON 字符串中提取 bilibili URL"""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _find_qqdocurl(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _extract_from_raw_message(raw) -> str:
    """从 raw_message 的各种可能格式中提取 QQ小程序 bilibili URL。

    raw_message 可能是:
    - dict: 已解析的 JSON 卡片
    - list: OneBot 消息段列表 [{"type":"json","data":{"data":"{...}"}}]
    - str: CQ码字符串 或 纯 JSON 字符串
    """
    if raw is None:
        return ""

    # 1) raw 本身是 dict（已解析的 JSON 卡片）
    if isinstance(raw, dict):
        url = _find_qqdocurl(raw)
        if url:
            return url
        # 可能是单个 OneBot 消息段: {"type":"json","data":{"data":"{...}"}}
        if raw.get("type") == "json":
            inner = raw.get("data", {})
            if isinstance(inner, dict):
                json_str = inner.get("data", "")
                if isinstance(json_str, str):
                    url = _try_parse_json(json_str)
                    if url:
                        return url

    # 2) raw 是 list（OneBot 消息段列表）
    if isinstance(raw, list):
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "json":
                inner = seg.get("data", {})
                if isinstance(inner, dict):
                    json_str = inner.get("data", "")
                    if isinstance(json_str, str):
                        url = _try_parse_json(json_str)
                        if url:
                            return url
                elif isinstance(inner, str):
                    url = _try_parse_json(inner)
                    if url:
                        return url

    # 3) raw 是 str
    if isinstance(raw, str):
        raw_str = raw.strip()
        # 3a) 纯 JSON 字符串
        if raw_str.startswith("{"):
            url = _try_parse_json(raw_str)
            if url:
                return url
        # 3b) CQ码: [CQ:json,data=...] — data 内容可能经过转义
        cq_match = re.search(r'\[CQ:json,data=(.*?)\]', raw_str, re.S)
        if cq_match:
            cq_data = cq_match.group(1)
            # CQ码中逗号等字符会被转义，&amp; 必须最先解码
            cq_data = (
                cq_data
                .replace("&amp;", "&")
                .replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
            )
            url = _try_parse_json(cq_data)
            if url:
                return url

    return ""


def _is_image(msg: str) -> bool:
    """判断字符串是否为图片 URL"""
    if not isinstance(msg, str) or not msg:
        return False
    try:
        parsed = urllib.parse.urlparse(msg)
        suffix = PurePosixPath(parsed.path).suffix.lower()
        return suffix in IMAGE_SUFFIXES
    except Exception:
        return False


def _flatten(container):
    """递归展开嵌套列表"""
    for i in container:
        if isinstance(i, (list, tuple)):
            yield from _flatten(i)
        else:
            yield i


def _format_msg(msg_list: List[Union[List[str], str]]) -> list:
    """将消息列表转换为 AstrBot 消息链"""
    flatten_msg_list = list(_flatten(msg_list))
    chain = []
    text_buffer = ""
    for i in flatten_msg_list:
        if not i:
            continue
        if _is_image(i):
            if text_buffer:
                chain.append(Comp.Plain(text_buffer))
                text_buffer = ""
            if i.startswith("http"):
                url = i
            elif i.startswith("//"):
                url = f"https:{i}"
            else:
                url = f"https://{i}"
            chain.append(Comp.Image.fromURL(url))
        else:
            text_buffer += str(i)
    if text_buffer:
        chain.append(Comp.Plain(text_buffer))
    return chain


@register(
    "astrbot_plugin_bili_resolver",
    "chufeng",
    "bilibili小组件等转链的工具,方便PC查看链接,"
    "因为之前用其他的转链总是被踢下线,所以自己写了个简单版的,"
    "从发布以来还没被踢下线",
    "1.0.3",
    "https://github.com/chufeng/astrbot_plugin_bili_resolver",
)
class BilibiliAnalysis(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.trust_env = False
        self._session: Optional[ClientSession] = None

        # 功能开关
        self.enable_auto_parse = config.get("enable_auto_parse", True)
        self.enable_search = config.get("enable_search", True)

        # 图片开关，同步到 analysis_bilibili 模块
        analysis_bilibili.analysis_display_image = config.get(
            "enable_image", True
        )

        # 视频排版模板（根据预设选择）
        preset = config.get("template_preset", "原始格式")
        if preset == "原始格式":
            analysis_bilibili.analysis_video_template = ""
        elif preset == "简洁风格":
            analysis_bilibili.analysis_video_template = TEMPLATE_PRESET_EMOJI
        else:  # 自定义
            analysis_bilibili.analysis_video_template = config.get(
                "video_template", ""
            )

        # 群组白名单/黑名单
        self.group_whitelist_mode = config.get("group_whitelist_mode", False)
        self.group_list = [str(g) for g in config.get("group_list", [])]

    async def _get_session(self) -> ClientSession:
        """懒初始化并复用 ClientSession"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                trust_env=self.trust_env,
                headers=HEADERS,
                timeout=DEFAULT_TIMEOUT,
            )
        return self._session

    def _check_group(self, group_id: str) -> bool:
        """检查群组是否允许使用。返回 True 表示允许。"""
        if not group_id or not self.group_list:
            return True
        if self.group_whitelist_mode:
            return group_id in self.group_list
        else:
            return group_id not in self.group_list

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自动解析消息中的 Bilibili 链接"""
        if not self.enable_auto_parse:
            return

        # 群组白名单/黑名单检查
        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()

        # 尝试从 QQ小程序 JSON 卡片中提取 URL
        json_url = ""
        if event.message_obj:
            json_url = _extract_from_raw_message(
                event.message_obj.raw_message
            )
            if not json_url and event.message_obj.message:
                for comp in event.message_obj.message:
                    raw = getattr(comp, "raw", None) or getattr(
                        comp, "data", None
                    )
                    if raw:
                        json_url = _extract_from_raw_message(raw)
                        if json_url:
                            break

        # message_str 本身可能就是 JSON
        if not json_url and text.startswith("{"):
            json_url = _try_parse_json(text)

        if json_url:
            logger.info(f"从 JSON 卡片提取到 URL: {json_url}")
            text = json_url
        elif not text or not BILI_PATTERN.search(text):
            return

        try:
            session = await self._get_session()
            if re.search(
                r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I
            ):
                text = await b23_extract(text, session=session)

            msg = await bili_keyword(group_id, text, session=session)
        except Exception as e:
            logger.error(f"Bilibili 解析出错: {e!r}", exc_info=True)
            return

        if not msg:
            return

        # 只在有结果后才阻断
        event.stop_event()

        if isinstance(msg, str):
            if msg:
                yield event.plain_result(msg)
            return

        chain = _format_msg(msg)
        if chain:
            yield event.chain_result(chain)

    @filter.command("搜视频")
    async def search_video(self, event: AstrMessageEvent):
        """通过关键词搜索 Bilibili 视频"""
        if not self.enable_search:
            return

        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()
        # 去除指令前缀
        for prefix in ["/搜视频", "搜视频"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        if not text:
            yield event.plain_result("请输入搜索关键词，例如：/搜视频 猫咪")
            return

        event.stop_event()

        try:
            session = await self._get_session()
            search_url = await search_bili_by_title(text, session=session)
            if not search_url:
                yield event.plain_result("未找到相关视频")
                return

            msg = await bili_keyword(group_id, search_url, session=session)
        except Exception as e:
            logger.error(f"Bilibili 搜索出错: {e!r}", exc_info=True)
            yield event.plain_result("搜索出错，请稍后再试")
            return

        if not msg:
            yield event.plain_result("解析失败")
            return

        if isinstance(msg, str):
            if msg:
                yield event.plain_result(msg)
            return

        chain = _format_msg(msg)
        if chain:
            yield event.chain_result(chain)

    async def terminate(self):
        """插件被卸载/停用时调用，关闭持久化 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
