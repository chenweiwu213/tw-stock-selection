"""CMoney 網站共用爬取工具(NUXT SSR payload 解析 + 熱門貼文API)。"""

import re
from datetime import timedelta, timezone
from py_mini_racer import MiniRacer

from app.common.http import get_relaxed_session

SECTOR_CATEGORY_SOURCE_URL = "https://www.cmoney.tw/forum/category/C11010"
RECOMMENDATION_URL = "https://www.cmoney.tw/api/mach/api/Article/Recommendation"
FETCH_PER_PAGE = 10  # API上限，超過會回400 "fetch is invalid."
MAX_PAGES = 100
TAIWAN_TZ = timezone(timedelta(hours=8))

CMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

_DEEP_FIND_JS = """
function deepFind(obj, key, maxDepth, seen) {
    if (!obj || typeof obj !== 'object' || maxDepth < 0) return undefined;
    if (seen.indexOf(obj) !== -1) return undefined;
    seen.push(obj);
    if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
    var children;
    if (obj instanceof Map) {
        children = [];
        obj.forEach(function(v) { children.push(v); });
    } else if (Array.isArray(obj)) {
        children = obj;
    } else {
        children = Object.keys(obj).map(function(k) { return obj[k]; });
    }
    for (var i = 0; i < children.length; i++) {
        var found = deepFind(children[i], key, maxDepth - 1, seen);
        if (found !== undefined) return found;
    }
    return undefined;
}
"""


def extract_nuxt_script(html: str) -> str:
    """從 CMoney 頁面 HTML 取出 window.__NUXT__=(function(...){...})(...) 區塊，
    資料全都寫在HTML裡，Network裡面找不到。"""
    marker = "window.__NUXT__=(function("
    start_idx = html.find(marker)
    if start_idx == -1:
        raise ValueError("找不到 window.__NUXT__ 區塊，CMoney網站可能已改版")
    end_idx = html.find("</script>", start_idx)
    if end_idx == -1:
        raise ValueError("找不到對應的 </script> 結尾，CMoney網站可能已改版")
    code = html[start_idx:end_idx]
    return re.sub(r";\s*$", "", code)


def fetch_cmoney_nuxt_code(url: str = SECTOR_CATEGORY_SOURCE_URL) -> str:
    """抓取指定 CMoney 頁面並回傳抽出的 __NUXT__ 區塊原始碼。"""
    session = get_relaxed_session()
    resp = session.get(url, headers=CMONEY_HEADERS, timeout=15)
    resp.raise_for_status()
    return extract_nuxt_script(resp.text)


def run_nuxt_js(nuxt_code: str, body_js: str) -> str:
    """組合並執行「NUXT區塊 + 共用deepFind + 呼叫端自訂邏輯」的JS程式，回傳字串結果。

    body_js 需 return 字串(通常是 JSON.stringify(...))，可直接呼叫已注入的
    deepFind(obj, key, maxDepth, seen)。
    """
    js_program = (
        "var window = {};\n"
        + nuxt_code + ";\n"
        + "(function() {\n"
        + _DEEP_FIND_JS
        + body_js
        + "\n})();\n"
    )
    ctx = MiniRacer()
    return ctx.eval(js_program)


def extract_cmoney_bearer_token(nuxt_code: str) -> str:
    """從 __NUXT__ 區塊解析出訪客登入用的 Bearer token(token.tokens.at)。"""
    body_js = (
        "    var tokenObj = deepFind(window.__NUXT__, 'tokens', 14, []);\n"
        "    if (!tokenObj || !tokenObj.at) {\n"
        "        throw new Error('找不到 tokens.at，CMoney 登入資料結構可能已改版');\n"
        "    }\n"
        "    return tokenObj.at;\n"
    )
    token = run_nuxt_js(nuxt_code, body_js)
    if not token or not isinstance(token, str):
        raise ValueError("解析出的 CMoney Bearer token 為空")
    return token


def fetch_bearer_token() -> str:
    """抓取CMoney訪客登入用的Bearer token，供呼叫Recommendation等需授權的API使用。"""
    nuxt_code = fetch_cmoney_nuxt_code(SECTOR_CATEGORY_SOURCE_URL)
    return extract_cmoney_bearer_token(nuxt_code)


def fetch_recommendation_page(session, token: str, offset: int, fetch: int = FETCH_PER_PAGE) -> dict:
    """呼叫CMoney熱門貼文API，回傳原始JSON(含articles/hasNext/nextOffset)。"""
    headers = dict(CMONEY_HEADERS)
    headers["accept"] = "application/json, text/plain, */*"
    headers["authorization"] = f"Bearer {token}"
    headers["x-version"] = "3.0"
    headers["referer"] = "https://www.cmoney.tw/forum/popular/buzz?tab=popular"
    resp = session.get(
        RECOMMENDATION_URL,
        headers=headers,
        params={"fetch": fetch, "offset": offset, "includingGroupArticle": "false"},
        timeout=15,
    )
    if not resp.ok:
        print(f"[錯誤] {resp.status_code} 回應內容：{resp.text[:1000]}")
    resp.raise_for_status()
    return resp.json()