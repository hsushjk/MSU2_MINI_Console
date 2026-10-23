import json
import locale
import logging
import os
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, ellipsis, text_w

log = logging.getLogger("msu2.widgets.todo")

_DEFAULT_ITEMS = ["[ ] 请在 WebUI 开荒"]

_ALLOWED_SUFFIXES = (".md", ".txt")
_MAX_FILE_BYTES = 1024 * 1024
_ENCODINGS = ("utf-8-sig", "utf-8", locale.getpreferredencoding(False), "gbk")


def _clamp_float(value, default, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return max(lo, min(hi, v))


def _check_path(raw):
    try:
        text = str(raw or "").strip()
    except Exception:
        return None, "路径无效"
    if not text:
        return None, "路径为空"
    try:
        path = Path(text).expanduser()
    except Exception:
        return None, "路径无效"
    if not path.name:
        return None, "路径为空"
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        return None, "仅支持 .md/.txt"
    if not path.is_absolute():
        path = _app_dir() / path
    return path, ""


def _norm_item(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("- [") or s.startswith("* ["):
        s = s[2:].strip()
    low = s.lower()
    if low.startswith("[x]"):
        return "[x] " + s[3:].strip()
    if low.startswith("[ ]"):
        return "[ ] " + s[3:].strip()
    return "[ ] " + s


def _norm_items(values):
    out = []
    try:
        seq = list(values or [])
    except Exception:
        return out
    for value in seq:
        item = _norm_item(value)
        if item:
            out.append(item)
    return out


def _item_done(item):
    low = str(item or "").strip().lower()
    if low.startswith("- [") or low.startswith("* ["):
        low = low[2:].strip()
    return low.startswith("[x]")


def _item_text(item):
    s = str(item or "").strip()
    if s.startswith("- [") or s.startswith("* ["):
        s = s[2:].strip()
    low = s.lower()
    if low.startswith("[x]") or low.startswith("[ ]"):
        return s[3:].strip()
    return s


def _toggle_item(item):
    if _item_done(item):
        return "[ ] " + _item_text(item)
    return "[x] " + _item_text(item)


def _render_md(items, title="# Todo"):
    head = str(title or "").strip() or "# Todo"
    text = head + "\n\n"
    for item in items:
        norm = _norm_item(item)
        if not norm:
            continue
        text += "- " + norm + "\n"
    return text


def _title_of(content):
    for line in str(content or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s
    return "# Todo"


def _decode(raw):
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), "", False
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        return raw.decode("utf-8", "replace"), "", True
    except Exception:
        return "", "解码失败", True


def _app_dir():
    try:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent
    except Exception:
        return None


def _config_paths():
    out = []
    try:
        argv = list(sys.argv or [])
    except Exception:
        argv = []
    i = 0
    while i < len(argv):
        arg = str(argv[i])
        if arg == "--config" and i + 1 < len(argv):
            out.append(Path(str(argv[i + 1])))
            i += 1
        elif arg.startswith("--config="):
            out.append(Path(arg[9:]))
        i += 1
    base = _app_dir()
    if base is not None:
        out.append(base / "config.json")
    try:
        out.append(Path.cwd() / "config.json")
    except Exception:
        pass
    result = []
    for path in out:
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


@register
class TodoWidget(Widget):
    name = "todo"
    interval = 1.0
    orientations = BOTH
    priority = 40

    actions = {
        "next": "下一个",
        "prev": "上一个",
        "toggle": "勾选/取消",
    }
    touch_help = "单击=下一个，双击=上一个，长按=勾选/取消"

    config_schema = [
        Field("path", "str", "todo.md 路径", default="./todo.md"),
        Field(
            "items",
            "list_str",
            "待办列表",
            default=list(_DEFAULT_ITEMS),
            help="每行一条；[ ] 未完成，[x] 已完成",
        ),
        Field("cycle", "bool", "循环浏览", default=False),
        Field(
            "cycle_interval",
            "float",
            "切换周期(秒)",
            default=10.0,
            min=2.0,
            max=3600.0,
            step=1.0,
        ),
    ]

    def setup(self, ctx):
        cfg = self.get_config(ctx)
        section = ctx.config.setdefault("widget_config", {}).setdefault(self.name, {})
        st = ctx.state
        st["_path"] = ""
        st["_path_error"] = ""
        st["_last_key"] = None
        st["_selected"] = 0
        st["_mtime"] = 0.0
        st["_readonly"] = False
        st["_title"] = "# Todo"
        st["_cycle_next"] = time.monotonic() + _clamp_float(
            cfg.get("cycle_interval"), 10.0, 2.0, 3600.0
        )

        raw_items = section.get("items")
        cfg_items = _norm_items(raw_items) if raw_items else []
        explicit = bool(cfg_items) and cfg_items != _norm_items(_DEFAULT_ITEMS)
        path, err = _check_path(cfg.get("path"))

        if path is None:
            st["_path_error"] = err
            st["_items"] = cfg_items or list(_DEFAULT_ITEMS)
            return

        st["_path"] = str(path)
        text, read_err, lossy = self._read_text(path)
        if read_err:
            st["_path_error"] = read_err
            st["_items"] = cfg_items or list(_DEFAULT_ITEMS)
            return

        if text is None:
            target = cfg_items or list(_DEFAULT_ITEMS)
            if not self._write_md(path, target, st["_title"], st):
                st["_path_error"] = "写入失败"
            st["_items"] = list(target)
            return

        st["_mtime"] = self._mtime(path)
        st["_title"] = _title_of(text)
        st["_readonly"] = lossy
        file_items = self._parse_md(text)

        if not file_items:
            target = cfg_items or list(_DEFAULT_ITEMS)
            if not self._write_md(path, target, st["_title"], st):
                st["_path_error"] = "写入失败"
            st["_items"] = list(target)
            return

        if not explicit or cfg_items == file_items:
            items = file_items
        elif st["_readonly"]:
            items = file_items
        else:
            cfg_ts = self._config_mtime(cfg_items)
            if cfg_ts is not None and st["_mtime"] > cfg_ts + 0.000001:
                items = file_items
            elif self._write_md(path, cfg_items, st["_title"], st):
                items = cfg_items
            else:
                st["_path_error"] = "写入失败"
                items = file_items

        if section.get("items") != items and not st["_readonly"]:
            section["items"] = list(items)
        st["_items"] = list(items)

    def teardown(self, ctx):
        pass

    def on_enter(self, ctx):
        st = ctx.state
        st["_last_key"] = None
        st["_cycle_next"] = time.monotonic() + _clamp_float(
            self.get_config(ctx).get("cycle_interval"), 10.0, 2.0, 3600.0
        )

    def on_leave(self, ctx):
        pass

    def on_action(self, action, payload, ctx):
        try:
            st = ctx.state
            items = list(st.get("_items") or [])
            total = len(items)
            if total == 0:
                return {"ok": False, "err": "列表为空"}
            if action in ("next", "touch_single"):
                st["_selected"] = (int(st.get("_selected", 0)) + 1) % total
                self._postpone(ctx)
                return {"ok": True}
            if action in ("prev", "touch_double"):
                st["_selected"] = (int(st.get("_selected", 0)) - 1) % total
                self._postpone(ctx)
                return {"ok": True}
            if action in ("toggle", "touch_long"):
                index = int(st.get("_selected", 0)) % total
                items[index] = _toggle_item(items[index])
                st["_items"] = items
                section = ctx.config.setdefault("widget_config", {}).setdefault(
                    self.name, {}
                )
                section["items"] = list(items)
                self._persist(ctx, items)
                self._postpone(ctx)
                return {"ok": True}
            return {"ok": False, "err": "未知动作: " + str(action)}
        except Exception as exc:
            log.warning("todo 动作 %s 失败: %s", action, exc)
            return {"ok": False, "err": "动作失败"}

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception as exc:
            log.warning("todo 渲染失败: %s", exc)
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state
        path = st.get("_path") or ""
        if path and not st.get("_readonly"):
            path_obj = Path(path)
            mtime = self._mtime(path_obj)
            old = float(st.get("_mtime") or 0.0)
            if mtime and old and mtime > old + 0.000001:
                self._reload(ctx, path_obj, mtime)

        items = list(st.get("_items") or [])
        total = len(items)
        W, H = ctx.size
        err = st.get("_path_error") or ""

        if ctx.orientation == LANDSCAPE:
            font = get_font(11, cjk=True)
            line_h = 12
            pad_y = 2
        else:
            font = get_font(12, cjk=True)
            line_h = 16
            pad_y = 4
        per_page = max(1, (H - pad_y * 2) // line_h)

        if err:
            key = "err|%s|%s|%s" % (W, H, err)
            if st.get("_last_key") == key:
                return None
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, W - 1, H - 1], outline="#663333")
            d.text((W // 2, H // 2 - 9), "todo 路径错误", font=font, fill="#ff5050")
            d.text(
                (W // 2, H // 2 + 9),
                ellipsis(font, err, max(1, W - 8)),
                font=font,
                fill="#cccccc",
            )
            st["_last_key"] = key
            return img

        if total == 0:
            key = "empty|%s|%s" % (W, H)
            if st.get("_last_key") == key:
                return None
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), "(空)", font=font, fill="#909090", anchor="mm")
            st["_last_key"] = key
            return img

        interval = _clamp_float(cfg.get("cycle_interval"), 10.0, 2.0, 3600.0)
        now_mono = time.monotonic()
        if bool(cfg.get("cycle")) and total > 1:
            nxt = float(st.get("_cycle_next") or 0.0)
            if nxt <= 0.0:
                nxt = now_mono + interval
                st["_cycle_next"] = nxt
            if now_mono >= nxt:
                steps = int((now_mono - nxt) // interval) + 1
                st["_selected"] = (
                    int(st.get("_selected", 0)) + steps
                ) % total
                st["_cycle_next"] = nxt + steps * interval
                st["_last_key"] = None
        else:
            st["_cycle_next"] = now_mono + interval

        selected = int(st.get("_selected", 0)) % total
        st["_selected"] = selected
        if total <= per_page:
            start = 0
        else:
            start = selected - per_page // 2
            if start < 0:
                start = 0
            elif start + per_page > total:
                start = total - per_page

        box = line_h - 4
        if box < 6:
            box = 6
        text_x = 4 + box + 5
        max_text_w = W - 8 - box - 5
        if max_text_w < 1:
            max_text_w = 1

        scroll = False
        for i in range(min(per_page, total)):
            index = start + i
            if index >= total:
                break
            if text_w(font, _item_text(items[index])) > max_text_w:
                scroll = True
                break

        key = repr((W, H, start, per_page, selected, tuple(items)))
        if scroll:
            key += "|" + str(int(ctx.now * 2))
        if st.get("_last_key") == key:
            return None

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        shown = min(per_page, total)
        for i in range(shown):
            index = start + i
            y = pad_y + i * line_h
            self._draw_item(img, d, 4, y, W - 8, line_h, items, index, font, ctx)
        st["_last_key"] = key
        return img

    def _draw_item(self, img, d, x, y, w, h, items, index, font, ctx):
        selected = index == int(ctx.state.get("_selected", 0))
        item = items[index]
        done = _item_done(item)
        text = _item_text(item)
        box = h - 4
        if box < 6:
            box = 6
        if w < box + 2:
            box = max(2, w - 2)
        if selected:
            d.rectangle([x - 2, y + 1, x + w, y + h - 2], fill="#1e3a5c")
            d.rectangle([x - 2, y + 1, x - 1, y + h - 2], fill="#4a7fe0")
        if done:
            color = "#22cc66"
        elif selected:
            color = "#ffffff"
        else:
            color = "#909090"
        d.rectangle([x, y + 2, x + box, y + 2 + box], outline=color)
        if done:
            d.line(
                [
                    (x + box * 0.2, y + 2 + box * 0.55),
                    (x + box * 0.45, y + 2 + box * 0.8),
                    (x + box * 0.8, y + 2 + box * 0.25),
                ],
                fill=color,
                width=1,
            )
        if selected:
            fill = "#ffffff"
        elif done:
            fill = "#22cc66"
        else:
            fill = "#cccccc"
        if text:
            text_x = x + box + 5
            max_w = w - box - 5
            if max_w < 1:
                max_w = 1
            row_text(
                img,
                (text_x, y + h // 2),
                text,
                font,
                fill,
                max_w,
                ctx.now,
                scroll=True,
            )

    def _postpone(self, ctx):
        st = ctx.state
        st["_last_key"] = None
        st["_cycle_next"] = time.monotonic() + _clamp_float(
            self.get_config(ctx).get("cycle_interval"), 10.0, 2.0, 3600.0
        )

    def _persist(self, ctx, items):
        st = ctx.state
        path = st.get("_path") or ""
        if not path or st.get("_path_error") or st.get("_readonly"):
            return
        if not self._write_md(
            Path(path).expanduser(), items, st.get("_title") or "# Todo", st
        ):
            st["_path_error"] = "写入失败"

    def _reload(self, ctx, path, mtime):
        st = ctx.state
        text, err, lossy = self._read_text(path)
        if err or text is None or lossy:
            st["_mtime"] = mtime
            st["_readonly"] = lossy
            return
        items = self._parse_md(text)
        st["_mtime"] = mtime
        st["_title"] = _title_of(text)
        st["_items"] = list(items)
        st["_last_key"] = None
        section = ctx.config.setdefault("widget_config", {}).setdefault(self.name, {})
        if section.get("items") != items:
            section["items"] = list(items)

    def _config_mtime(self, items):
        want = list(items)
        for path in _config_paths():
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                raw = json.loads(path.read_text("utf-8"))
                if not isinstance(raw, dict):
                    continue
                wc = raw.get("widget_config")
                section = wc.get(self.name) if isinstance(wc, dict) else None
                current = _norm_items(section.get("items")) if isinstance(section, dict) else []
                if current != want:
                    continue
                return float(path.stat().st_mtime)
            except Exception:
                continue
        return None

    def _parse_md(self, content):
        out = []
        try:
            lines = str(content or "").splitlines()
        except Exception:
            return out
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            item = _norm_item(s)
            if item:
                out.append(item)
        return out

    def _read_text(self, path):
        try:
            if not path.exists():
                return None, "", False
            if not path.is_file():
                return None, "不是文件", False
            if path.stat().st_size > _MAX_FILE_BYTES:
                return None, "文件超过 1MB", False
            raw = path.read_bytes()
        except Exception as exc:
            log.warning("读取 %s 失败: %s", path, exc)
            return None, "读取失败", False
        return _decode(raw)

    def _write_md(self, path, items, title, st=None):
        tmp = None
        try:
            data = _render_md(items, title).encode("utf-8")
            if path.is_file():
                try:
                    if path.stat().st_size <= _MAX_FILE_BYTES and path.read_bytes() == data:
                        if st is not None:
                            st["_mtime"] = self._mtime(path)
                        return True
                except Exception:
                    pass
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(str(tmp), str(path))
            if st is not None:
                st["_mtime"] = self._mtime(path)
            log.info("todo 文件已写入 %s（%d 条）", path, len(items))
            return True
        except Exception as exc:
            log.warning("写入 %s 失败: %s", path, exc)
            if tmp is not None:
                try:
                    tmp.unlink()
                except Exception:
                    pass
            return False

    @staticmethod
    def _mtime(path):
        try:
            return float(path.stat().st_mtime)
        except Exception:
            return 0.0