import html
import json
import re
import urllib.parse
from pathlib import PurePosixPath
from typing import Iterator, List, Optional, Set, Tuple, Union

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from . import analysis_bilibili
from .analysis_bilibili import (
    b23_extract,
    bili_keyword,
    search_bili_by_title,
    search_bili_video_by_title,
)

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

# 允许的 bilibili 相关域名后缀（包含 CDN，用于判断图片）
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

# 内容页域名后缀：只有这些域名下的链接才值得拿去解析
_CONTENT_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bili33.cn",
)

# QQ 官方机器人群消息中，结构化卡片（小程序/图文）的真实跳转链接放在
# ark_data.fields 里；字段名在不同卡片模板下并不统一，这里按优先级逐个尝试。
ARK_URL_FIELDS = (
    "jump_url",
    "jumpUrl",
    "jumpurl",
    "qqdocurl",
    "share_url",
    "target_url",
    "link",
    "url",
)

ARK_TITLE_FIELDS = ("title", "desc", "summary", "prompt")

# 用来判断一张卡片是否来自 B 站
BILI_CARD_HINTS = ("哔哩哔哩", "bilibili", "b站", "b23")

# 卡片消息里的标题摘要行，例如「摘要: [QQ小程序]【xxx】」
_CARD_TITLE_PATTERN = re.compile(r"(?:摘要|标题)[:：]\s*(.+)")

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.I)

_SHORT_LINK_PATTERN = re.compile(
    r"(?:b23\.tv|bili(?:22|23|33|2233)\.cn)/\w+",
    re.I,
)

_CQ_JSON_PATTERN = re.compile(r"\[CQ:json,data=(.*?)\]", re.S)


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


