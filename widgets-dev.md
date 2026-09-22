# 插件开发和导入手册

**您可能觉得我的项目写的可差，确实差，我基本上不怎么会python，敬请谅解**

## 1. 插件目录结构

```
项目根/
├── msu2_mini.py          主程序（不需要动）
├── msu2_core/            插件基础设施（一般不需要动）
│   ├── widget.py         Widget 基类
│   ├── registry.py       注册表
│   └── config_schema.py  配置字段类型
└── widgets/              你写的插件都放这里
├── __init__.py       自动发现（不需要动）
├── demos.py
└── your_plugin.py    ← 你新建的文件
```

**核心原则**：加插件只需要在 `widgets/` 里**新建一个 .py 文件**。主程序、`msu2_core`、其它插件，一律不用改。

## 2. 最小可用插件

新建 `widgets/hello.py`：

```python
# -*- coding: utf-8 -*-
"""一个最小示例：在屏幕中央显示 Hello。"""
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, get_font


@register
class HelloWidget(Widget):
    """第一行 docstring 会成为插件描述。"""
    name = "hello"              # 唯一 ID，不能和其它插件重名
    interval = 1.0              # 刷新间隔（秒）
    orientations = BOTH         # BOTH / (LANDSCAPE,) / (PORTRAIT,)
    priority = 100              # 排序权重，越小越靠前，可以重复

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), "Hello",
               fill="white", font=get_font(24), anchor="mm")
        return img
```

保存，重启程序。日志里会看到：

```
[INFO] msu2.widgets: 插件模块加载完成: 成功 N 个 (... hello)
[INFO] msu2: 已注册插件: [..., 'hello']
```

Web 页面上会出现 `hello [landscape/portrait]`，启用后小屏就显示了。

## 3. Widget 基类完整 API

```python
class Widget:
    # ---- 类属性 ----
    name = "base"              # str，唯一 ID
    interval = 1.0             # float，秒
    orientations = BOTH        # tuple，(LANDSCAPE, PORTRAIT) 的子集
    priority = 50              # int，越小越靠前（首次生成 config 时用）
    config_schema = []         # list[Field]，声明式配置
    actions = {}               # dict[str, str]，Web 按钮

    # ---- 生命周期 ----
    def setup(self, ctx):
        """插件加载时调用一次。适合读文件、开线程、初始化状态。"""

    def teardown(self, ctx):
        """插件卸载/程序退出时调用。适合关闭线程、释放资源。"""

    def on_enter(self, ctx):
        """本插件成为当前显示页时调用。适合清缓存（切页后强制重绘）。"""

    def on_leave(self, ctx):
        """本插件离开当前显示页时调用。"""

    def render(self, ctx) -> Image.Image:
        """
        核心方法，主循环每隔 interval 秒调一次。
        返回 PIL.Image（推荐 RGBA）表示要画的帧。
        返回 None 表示"本轮不需要重绘"。
        """

    def get_config(self, ctx) -> dict:
        """读自己的配置（已用 config_schema 的默认值补齐）。"""

    def on_action(self, action, payload, ctx) -> dict:
        """
        Web 按钮点击 / 硬件触控时调用。
        返回 {'ok': bool, 'err': str(可选)}。
        """
```

## 4. RenderContext 详解

每次 `render()` 收到的 `ctx` 里有：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ctx.size` | `(W, H)` | 当前朝向下的画布尺寸。横屏 (160, 80)，竖屏 (80, 160) |
| `ctx.orientation` | `str` | `"landscape"` 或 `"portrait"` |
| `ctx.now` | `float` | 当前时间戳（`time.time()`） |
| `ctx.state` | `dict` | **每个插件独立**的持久字典，可跨帧、跨页存数据 |
| `ctx.config` | `dict` | 全局 config（只读） |
| `ctx.device` | `MiniDevice` | 底层设备对象 |

**`ctx.state` 是插件专属的**，不用怕键名冲突。但要注意：`restart_widget`（Web 改配置后触发）会重置 state，如果你希望状态持久化，得写回 `ctx.config["widget_config"][name]`。

## 5. 声明式配置：`config_schema`

只要在 Widget 类里声明 `config_schema`，Web 上会自动出现 ⚙ 按钮，点开就是一个表单。

```python
from msu2_core import Field

class MyWidget(Widget):
    config_schema = [
        Field("name", "str", "名字", default="世界"),
        Field("count", "int", "次数", default=10, min=1, max=100),
        Field("gain", "float", "增益", default=2.0, min=0.5, max=10.0, step=0.5),
        Field("enable", "bool", "启用", default=True),
        Field("mode", "select", "模式", default="A", options=["A", "B", "C"]),
        Field("items", "list_str", "列表", default=["a", "b", "c"],
              help="每行一个"),
    ]
```

`Field` 参数：

| 参数 | 说明 |
|---|---|
| `key` | 字段名（必填，第一个位置参数） |
| `type` | `str` / `int` / `float` / `bool` / `select` / `list_str` |
| `label` | Web 上显示的标签 |
| `default` | 默认值 |
| `help` | 帮助文字（显示在标签后面） |
| `options` | 仅 `select` 用，字符串列表 |
| `min` / `max` / `step` | 仅 `int` / `float` 用 |

读配置：

```python
def render(self, ctx):
    cfg = self.get_config(ctx)
    name = cfg["name"]        # 用户填的值，未填则是 default
    count = cfg["count"]
