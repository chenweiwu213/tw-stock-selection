"""help 圖示的提示文字位置統一：一律往圖示的右上方展開(Streamlit 原生提示文字會依空間在左上／右上之間自動切換，不固定)。"""

import json
import streamlit.components.v1 as components

_SCRIPT_ID = "help-tooltip-top-right-js"
_STYLE_ID = "help-tooltip-holder-css"
_HOLDER_ATTR = "data-help-tooltip-holder"
# 用 CSS 規則(而非直接改 inline style)隱藏高度0的 iframe 外層容器：Streamlit 切換分頁時會沿用同一個
# 外層容器節點放新分頁的第一個元素，inline style 會殘留而把新元素(如 st.header)一起藏起來；
# 規則只在容器「目前」還包著這個 iframe 時才生效，iframe 一被銷毀就自動失效。
_HOLDER_CSS = (
    '[data-testid="stElementContainer"]:has(iframe[' + _HOLDER_ATTR + ']),'
    '.element-container:has(iframe[' + _HOLDER_ATTR + ']){display:none !important;}'
)
_VIEWPORT_MARGIN_PX = 8

# 這段 JS 會被塞進「父頁面」執行(而不是留在 components.html 的 iframe 裡)，
# 因為 iframe 會隨頁面切換被銷毀，observer 也會跟著失效；放在父頁面才會一直有效。
_PARENT_JS = """
(function () {
  if (window.__helpTooltipTopRight) return;
  window.__helpTooltipTopRight = true;
  var MARGIN = %d;

  function currentDx(tip) {
    var v = tip.style.translate;
    return v ? (parseFloat(v) || 0) : 0;
  }

  function place(tip) {
    var hit = document.querySelector('[data-testid="stTooltipHoverTarget"][aria-describedby="' + tip.id + '"]');
    if (!hit || !hit.querySelector('button[aria-label^="Help"]')) return;  // 只處理 help 圖示，不動按鈕等其他元件自帶的提示
    var cur = currentDx(tip);
    var box = tip.getBoundingClientRect();
    var icon = hit.getBoundingClientRect();
    var vw = document.documentElement.clientWidth;
    // 右邊放得下：讓提示框左緣對齊圖示左緣(往右展開)；放不下就維持原生位置，避免被切到畫面外
    var want = (icon.left + box.width <= vw - MARGIN) ? icon.left - (box.left - cur) : 0;
    if (Math.abs(want - cur) > 0.5) tip.style.translate = want + 'px 0px';
  }

  function watch(tip) {
    if (tip.__helpTipWatched) return;
    tip.__helpTipWatched = true;
    place(tip);
    new MutationObserver(function () { place(tip); })
      .observe(tip, { attributes: true, attributeFilter: ['style'] });
  }

  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.getAttribute('role') === 'tooltip') watch(node);
    var found = node.querySelectorAll('[role="tooltip"]');
    for (var i = 0; i < found.length; i++) watch(found[i]);
  }

  new MutationObserver(function (records) {
    records.forEach(function (r) { r.addedNodes.forEach(scan); });
  }).observe(document.body, { childList: true, subtree: true });
})();
""" % _VIEWPORT_MARGIN_PX


def apply_help_tooltip_top_right() -> None:
    """讓所有 help 圖示的提示文字固定顯示在圖示右上方；重複呼叫無副作用，各分頁 render 函式開頭呼叫一次即可。"""
    components.html(
        f"""<script>
(function () {{
  try {{
    var pd = window.parent.document;
    if (!pd.getElementById({json.dumps(_SCRIPT_ID)})) {{
      var s = pd.createElement('script');
      s.id = {json.dumps(_SCRIPT_ID)};
      s.textContent = {json.dumps(_PARENT_JS)};
      pd.head.appendChild(s);
    }}
    if (!pd.getElementById({json.dumps(_STYLE_ID)})) {{
      var st = pd.createElement('style');
      st.id = {json.dumps(_STYLE_ID)};
      st.textContent = {json.dumps(_HOLDER_CSS)};
      pd.head.appendChild(st);
    }}
    if (window.frameElement) window.frameElement.setAttribute({json.dumps(_HOLDER_ATTR)}, '1');  // 標記高度0的iframe，讓上面的 CSS 隱藏其外層容器，避免多出一段空白間距
  }} catch (e) {{}}
}})();
</script>""",
        height=0,
    )