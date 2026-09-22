import logging
from pathlib import Path
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.todo")

_DEFAULT_ITEMS = ["[ ] 请在 WebUI 开荒"]

_INITIALIZED = set()

MANUAL_HOLD_SEC = 15.0

@register
class TodoWidget(Widget):
    name = "todo"
    interval = 1.0
    orientations = BOTH
    priority = 40

    config_schema = [
        Field("path", "str", "todo.md 路径", default="./todo.md"),
        Field("items", "list_str", "待办列表",
              default=list(_DEFAULT_ITEMS),
              help="每行一条。[ ] 未完成，[x] 已完成"),
        Field("cycle_sec", "float", "轮播周期(秒)", default=4.0,
              min=2.0, max=30.0, step=0.5),
    ]

    def _parse_md(self, content):
        out = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("- [") or s.startswith("* ["):
                out.append(s[2:].strip())
            elif s.startswith("- ") or s.startswith("* "):
                out.append("[ ] " + s[2:].strip())
            elif s.startswith("[ ]") or s.startswith("[x]") or s.startswith("[X]"):
                out.append(s)
            else:
                out.append("[ ] " + s)
        return out

    def _write_md(self, path, items):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "# Todo\n\n"
            for it in items:
                s = (it or "").strip()
                if not s:
                    continue
                if s.startswith("- ["):
                    text += s + "\n"
                elif s.lower().startswith("[x]"):
                    text += "- [x] " + s[3:].strip() + "\n"
                elif s.startswith("[ ]"):
                    text += "- [ ] " + s[3:].strip() + "\n"
                else:
                    text += "- [ ] " + s + "\n"
            path.write_text(text, "utf-8")
            log.info("todo.md 已写入: %s (%d 条)", path, len(items))
        except Exception as e:
            log.warning("写入 %s 失败: %s", path, e)

    def setup(self, ctx):
        cfg = self.get_config(ctx)
        section = ctx.config.setdefault("widget_config", {}).setdefault(self.name, {})
        path = Path(cfg["path"]).expanduser()
        ctx.state["_path"] = str(path)
        ctx.state["_last_key"] = None
        ctx.state["_selected"] = 0
        ctx.state["_manual_ts"] = 0.0

        cfg_items = list(cfg.get("items") or _DEFAULT_ITEMS)

        if self.name not in _INITIALIZED:
            _INITIALIZED.add(self.name)
            default_items = list(_DEFAULT_ITEMS)
            if path.exists() and cfg_items == default_items:
                try:
                    file_items = self._parse_md(path.read_text("utf-8", "ignore"))
                    if file_items:
                        cfg_items = file_items
                        section["items"] = list(cfg_items)
                        log.info("从 md 载入 %d 条", len(cfg_items))
                except Exception as e:
                    log.warning("读取 %s 失败: %s", path, e)

        self._write_md(path, cfg_items)
        ctx.state["_items"] = list(cfg_items)

    def on_enter(self, ctx):
        ctx.state["_last_key"] = None
        ctx.state["_manual_ts"] = ctx.now

    def on_action(self, action, payload, ctx):
        st = ctx.state
        items = list(st.get("_items") or [])
        n = len(items)
        if n == 0:
            return {"ok": False, "err": "空列表"}

        if action == "touch_single":
            st["_selected"] = (st.get("_selected", 0) + 1) % n
            st["_manual_ts"] = ctx.now
            st["_last_key"] = None
            return {"ok": True}

        if action == "touch_double":
            st["_selected"] = (st.get("_selected", 0) - 1) % n
            st["_manual_ts"] = ctx.now
            st["_last_key"] = None
            return {"ok": True}

        if action == "touch_long":
            idx = st.get("_selected", 0) % n
            s = items[idx].strip()
            if s.lower().startswith("[x]"):
                items[idx] = "[ ] " + s[3:].strip()
            elif s.startswith("[ ]"):
                items[idx] = "[x] " + s[3:].strip()
            else:
                items[idx] = "[x] " + s
            st["_items"] = items
            st["_manual_ts"] = ctx.now
            st["_last_key"] = None
            section = ctx.config.setdefault("widget_config", {}).setdefault(self.name, {})
            section["items"] = list(items)
            path = Path(st.get("_path") or "./todo.md").expanduser()
            self._write_md(path, items)
            return {"ok": True}

        return {"ok": False, "err": f"未知动作: {action}"}

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state
        path = Path(st.get("_path") or cfg["path"]).expanduser()

        cfg_items = list(cfg.get("items") or [])
        cur_items = list(st.get("_items") or [])

        if cfg_items and cfg_items != cur_items:
            st["_items"] = list(cfg_items)
            self._write_md(path, cfg_items)

        items = list(st.get("_items") or [])
        if not items:
            items = ["(空)"]
        total = len(items)

        W, H = ctx.size
        if ctx.orientation == LANDSCAPE:
            f = get_font(11, cjk=True)
            line_h = 12
            pad_y = 2
        else:
            f = get_font(12, cjk=True)
            line_h = 16
            pad_y = 4

        per_page = max(1, (H - pad_y * 2) // line_h)
        cycle = max(2.0, float(cfg["cycle_sec"]))

        manual_ts = st.get("_manual_ts", 0.0)
        if ctx.now - manual_ts >= MANUAL_HOLD_SEC:
            auto_idx = int(ctx.now / cycle) % total
            st["_selected"] = auto_idx

        selected = st.get("_selected", 0) % total
        st["_selected"] = selected

        if total <= per_page:
            start = 0
        else:
            start = selected - per_page // 2
            if start < 0:
                start = 0
            elif start + per_page > total:
                start = total - per_page

        key = f"{start}:{per_page}:{selected}:" + "|".join(items)
        if st.get("_last_key") == key:
            return None
        st["_last_key"] = key

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        shown_count = min(per_page, total)
        for i in range(shown_count):
            idx = start + i
            if idx >= total:
                break
            y = pad_y + i * line_h
            is_sel = (idx == selected)
            self._draw_item(d, 4, y, W - 8, line_h, items[idx], f, is_sel)

        return img

    def _draw_item(self, d, x, y, w, h, item, font, selected=False):
        if selected:
            d.rectangle([x - 2, y, x + w + 2, y + h],
                        fill="#1e3a5c")
            d.rectangle([x - 2, y, x - 1, y + h], fill="#4a7fe0")

        s = (item or "").strip()
        done = False
        if s.startswith("- ["):
            s = s[2:].strip()
        low = s.lower()
        if low.startswith("[x]"):
            done = True
            text = s[3:].strip()
        elif s.startswith("[ ]"):
            text = s[3:].strip()
        else:
            text = s

        box = h - 4
        bx = x
        by = y + 2
        color = "#22cc66" if done else ("#ffffff" if selected else "#909090")
        d.rectangle([bx, by, bx + box, by + box], outline=color)
        if done:
            d.line([bx + 2, by + box // 2,
                    bx + box // 2, by + box - 3], fill=color, width=2)
            d.line([bx + box // 2, by + box - 3,
                    bx + box - 2, by + 2], fill=color, width=2)

        text_x = bx + box + 5
        text_w = w - box - 5
        t = text
        try:
            while t and font.getlength(t) > text_w:
                t = t[:-1]
            if t != text and len(t) > 0:
                t = t[:-1] + "…"
        except Exception:
            pass

        if selected:
            fill = "#ffffff"
        elif done:
            fill = "#808080"
        else:
            fill = "#e0e0e0"

        d.text((text_x, y + h // 2), t, fill=fill, font=font, anchor="lm")
