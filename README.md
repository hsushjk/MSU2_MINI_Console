# MSU2_MINI_Console

> **MSU2_MINI 可编程 USB 屏幕的跨平台上位机**

一个插件化的上位机控制台，使用 WebUI 调整小屏显示的内容、顺序和朝向，支持 Windows / Linux

> **关于代码质量**：本项目作者并非专业 Python 开发者，代码可能不够优雅，欢迎接手。

<p>
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey" alt="platform">
  <img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-orange" alt="license">
</p>

---

## 目录

- [内置插件](#内置插件)
- [安装](#安装)
- [快速开始](#快速开始)
- [Web 控制台](#web-控制台)
- [硬件触控按键](#硬件触控按键)
- [命令行参数](#命令行参数)
- [打包成二进制文件](#打包成二进制文件)
- [插件开发](#插件开发)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [致谢](#致谢)

---

## 内置插件

18 个 demo

| 插件 ID | 名称 | 功能 | 朝向 | 触控支持 |
|---|---|---|---|---|
| `clock` | 时钟 | 时间 + 年月日 + 星期 | 横 / 竖 | — |
| `calendar` | 月历 | 月历 / 周历，今日高亮 | 横 / 竖 | — |
| `countdown` | 倒计时 | 多事件倒计时，按紧迫度着色 | 横 / 竖 | — |
| `anniversary` | 纪念日 | 如题 | 横 / 竖 | — |
| `holiday` | 节假日 | 距下个节假日的天数 | 横 / 竖 | — |
| `pcstats` | 系统状态 | CPU / RAM / 磁盘 / 网络 / 开机时长 | 横 / 竖 | — |
| `disk_io` | 磁盘读写 | 读写速率 + 折线图 | 横 / 竖 | — |
| `network_detail` | 网络详情 | 网卡速率 + Wi-Fi 信号 | 横 / 竖 | — |
| `process_top` | 进程排行 | Top 5 进程，可按 CPU / 内存排序 | 横 / 竖 | — |
| `app_usage` | 前台应用 | 当前应用 + 使用时长 | 横 / 竖 | — |
| `keyboard_stats` | 键盘统计 | 今日敲击次数 + KPM | 横 / 竖 | — |
| `mouse_mileage` | 鼠标里程 | 今日移动距离 | 横 / 竖 | — |
| `music` | 音乐 | 音乐信息 + 封面旋转动画 | 横 / 竖 | ✔ |
| `crypto` | 加密货币 | 实时价 + 涨跌幅 | 横 / 竖 | — |
| `clipboard` | 剪贴板 | 最近一条内容 | 横 / 竖 | ✔ |
| `todo` | 待办 | 读 `todo.md`，Web 端可编辑 | 横 / 竖 | ✔ |
| `wifi_scanner` | Wi-Fi 扫描 | 附近 SSID + 信号强度 | 横 / 竖 | ✔ |
| `gif` | 背景透传 | 静态 / 动图背景 | 横 / 竖 | — |

「触控支持」一栏表示该插件实现了 `on_action("touch_*")`，在透传模式下响应小屏按键。

---

## 安装

### 环境要求

- Python 3.8 或更高
- 一块 MSU2_MINI（或兼容的同款硬件）

### 步骤

```bash
# 1. 拷贝整个项目到本地
cd MSU2_MINI_Console

# 2. 装必装依赖
pip install pyserial pillow psutil pynput
```

### 可选依赖

用到对应插件时才需要装：

```bash
# Linux 上的剪贴板插件
sudo apt install xclip              # X11
sudo apt install wl-clipboard       # Wayland

# Linux 上的音乐控制
sudo apt install playerctl

# Linux 上的音乐信息读取
pip install dbus-python
```

### Linux 额外说明

如果扫描设备出现 PermissionError ，说明当前用户权限不足，无法访问串口。解决方案如下：

检查用户是否在 `dialout` 组：

```bash
groups $USER | grep dialout
# 如果没有：
sudo usermod -a -G dialout $USER
# 注销重新登录后生效
```

除非你用的是无头服务器，否则非常不建议直接用root账户

---

## 快速开始

```bash
python msu2_mini.py
# linux需要创建一个venv并进入
```

程序会自动：

1. 扫描所有串口，找到 MSU2_MINI
2. 在项目目录生成 `config.json`
3. 加载所有插件，并生成插件独有的文件(如有)
4. 启动 WebUI，监听所有网卡的8765端口，即 `http://0.0.0.0:8765`

本机打开浏览器访问 `http://127.0.0.1:8765` 就能看到控制台。

**如果没找到设备**：

- 检查串口是否被其它串口程序占用
- 拔插一次 USB 后重跑
- 用 `python msu2_mini.py --list` 看有没有识别到串口

---

## Web 控制台

浏览器打开 WebUI 后分为六块：

### 显示控制

| 按钮 | 作用 |
|---|---|
| `上一页` / `下一页` | 切换小屏当前显示的插件 |
| `切换朝向` | 横屏 ↔ 竖屏 |
| `上下翻转` | 屏幕图像 180° 翻转 |

> 每个操作有 0.8 秒冷却，防止连点导致花屏。

### 全局触控

小屏上的触摸按键可以绑三种手势——**单击 / 双击 / 长按**——各自对应一个动作。

**两种运行模式**：

1. **透传模式**：三个手势都选 `未定义（透传给插件）`。手势事件交给当前插件处理。此时：
   
   | 示例插件 | 单击 | 双击 | 长按 |
|---|---|---|---|
| music | 下一首 | 上一首 | 播放 / 暂停 |
| todo | 选中下一条 | 选中上一条 | 勾选 / 取消 |
| wifi_scanner | 立即刷新 | — | — |
| clipboard | — | — | 固定 / 取消固定 |
   
   > 需要 `playerctl`（Linux）/ pynput（宏）等外部工具支持的，见[安装](#安装)。
2. **全局模式**：任意一个手势选了具体动作，则所有手势走全局定义，插件不再收到触控。

**可选动作**：

| 动作 | 说明 |
|---|---|
| `未定义（透传给插件）` | 交给当前插件（默认值） |
| `下一页` / `上一页` | 切页 |
| `上下翻转` | 屏幕 180° 翻转 |
| `横竖屏切换` | 横屏 ↔ 竖屏 |
| `锁定/解锁当前页` | 锁定时忽略触摸翻页，Web 按钮不受影响 |
| `截屏` | 保存到 `./screenshots/shot_YYYYMMDD_HHMMSS.png` |
| `静音 / 解除` | 系统静音切换 |
| `自定义键盘宏` | 发送组合键，如 `ctrl+shift+s`、`alt+f4`、`win+r` |
| `自定义程序` | 启动一个程序或命令，例：`notepad`、`C:\Windows\notepad.exe`、`firefox`、`/usr/bin/code` |
| `关闭程序` | 退出上位机 |

**宏语法**：用 `+` 连接修饰键和主键，大小写不敏感。修饰键 `ctrl` / `shift` / `alt` / `win` / `cmd`，主键支持单字符、`enter` / `esc` / `tab` / `space` / `backspace` / `delete` / `up` / `down` / `left` / `right` / `home` / `end` / `pageup` / `pagedown` / `f1`~`f12`。

### 页面顺序

- 每一行是一个插件。`↑↓` 调整顺序，`×` 停用，`+` 启用
- `⚙` 打开该插件的配置表单
- 蓝色高亮 + ▶ 标记的是当前屏幕正在显示的插件
- 插件右侧的按钮是插件自定义的动作（目前没有插件实现）

### 配置面板

点任意插件的 `⚙` 会展开配置表单，修改后点"保存"立即生效，同时写入 `config.json`。

### 背景图

- 填图片路径（支持 PNG / JPG / GIF），点"启用背景"
- 背景图会铺满整屏，插件绘制的透明区域会透出背景
- 应该支持动态 GIF

### 状态

实时 JSON 显示：连接状态、串口号、当前插件、朝向、翻转、锁定、背景开关。

---

## 硬件触控按键

MSU2_MINI 面板上有一个触摸按键，通过 ADC 通道 9 检测按下。

### 判定逻辑

| 手势 | 触发条件 |
|---|---|
| 单击 | 按下后松开，两次按下之间超过 350ms |
| 双击 | 两次短按之间小于 350ms |
| 长按 | 按住超过 800ms |

### 阈值标定

不同板子的触摸 ADC 基线不同，程序启动时会自动采集前 6 个样本作为基准值，日志里会打印：

```
[INFO] msu2: 触摸基准 = 1234
```

如果按键**完全没反应**或**一直误触发**，说明阈值不对。用 `--log DEBUG` 观察按下时日志里的 ADC 值：

```
[DEBUG] touch adc=1130 base=1234
```

把 `config.json` 里 `touch.threshold` 设为 `(基准 - 按下值) / 2` 左右。默认 60。

### 手动改配置

不方便用 Web 时，直接改 `config.json`：

```json
"touch": {
  "single": "passthrough",
  "double": "passthrough",
  "long":   "passthrough",
  "macro_single": "",
  "macro_double": "",
  "macro_long":   "",
  "program_single": "",
  "program_double": "",
  "program_long":   "",
  "threshold": 60
}
```

---

## 命令行参数

```bash
python msu2_mini.py [选项]
```

| 参数 | 说明 |
|---|---|
| `--list` | 列出所有串口 |
| `--plugins` | 列出所有已发现的插件 |
| `--init-config` | 删除旧配置，重新生成 `config.json` |
| `--headless` | 无头模式，不启动 WebUI |
| `--log DEBUG` | 打印 DEBUG 级日志 |
| `--config 2333.json` | 指定配置文件路径 |
| `--port /dev/ttyACM0` | 手动指定串口，跳过自动扫描 |

---

## 打包成二进制文件

如果你想在不含 Python 的机器上运行，打包成单文件即可。

### Windows

装好 `pyinstaller` 后直接双击 `build.bat`：

```bash
pip install pyinstaller
```

产物在 `dist/MSU2_MINI.exe`。

**注意**：

- 想看到控制台窗口输出日志，删掉 `build.bat` 里的 `--noconsole`
- 想动态添加插件，删掉 `--onefile`，这样 exe 旁边会有一个 `_internal` 文件夹，可以往里丢新的 `.py` 文件。**不推荐，偏离打包的意义**

### Linux

```bash
pip install pyinstaller
chmod +x build.sh
./build.sh
```

产物在 `dist/MSU2_MINI`。

**注意**：同 Windows 的说明，`--onefile` 与 `--noconsole` 取舍相同。

### macOS

未测试。理论命令与 Linux 相同。

---

## 插件开发

**核心理念**：加插件 = 在 `widgets/` 里新建一个 `.py` 文件。主程序、`msu2_core`、其它插件都不用动。

### 最小示例

新建 `widgets/hello.py`：

```python
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, get_font

@register
class HelloWidget(Widget):
    """第一行 docstring 会成为插件描述。"""
    name = "hello"              # 唯一 ID
    interval = 1.0              # 刷新间隔（秒）
    orientations = BOTH         # BOTH / (LANDSCAPE,) / (PORTRAIT,)
    priority = 100              # 排序权重，越小越靠前

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), "Hello",
               fill="white", font=get_font(24), anchor="mm")
        return img
```

保存后重启程序，Web 页面上就会出现 `hello` 插件。

### 声明式配置

只要声明 `config_schema`，Web 上会自动出现 `⚙` 按钮和表单：

```python
from msu2_core import Field

class MyWidget(Widget):
    config_schema = [
        Field("name", "str", "名字", default="世界"),
        Field("count", "int", "次数", default=10, min=1, max=100),
        Field("mode", "select", "模式", default="A", options=["A", "B", "C"]),
        Field("items", "list_str", "列表", default=["a", "b", "c"],
              help="每行一个"),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        name = cfg["name"]      # 用户填的值，未填则是 default
```

字段类型：`str` / `int` / `float` / `bool` / `select` / `list_str`

### 声明式按钮

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

Web 页面会自动生成"开始"、"停止"两个按钮。

### 响应硬件触控

当「全局触控」三个手势都设为 `未定义（透传给插件）` 时，触摸事件会**透传给当前插件**，通过 `on_action` 收到下列动作名：

| action | 触发时机 |
|---|---|
| `touch_single` | 单击 |
| `touch_double` | 双击 |
| `touch_long` | 长按 |

最小示例：

```python
class MyWidget(Widget):
    def on_action(self, action, payload, ctx):
        if action == "touch_single":
            ctx.state["page"] = ctx.state.get("page", 0) + 1
            return {"ok": True}
        if action == "touch_double":
            ctx.state["page"] = ctx.state.get("page", 0) - 1
            return {"ok": True}
        if action == "touch_long":
            ctx.state["page"] = 0
            return {"ok": True}
        return {"ok": False, "err": f"未知动作 {action}"}
```

`return {"ok": True}` 会让主程序立刻触发重绘，不用等 `interval`。

参考实现：`music` / `todo` / `wifi_scanner` / `clipboard`。

### 渲染提示

- `ctx.size` 会随朝向变化，**不要硬编码** 160×80
- 返回 `RGBA` 图片，透明区域会透出背景
- **缓存机制**：内容未变化时返回 `None`，可大幅降低带宽占用：
  ```python
  def render(self, ctx):
      key = f"{int(ctx.now)}"     # 每秒变一次
      if ctx.state.get("_last_key") == key:
          return None
      ctx.state["_last_key"] = key
      # ... 绘制 ...
  ```
- **切页清缓存**：使用 `_last_key` 缓存的插件，切回来时必须清缓存：
  ```python
  def on_enter(self, ctx):
      ctx.state.pop("_last_key", None)
  ```

### Widget API 速览

| 方法 | 调用时机 | 用途 |
|---|---|---|
| `setup(ctx)` | 插件加载时 | 读文件、开线程、初始化状态 |
| `teardown(ctx)` | 插件卸载 / 退出时 | 关闭线程、释放资源 |
| `on_enter(ctx)` | 成为当前页时 | 清缓存，强制重绘 |
| `on_leave(ctx)` | 离开当前页时 | 暂停后台工作 |
| `render(ctx)` | 主循环按 `interval` 调 | 返回图像或 `None` |
| `get_config(ctx)` | 任意时刻 | 读自己那份配置 |
| `on_action(action, payload, ctx)` | Web 按钮 / 硬件触控 | 处理交互动作 |

`ctx` 字段：`size` / `orientation` / `now` / `state`（插件专属字典）/ `config` / `device`

---

## 项目结构

```
MSU2_MINI_Console/
├── msu2_mini.py              # 主程序
├── build.bat                 # Windows 打包脚本
├── build.sh                  # Linux 打包脚本
├── README.md
├── widgets-dev.md            # 插件开发手册
├── msu2_core/ 
│   ├── __init__.py           # 对外 API
│   ├── widget.py             # Widget 基类 + 渲染上下文 + 字体
│   ├── registry.py           # 插件注册表
│   └── config_schema.py      # 配置字段类型
└── widgets/                  # 插件目录
    ├── __init__.py           # 自动发现
    ├── clock.py
    ├── pcstats.py
    ├── music.py
    └── ...                   # 其它插件
```

---

## 常见问题

**Q: 屏幕上的中文字显示成问号 / 方块？**

字体问题。Windows 上一定有 `msyh.ttc`（微软雅黑），Linux 可能没有：

```bash
# Ubuntu / Debian
sudo apt install fonts-noto-cjk
```

或者在 `config.json` 里显式指定字体路径：

```json
"font": "C:/Windows/Fonts/msyh.ttc"
```

---

**Q: 切页要等好几秒？**

19200 波特率下传一整帧需要 13 秒。切页响应速度取决于当前帧传到哪里了，最坏情况要等一整帧。这是物理限制

---

**Q: 硬件触控按键没反应 / 一直误触发？**

阈值问题。看启动日志：

```
[INFO] msu2: 触摸基准 = 1234
```

用 `--log DEBUG` 观察按下时的 ADC 值，然后到 Web 的「全局触控」里改 `threshold`（或直接改 `config.json`），一般设为 **基准值与按下值差值的一半**。

---

**Q: 触摸按键单击没反应，但双击有反应？**

单击有 350ms 的延迟——因为要等双击窗口关闭才能确定是单击。这是判定逻辑的必然结果，无法避免。

---

**Q: 键盘宏（`ctrl+shift+s`）没反应？**

需要装 `pynput`：

```bash
pip install pynput
```

macOS 不清楚

Linux Wayland 会话下 `pynput` 不工作，换 X11 会话。

---

**Q: 自定义程序填了路径但启动不了？**

`自定义程序` 走系统 shell（Windows 是 `cmd.exe`，Linux 是 `/bin/sh`），路径里有空格要用引号：

- Windows：`"C:\Program Files\SomeApp\app.exe"`
- Linux：`"/opt/Some App/app"`

也可以直接填系统 PATH 里的命令：`notepad`、`calc`、`firefox`。

---

**Q: 音乐封面显示不出来？**

我没做好

---

**Q: CPU / 内存 / 磁盘读数不对？**

`psutil.cpu_percent()` 第一次调用返回 0，是正常的。启动 1 秒后才会显示真实值。

磁盘读的是**系统盘**（Windows 是 `C:\`，Linux 是 `/`）。

---

**Q: 怎么隐藏程序窗口？**

用 `pyinstaller` 打包时带 `--noconsole` 参数（`build.bat` 里已经包含）。打包后启动无窗口，浏览器控制台底部有"关闭程序"按钮。

---

**Q: Linux 下 `clipboard` 插件显示 "无法读取剪贴板"？**

需要装剪贴板工具：

```bash
sudo apt install xclip              # X11
sudo apt install wl-clipboard       # Wayland
```

---

**Q: Linux 下找不到串口？**

检查用户是否在 `dialout` 组：

```bash
groups $USER | grep dialout
# 如果没有：
sudo usermod -a -G dialout $USER
```

注销重新登录后生效。

---

## 致谢

感谢 墨砺工作室 提供 MSU2 系列硬件及官方 demo

---

## 许可证

本项目采用 **PolyForm Noncommercial License 1.0.0**。

- **允许**：个人使用、学习、研究、教学、非营利组织内部使用
- **允许**：修改、二次开发、再分发（须保留许可协议和版权声明）
- **禁止**：任何商业用途（包括但不限于：销售、付费服务、商业产品集成、企业内部商用系统）

> **附加说明**：MSU2 系列硬件的通信协议、固件等相关信息，版权归墨砺工作室所有。本项目仅为个人学习作品，不代表官方立场，请勿将本项目用于任何商业目的。