import time
import urllib.parse
from functools import reduce
from hashlib import md5
from typing import Dict, Optional, Tuple

from aiohttp import ClientSession

# doc: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

# fmt: off
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]
# fmt: on

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

# WBI key cache: (img_key, sub_key, timestamp)
_wbi_key_cache: Optional[Tuple[str, str, float]] = None
_WBI_KEY_TTL = 30 * 60  # 30 minutes


def get_mixin_key(orig: str) -> str:
    """对 imgKey 和 subKey 进行字符顺序打乱编码"""
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> Dict[str, str]:
    """为请求参数进行 wbi 签名"""
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())
    params["wts"] = curr_time
    params = dict(sorted(params.items()))
    # 过滤 value 中的 "!'()*" 字符
    params = {
        k: "".join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in params.items()
    }
    query = urllib.parse.urlencode(params)
    wbi_sign = md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = wbi_sign
    return params


async def get_wbi_keys(
    session: Optional[ClientSession] = None,
) -> Tuple[str, str]:
    """获取最新的 img_key 和 sub_key，带 30 分钟缓存"""
    global _wbi_key_cache

    now = time.time()
    if _wbi_key_cache is not None:
        img_key, sub_key, cached_at = _wbi_key_cache
        if now - cached_at < _WBI_KEY_TTL:
            return img_key, sub_key

    owns_session = session is None
    if owns_session:
        session = ClientSession(headers=HEADERS)
    try:
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            timeout=15,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"WBI nav request failed with status {resp.status}"
                )
            json_content = await resp.json()
    finally:
        if owns_session:
            await session.close()

    img_url: str = json_content["data"]["wbi_img"]["img_url"]
    sub_url: str = json_content["data"]["wbi_img"]["sub_url"]
    img_key = img_url.rsplit("/", 1)[1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]

    _wbi_key_cache = (img_key, sub_key, now)
    return img_key, sub_key


async def get_query(
    params: dict,
    session: Optional[ClientSession] = None,
) -> str:
    """获取签名后的查询参数"""
    img_key, sub_key = await get_wbi_keys(session=session)
    signed_params = enc_wbi(params=params, img_key=img_key, sub_key=sub_key)
    query = urllib.parse.urlencode(signed_params)
    return query
