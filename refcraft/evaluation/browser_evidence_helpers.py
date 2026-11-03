"""Trusted read-only inspection and finite native-range actions for follow-ups.

Not hot-installed into the active evidence producers: their runtime is frozen.
No generated JavaScript or application callbacks are executed by these helpers.
"""
import math


RANGE_STATE_JS = """e => {
 const s = getComputedStyle(e);
 const native = e instanceof HTMLInputElement && e.type === 'range';
 return {native, value: native ? e.valueAsNumber : null,
  min: native ? Number(e.min || 0) : null, max: native ? Number(e.max || 100) : null,
  disabled: !!e.disabled, writing_mode: s.writingMode, direction: s.direction};
}"""

TARGET_STATE_JS = """e => {
 const r=e.getBoundingClientRect(), s=getComputedStyle(e);
 return {tag:e.tagName,id:e.id,role:e.getAttribute('role'),text:(e.innerText||e.textContent||'').slice(0,12000),
  box:{x:r.x,y:r.y,width:r.width,height:r.height,document_x:r.x+scrollX,document_y:r.y+scrollY},
  intersects_viewport:r.right>0&&r.bottom>0&&r.left<innerWidth&&r.top<innerHeight,
  has_layout_box:!!(r.width&&r.height),focused:document.activeElement===e,focus_visible:e.matches(':focus-visible'),
  style:Object.fromEntries(['display','visibility','opacity','font-family','font-size','line-height','font-weight',
   'outline-style','outline-width','outline-color','outline-offset','box-shadow','border-radius','transform',
   'transition-property','transition-duration','animation-name','animation-duration','overflow-x','overflow-y']
   .map(k=>[k,s.getPropertyValue(k)]))};
}"""


def range_drag_points(box, state, fraction):
    def finite(value): return type(value) in (int, float) and math.isfinite(value)
    if not finite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("invalid_range_fraction")
    if not state.get("native") or state.get("disabled"):
        raise ValueError("unsupported_range_control: require enabled native input[type=range]")
    if state.get("writing_mode") != "horizontal-tb" or state.get("direction") not in {"ltr", "rtl"}:
        raise ValueError("unsupported_range_orientation: use another trusted action")
    if not box or any(not finite(box.get(k)) for k in ("x", "y", "width", "height")) or box["width"] <= 16 or box["height"] <= 0:
        raise ValueError("unsupported_range_geometry")
    if any(not finite(state.get(k)) for k in ("min", "max", "value")) or not state["min"] <= state["value"] <= state["max"] or state["min"] == state["max"]:
        raise ValueError("invalid_range_numeric_state")
    ratio = (state["value"] - state["min"]) / (state["max"] - state["min"])
    if state["direction"] == "rtl": ratio, fraction = 1 - ratio, 1 - fraction
    span = box["width"] - 16
    points = box["x"] + 8 + span * ratio, box["x"] + 8 + span * fraction, box["y"] + box["height"] / 2
    if not all(math.isfinite(v) for v in points): raise ValueError("nonfinite_pointer_coordinate")
    return points


def drag_native_range(page, locator, fraction):
    locator.scroll_into_view_if_needed()
    start, end, y = range_drag_points(locator.bounding_box(), locator.evaluate(RANGE_STATE_JS), fraction)
    page.mouse.move(start, y); page.mouse.down()
    try:
        page.mouse.move(end, y, steps=10)
    finally:
        page.mouse.up()


def inspect_target(locator):
    if locator.count() != 1: raise ValueError("inspection_target_not_unique")
    return locator.evaluate(TARGET_STATE_JS)


def focused_element(page):
    return page.locator(':focus').evaluate(TARGET_STATE_JS) if page.locator(':focus').count() == 1 else None
