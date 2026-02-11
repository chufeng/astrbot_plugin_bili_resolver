import re
import json
from typing import List, Union

from aiohttp import ClientSession
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from . import analysis_bilibili
from .analysis_bilibili import b23_extract, bili_keyword, search_bili_by_title

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.69"
}

BILI_PATTERN = re.compile(
    r"(b23\.tv)|(bili(22|23|33|2233)\.cn)|(\.bilibili\.com)"
    r"|(^(av|cv)(\d+))|(^BV([a-zA-Z0-9]{10})+)"
    r"|(\[\[QQ小程序\]哔哩哔哩\])|(QQ小程序&amp;#93;哔哩哔哩)|(QQ小程序&#93;哔哩哔哩)",
    re.I,
)

IMAGE_SUFFIXES = {".jpg", "jpeg", ".png", ".gif", ".bmp", "jfif", "webp"}


def _find_qqdocurl(data: dict) -> str:
    """从已解析的 JSON dict 中查找 bilibili 相关的 qqdocurl"""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    for _key, val in meta.items():
        if isinstance(val, dict):
            url = val.get("qqdocurl", "") or val.get("url", "")
            if url and ("bilibili" in url or "b23.tv" in url or "bili" in url):
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
            # {"type": "json", "data": {"data": "{...json...}"}}
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
            # CQ码中逗号等字符会被转义
            cq_data = cq_data.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")
            url = _try_parse_json(cq_data)
            if url:
                return url

    return ""


def _is_image(msg: str) -> bool:
    """判断字符串是否为图片 URL"""
    return isinstance(msg, str) and len(msg) > 4 and msg[-4:].lower() in IMAGE_SUFFIXES


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
    for i in flatten_msg_list:
        if not i:
            continue
        elif _is_image(i):
            # 确保图片 URL 以 http 开头
            url = i if i.startswith("http") else f"https:{i}"
            chain.append(Comp.Image.fromURL(url))
        else:
            chain.append(Comp.Plain(str(i)))
    return chain


@register(
    "astrbot_plugin_bili_resolver",
    "chufeng",
    "Bilibili的小组件最麻烦了，电脑打不开，于是做了个可以直接转成原链接和展示播放和介绍的工具",
    "1.0.1",
    "https://github.com/chufeng/astrbot_plugin_bili_resolver",
)
class BilibiliAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.trust_env = False

        # 功能开关
        self.enable_auto_parse = config.get("enable_auto_parse", True)
        self.enable_search = config.get("enable_search", True)

        # 图片开关，同步到 analysis_bilibili 模块
        analysis_bilibili.analysis_display_image = config.get("enable_image", True)

        # 群组白名单/黑名单
        self.group_whitelist_mode = config.get("group_whitelist_mode", False)
        self.group_list = [str(g) for g in config.get("group_list", [])]

    def _check_group(self, group_id: str) -> bool:
        """检查群组是否允许使用。返回 True 表示允许。"""
        if not group_id or not self.group_list:
            # 没有群号或列表为空，不做限制
            return True
        if self.group_whitelist_mode:
            # 白名单模式：只有列表中的群才生效
            return group_id in self.group_list
        else:
            # 黑名单模式：列表中的群不生效
            return group_id not in self.group_list

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自动解析消息中的 Bilibili 链接"""
        if not self.enable_auto_parse:
            return

        # 群组白名单/黑名单检查
        group_id = event.message_obj.group_id if event.message_obj else None
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()

        # 尝试从 QQ小程序 JSON 卡片中提取 URL
        json_url = ""
        if event.message_obj:
            # 从 raw_message 提取
            json_url = _extract_from_raw_message(event.message_obj.raw_message)
            # 从消息链各组件中查找
            if not json_url and event.message_obj.message:
                for comp in event.message_obj.message:
                    raw = getattr(comp, "raw", None) or getattr(comp, "data", None)
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
            async with ClientSession(trust_env=self.trust_env, headers=HEADERS) as session:
                if re.search(r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I):
                    text = await b23_extract(text, session=session)

                msg = await bili_keyword(group_id, text, session=session)
        except Exception as e:
            logger.error(f"Bilibili 解析出错: {e}")
            event.stop_event()
            return

        if not msg:
            event.stop_event()
            return

        if isinstance(msg, str):
            if msg:
                yield event.plain_result(msg)
            event.stop_event()
            return

        chain = _format_msg(msg)
        if chain:
            yield event.chain_result(chain)
        event.stop_event()

    @filter.command("搜视频")
    async def search_video(self, event: AstrMessageEvent):
        """通过关键词搜索 Bilibili 视频"""
        if not self.enable_search:
            return

        group_id = event.message_obj.group_id if event.message_obj else None
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
            event.stop_event()
            return

        try:
            async with ClientSession(trust_env=self.trust_env, headers=HEADERS) as session:
                search_url = await search_bili_by_title(text, session=session)
                if not search_url:
                    yield event.plain_result("未找到相关视频")
                    event.stop_event()
                    return

                msg = await bili_keyword(group_id, search_url, session=session)
        except Exception as e:
            logger.error(f"Bilibili 搜索出错: {e}")
            yield event.plain_result("搜索出错，请稍后再试")
            event.stop_event()
            return

        if not msg:
            yield event.plain_result("解析失败")
            event.stop_event()
            return

        if isinstance(msg, str):
            if msg:
                yield event.plain_result(msg)
            event.stop_event()
            return

        chain = _format_msg(msg)
        if chain:
            yield event.chain_result(chain)
        event.stop_event()

    async def terminate(self):
        """插件被卸载/停用时调用"""
        pass