def _is_content_domain(url: str) -> bool:
    """检查 URL 是否指向 B 站内容页（排除封面/CDN 图片等资源链接）"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in _CONTENT_DOMAINS
    )


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


def _decode_text(text: str) -> str:
    """还原卡片/CQ 码里的 HTML 实体与转义斜杠"""
    if not isinstance(text, str):
        return ""
    return html.unescape(text).replace("\\/", "/")


def _bili_url_in_text(text: str) -> str:
    """从一段文本中找出第一个 B 站内容页链接，找不到返回空字符串"""
    if not isinstance(text, str) or not text:
        return ""
    # 部分卡片会把真实链接放在跳转链接的查询参数里，解码后再扫一遍
    for candidate_text in (text, urllib.parse.unquote(_decode_text(text))):
        decoded = _decode_text(candidate_text)
        for match in _URL_PATTERN.finditer(decoded):
            url = match.group(0).rstrip(".,;:!?)]}'\"")
            if _is_content_domain(url) and not _is_image(url):
                return url
        short_link = _SHORT_LINK_PATTERN.search(decoded)
        if short_link:
            return "https://" + short_link.group(0)
    return ""


def _walk_strings(obj, depth: int = 0) -> Iterator[str]:
    """递归遍历嵌套结构中的所有字符串"""
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _walk_strings(value, depth + 1)


def _find_ark_data(obj, depth: int = 0) -> Optional[dict]:
    """在消息 payload 里找到第一个结构化卡片数据（ark_data / ark）"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key in ("ark_data", "ark"):
            candidate = obj.get(key)
            if isinstance(candidate, dict):
                return candidate
            if isinstance(candidate, str) and candidate.strip().startswith("{"):
                try:
                    parsed = json.loads(candidate)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed
        for value in obj.values():
            found = _find_ark_data(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found = _find_ark_data(value, depth + 1)
            if found:
                return found
    return None


def _ark_fields(ark: dict) -> dict:
    """取出卡片数据的 fields 字段"""
    fields = ark.get("fields")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except (json.JSONDecodeError, TypeError):
            fields = None
    return fields if isinstance(fields, dict) else {}


def _ark_url(ark: dict) -> str:
    """从卡片数据中提取 B 站内容页链接"""
    fields = _ark_fields(ark)
    for key in ARK_URL_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            url = _bili_url_in_text(value)
            if url:
                return url
    # 字段名不在预期内时，兜底扫描卡片里的全部字符串
    for value in _walk_strings(ark):
        url = _bili_url_in_text(value)
        if url:
            return url
    return ""


def _find_qqdocurl(data: dict) -> str:
    """从已解析的 JSON dict 中查找 bilibili 相关的 qqdocurl"""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    for _key, val in meta.items():
        if not isinstance(val, dict):
            continue
        for key in ("qqdocurl", "jump_url", "url"):
            raw = val.get(key)
            if isinstance(raw, str):
                url = _bili_url_in_text(raw)
                if url:
                    return url
    return ""


def _decode_cq_json(data: str) -> str:
    """还原 CQ 码里的转义字符"""
    return (
        data.replace("&amp;", "&")
        .replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
    )


def _extract_from_payload(payload, depth: int = 0) -> str:
    """从已解析的消息 payload 中提取 B 站链接"""
    if depth > 6 or not isinstance(payload, (dict, list, tuple)):
        return ""
    if isinstance(payload, dict):
        # OneBot 卡片: {"meta": {"detail_1": {"qqdocurl": "..."}}}
        url = _find_qqdocurl(payload)
        if url:
            return url

        # QQ 官方机器人: {"ark_data": {"ark_type": "miniapp", "fields": {"jump_url": "..."}}}
        ark = _find_ark_data(payload)
        if ark:
            url = _ark_url(ark)
            if url:
                return url

        # OneBot JSON 消息段: {"type": "json", "data": {"data": "{...}"}}
        if payload.get("type") in ("json", "card"):
            inner = payload.get("data")
            if isinstance(inner, str):
                url = _try_parse_json(_decode_text(inner), depth + 1)
                if url:
                    return url
            elif isinstance(inner, dict):
                for key in ("data", "content"):
                    value = inner.get(key)
                    if isinstance(value, str):
                        url = _try_parse_json(_decode_text(value), depth + 1)
                        if url:
                            return url
                url = _extract_from_payload(inner, depth + 1)
                if url:
                    return url

        # 兜底：payload 里任意字符串携带的 B 站链接
        for value in payload.values():
            if isinstance(value, str):
                url = _bili_url_in_text(value)
                if url:
                    return url
        for value in payload.values():
            if isinstance(value, (dict, list, tuple)):
                url = _extract_from_payload(value, depth + 1)
                if url:
                    return url
    else:
        for item in payload:
            url = _extract_from_payload(item, depth + 1)
            if url:
                return url
    return ""


def _try_parse_json(text: str, depth: int = 0) -> str:
    """尝试从 JSON 字符串中提取 bilibili URL"""
    if depth > 6 or not isinstance(text, str):
        return ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(data, (dict, list)):
        return _extract_from_payload(data, depth + 1)
    return ""


def _extract_from_text(text: str, depth: int = 0) -> str:
    """从纯文本 / JSON / CQ 码字符串中提取 B 站链接"""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped or depth > 6:
        return ""
    if stripped.startswith("{"):
        url = _try_parse_json(stripped, depth + 1)
        if url:
            return url
    cq_match = _CQ_JSON_PATTERN.search(stripped)
    if cq_match:
        url = _try_parse_json(_decode_cq_json(cq_match.group(1)), depth + 1)
        if url:
            return url
    return _bili_url_in_text(stripped)


def _iter_payloads(raw) -> Iterator[Union[dict, list, tuple]]:
    """遍历 raw_message 上可能承载 payload 的容器"""
    if raw is None:
        return
    if isinstance(raw, (dict, list, tuple)):
        yield raw
        return
    for attr in ("raw_data", "msg_elements", "data", "raw"):
        value = getattr(raw, attr, None)
        if isinstance(value, (dict, list, tuple)):
            yield value


def _extract_from_raw_message(raw) -> str:
    """从 raw_message 的各种可能格式中提取 B 站链接。

    raw_message 可能是:
    - dict: 已解析的 JSON 卡片 / QQ 官方 bot 原始 payload
    - list: OneBot 消息段列表 [{"type":"json","data":{"data":"{...}"}}]
    - str: CQ码字符串 或 纯 JSON 字符串
    - obj: QQ 官方 bot 的消息对象（AstrBot 会在 raw_data 上保留原始 payload，
      ark 卡片的消息正文只有「[卡片消息]...」摘要，真实链接在 ark_data.fields 里）
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return _extract_from_text(raw)
    for payload in _iter_payloads(raw):
        url = _extract_from_payload(payload)
        if url:
            return url
    return ""


def _card_from_payload(payload) -> Tuple[bool, str]:
    """判断 payload 是否为 B 站卡片，并返回它的标题"""
    if not isinstance(payload, (dict, list, tuple)):
        return False, ""
    ark = _find_ark_data(payload)
    if not ark:
        return False, ""
    fields = _ark_fields(ark)
    hints = [ark.get("ark_type"), ark.get("ark_name")]
    for key in ("tag", "tags", "source", "title", "desc"):
        value = fields.get(key)
        if isinstance(value, (list, tuple)):
            hints.extend(str(item) for item in value)
        elif value:
            hints.append(str(value))
    hint_text = " ".join(str(item) for item in hints).lower()
    if not any(hint in hint_text for hint in BILI_CARD_HINTS):
        hint_text = " ".join(_walk_strings(ark)).lower()
        if not any(hint in hint_text for hint in BILI_CARD_HINTS):
            return False, ""
    for key in ARK_TITLE_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return True, value.strip()
    return True, ""


def _card_from_text(text: str) -> Tuple[bool, str]:
    """从卡片消息的摘要文本里判断是否为 B 站卡片，并取出标题"""
    if not isinstance(text, str) or not text:
        return False, ""
    if "卡片消息" not in text and "QQ小程序" not in text:
        return False, ""
    lowered = text.lower()
    if not any(hint in lowered for hint in BILI_CARD_HINTS):
        return False, ""
    match = _CARD_TITLE_PATTERN.search(text)
    if match:
        # 去掉「[QQ小程序]」这类来源前缀
        title = re.sub(r"^\[[^\]]*\]\s*", "", match.group(1).strip())
        if title:
            return True, title
    return True, ""


def _extract_card_title(raw, text: str = "") -> str:
    """卡片消息没有直链时，用卡片标题兜底搜索；非 B 站卡片返回空字符串"""
    for payload in _iter_payloads(raw):
        is_card, title = _card_from_payload(payload)
        if is_card:
            return title
    is_card, title = _card_from_text(text)
    return title if is_card else ""


def _describe_raw(raw, limit: int = 2000) -> str:
    """把 raw_message 序列化成便于排查的字符串（DEBUG 级别下使用）"""
    payload = getattr(raw, "raw_data", None)
    if payload is None:
        payload = raw
    try:
        description = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        description = repr(payload)
    return description[:limit]


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
    "1.0.4",
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
        # 卡片消息没有直链时，是否用卡片标题去 B 站搜索
        self.enable_card_title_search = config.get(
            "enable_card_title_search", True
        )

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
        raw_message = (
            event.message_obj.raw_message if event.message_obj else None
        )

        # 尝试从卡片消息（QQ 小程序 / QQ 官方 bot ark 卡片）中提取 URL
        json_url = _extract_from_raw_message(raw_message)
        if not json_url and event.message_obj:
            for comp in event.message_obj.message or []:
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

        card_title = ""
        if not json_url:
            card_title = _extract_card_title(raw_message, text)
            if "卡片消息" in text or "QQ小程序" in text:
                # 卡片拿不到直链时输出原始 payload，方便排查不同卡片模板的字段
                logger.debug(
                    f"未取到直链的卡片原始数据: {_describe_raw(raw_message)}"
                )

        if json_url:
            logger.info(f"从卡片消息提取到 URL: {json_url}")
            text = json_url
        elif card_title and self.enable_card_title_search:
            logger.info(f"卡片消息无直链，按标题搜索: {card_title}")
            try:
                session = await self._get_session()
                searched = await search_bili_video_by_title(
                    card_title, session=session
                )
            except Exception as e:
                logger.error(f"卡片标题搜索出错: {e!r}", exc_info=True)
                return
            if not searched:
                logger.info(f"卡片标题搜索无结果: {card_title}")
                return
            text = searched
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