```

用户改完保存后，主程序会**自动 teardown + setup 你的插件**（`restart_widget`），所以 `setup` 里的初始化逻辑要幂等。

## 6. 声明式按钮：`actions` + `on_action`

想让 Web 上出现按钮：

```python
class PomodoroWidget(Widget):
    actions = {
        "start": "开始",
        "stop":  "停止",
    }

    def on_action(self, action, payload, ctx):
        if action == "start":
            ctx.state["running"] = True
            return {"ok": True}
        if action == "stop":
            ctx.state["running"] = False
            return {"ok": True}
        return {"ok": False, "err": f"未知动作 {action}"}
```

保存后 Web 页面上会看到"开始"、"停止"两个按钮。

**注意**：`on_action` 执行完，主程序会自动设一次 `_switch`，让 Scheduler 下一轮**立即重绘**（不需要等 interval）。所以按钮按下后屏幕几乎立刻变化。

`payload` 参数留给未来扩展（比如带参数的动作），目前是 `{}`。

## 7. 硬件触控支持

MSU2_MINI 面板上有一个触摸按键，可以识别三种手势——**单击 / 双击 / 长按**。

### 7.1 两种运行模式

主程序有两种模式处理触控：

- **透传模式**：当「全局触控」的三个手势都选 `未定义（透传给插件）` 时，触控事件被转发给**当前显示的插件**，通过 `on_action` 送到你手里。
- **全局模式**：任意一个手势选了具体动作，则所有触控走主程序全局定义，插件不再收到事件。

**为什么这样设计**：屏幕只有一个键，全局定义是"系统级操作"（翻页 / 翻转 / 关机等），透传是"内容级操作"（切歌 / 勾选待办等）。用户根据使用场景自行选择。

### 7.2 收到的动作名

透传模式下，`on_action` 会收到三个专属动作名：

| action 字符串 | 触发时机 |
|---|---|
| `touch_single` | 单击（按下-松开，间隔 < 800ms，且 350ms 内无第二次按下） |
| `touch_double` | 双击（350ms 内连按两次） |
| `touch_long` | 长按（按住 ≥ 800ms） |

**注意**：单击有 350ms 的延迟——因为主程序要等双击窗口关闭才能确定不是双击。这是判定逻辑的必然结果。

### 7.3 最小示例

```python
# -*- coding: utf-8 -*-
"""触控示例：单击数字 +1，双击 -1，长按归零。"""
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, get_font


@register
class TouchDemoWidget(Widget):
    """演示触控响应。"""
    name = "touch_demo"
    interval = 1.0
    orientations = BOTH
    priority = 200

    def setup(self, ctx):
        ctx.state["count"] = 0

    def on_action(self, action, payload, ctx):
        if action == "touch_single":
            ctx.state["count"] = ctx.state.get("count", 0) + 1
            return {"ok": True}
        if action == "touch_double":
            ctx.state["count"] = ctx.state.get("count", 0) - 1
            return {"ok": True}
        if action == "touch_long":
            ctx.state["count"] = 0
            return {"ok": True}
        return {"ok": False, "err": f"未知动作: {action}"}

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), str(ctx.state.get("count", 0)),
               fill="white", font=get_font(40, bold=True, cjk=False),
               anchor="mm")
        return img
```

### 7.4 返回值语义

- `return {"ok": True}` → 主程序**立即重绘**，用户马上看到效果
- `return {"ok": False, "err": "..."}` → 日志里会记录，但屏幕不重绘

如果你的动作会改 `ctx.state`，**一定返回 `ok=True`**，否则要等 `interval` 秒才重绘。

### 7.5 什么时候不要实现

纯展示类插件（如 `clock` / `pcstats`）没必要响应触控——用户按了没反馈就是最好的反馈。**实现了反而会让用户困惑**："为什么按了没反应？"（其实有反应，就是没视觉效果）。

建议只在**有明显"下一项"语义**的插件里实现：

| 插件类型 | 推荐实现 |
|---|---|
| 列表 / 轮播类（todo / countdown） | ✔ 单击 / 双击翻项，长按操作当前项 |
| 媒体控制类（music） | ✔ 单击 / 双击切歌，长按播放暂停 |
| 可刷新类（crypto / wifi_scanner） | ✔ 单击手动刷新 |
| 开关类（clipboard 固定） | ✔ 长按切换状态 |
| 纯展示类（clock / pcstats / calendar） | ✘ 没必要 |

### 7.6 参考实现

- `widgets/music.py` → `_send_media_key()`
- `widgets/todo.py` → `_selected` 光标 + 窗口跟随
- `widgets/wifi_scanner.py` → 清缓存触发重扫
- `widgets/clipboard.py` → `pinned` 状态切换

## 8. 渲染注意事项

### 8.1 返回 None 的语义

如果你的插件内容**大部分时间不变**（比如时钟只在秒数变时才需要重绘），可以缓存上次的画布 key，不变就返回 `None`：

```python
def render(self, ctx):
    key = f"{int(ctx.now)}"     # 每秒变一次
    if ctx.state.get("_last_key") == key:
        return None              # 告诉 Scheduler 本轮跳过
    ctx.state["_last_key"] = key
    # ... 绘制 ...
    return img
```

**我觉得这个机制非常重要**。19200 波特率下一帧要 13 秒，如果每秒都重绘，屏幕永远在刷，还占满了串口导致其它操作（切页、翻转）没法响应

### 8.2 切页后要清缓存

用 `_last_key` 缓存的插件，切页回来时必须清缓存，否则屏幕还是上一页残留：

```python
def on_enter(self, ctx):
    ctx.state.pop("_last_key", None)
```

### 8.3 朝向

`ctx.size` 会随朝向变化。不要硬编码 160×80。如果你的插件只支持横屏，声明：

```python
orientations = (LANDSCAPE,)
```

Scheduler 切页时会自动跳过不支持当前朝向的插件。

### 8.4 颜色与透明

返回的图片建议用 `"RGBA"` 模式，未绘制的像素保持透明，会透出背景图。如果不想透出背景，可以先铺一层纯色：

```python
img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
```

### 8.5 字体

```python
from msu2_core import get_font

f = get_font(20)                    # 中英文都支持，自动找系统字体
f = get_font(20, bold=True)         # 粗体
f = get_font(20, cjk=False)         # 只含 ASCII 的字体，等宽，适合数字
```

**字体有缓存**，可以在 `render` 里反复调用 `get_font`，应该不会有性能问题。

## 9. 完整示例：显示当前分钟

```python
# -*- coding: utf-8 -*-
"""显示当前分钟数的示例插件。"""
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, Field, get_font


@register
class MinuteWidget(Widget):
    """每分钟变化一次的数字。"""
    name = "minute"
    interval = 1.0
    orientations = BOTH
    priority = 200

    config_schema = [
        Field("label", "str", "标签", default="MIN"),
        Field("color", "select", "颜色", default="青色",
              options=["青色", "绿色", "红色", "黄色"]),
    ]

    def on_enter(self, ctx):
        ctx.state.pop("_last_key", None)

    def on_action(self, action, payload, ctx):
        if action == "touch_single":
            # 单击立即刷一次（其实这个例子没必要）
            ctx.state.pop("_last_key", None)
            return {"ok": True}
        return {"ok": False, "err": f"未知动作: {action}"}

    def render(self, ctx):
        cfg = self.get_config(ctx)
        W, H = ctx.size

        minute = time.localtime().tm_min
        key = f"{minute}|{W}x{H}|{cfg['color']}"
        if ctx.state.get("_last_key") == key:
            return None
        ctx.state["_last_key"] = key

        color_map = {
            "青色": "#00e0ff",
            "绿色": "#22cc66",
            "红色": "#ff4444",
            "黄色": "#ffcc00",
        }
        color = color_map.get(cfg["color"], "#ffffff")

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        f_lbl = get_font(10, cjk=True)
        f_num = get_font(40, bold=True, cjk=False)

        d.text((6, 4), cfg["label"], fill="#808080", font=f_lbl)
        d.text((W // 2, H // 2 + 4), f"{minute:02d}",
               fill=color, font=f_num, anchor="mm")
        return img
```

## 10. 调试

**打开 DEBUG 日志**：

```bash
python msu2_mini.py --log DEBUG
```

会看到：

- 每个插件注册成功/失败
- 每帧渲染耗时
- 心跳发送
- 切页动作
- 触摸手势识别（`触摸: 单击` / `触摸: 双击` / `触摸: 长按`）
- 触摸 ADC 原始值（`touch adc=NNNN base=NNNN`）

**日志里 `[ERROR]` 或 `[WARNING]`**：

- `插件 XXX setup 失败: ...` → setup 里抛异常了，插件仍会加载但可能渲染不出来
- `插件 XXX 渲染异常: ...` → render 里抛异常了，主循环会打日志并继续下一个
- `插件 XXX touch_single 失败: ...` → `on_action` 里抛异常了
- `WMI 数据源: ...` → sensors 插件找到了数据源

**在插件里输出日志**：

```python
import logging
log = logging.getLogger("msu2.widgets.myplugin")
log.info("我的日志")
```

会跟主程序日志混在一起，方便排查。

**手动重载插件**：Web 页面上点 `重载插件` 按钮，会重新加载所有 `widgets/` 目录（不需要重启程序）。

**测试触控阈值**：

启动日志：

```
[INFO] msu2: 触摸基准 = 1234
```

按一下触摸键，日志应该出现：

```
[INFO] msu2: 触摸: 单击
```

如果按下时**没有**输出，说明 `threshold` 太大，把 `config.json` 里 `touch.threshold` 改小。

如果没按也一直输出，说明 `threshold` 太小，改大。

具体数值观察方法：加 `--log DEBUG`，日志里会打每次采样的 ADC 值（`touch adc=NNNN base=NNNN`），看按下和松开时的差值。

祝你好运