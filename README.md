# translate_tool

一个 Windows 桌面划词翻译工具：选中文本后按快捷键，自动调用翻译接口，弹出翻译结果窗口，并把原文和译文按日期保存到本地文件。

当前项目主要由三部分组成：

- `translator.ahk`：AutoHotkey v2 脚本，负责注册快捷键、复制选中文本、写入翻译请求。
- `resident_app.py`：Python + PySide6 常驻程序，负责轮询请求、展示翻译窗口。
- `translate.py`：翻译核心逻辑，负责读取配置、调用有道智云 API、保存翻译记录。

---

## 功能特性

- 支持选中文字后按快捷键翻译。
- 支持常驻模式，避免每次翻译都重新启动 Python/PySide6。
- 支持有道智云文本翻译 API。
- 支持 `mock` 测试模式，不调用真实 API。
- 翻译结果按日期追加保存到本地文件。
- 结果窗口支持：
  - 复制译文
  - 打开保存文件
  - 手动输入文本翻译
  - `Enter` / `Esc` 快速隐藏窗口
- 支持通过 `config.json` 配置快捷键、保存目录、语言和翻译服务。

---

## 环境要求

### 1. Windows

当前工具依赖 AutoHotkey 和 Windows 剪贴板/快捷键机制，主要面向 Windows 使用。

### 2. Python

建议使用 Python 3.10 或以上版本。

### 3. AutoHotkey v2

需要安装 AutoHotkey v2。

注意：本项目的 `translator.ahk` 使用的是 AutoHotkey v2 语法，不兼容 AutoHotkey v1。

### 4. 有道智云 API

如果使用真实翻译，需要准备有道智云文本翻译应用的：

- 应用 ID，即 `YOUDAO_APP_KEY`
- 应用密钥，即 `YOUDAO_APP_SECRET`

如果只是测试流程，可以使用 `mock` 模式，不需要有道 API Key。

---

## 安装依赖

在项目目录执行：

```bash
pip install -r requirements.txt
```

当前 Python 依赖包括：

```txt
requests
python-dotenv
PySide6
```

---

## 配置 `.env`

在项目根目录创建 `.env` 文件：

```env
YOUDAO_APP_KEY=你的有道应用ID
YOUDAO_APP_SECRET=你的有道应用密钥
```

示例：

```env
YOUDAO_APP_KEY=xxxxxxxx
YOUDAO_APP_SECRET=xxxxxxxxxxxxxxxx
```

`.env` 里保存的是密钥信息，不建议提交到 Git 仓库。

---

## 配置 `config.json`

项目会读取根目录下的 `config.json`。如果文件不存在，程序会使用代码里的默认配置。

推荐创建一个 `config.json`：

```json
{
  "save_dir": "D:/translate_tool/data",
  "provider": "youdao",
  "target_language": "zh-CHS",
  "source_language": "auto",
  "youdao_api_url": "https://openapi.youdao.com/api",
  "hotkey": "Ctrl+Alt+T"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `save_dir` | 翻译记录保存目录 |
| `provider` | 翻译服务，可选 `youdao` 或 `mock` |
| `target_language` | 目标语言，中文简体为 `zh-CHS` |
| `source_language` | 源语言，默认 `auto` 自动识别 |
| `youdao_api_url` | 有道文本翻译 API 地址 |
| `hotkey` | 翻译快捷键，例如 `Ctrl+Alt+T`、`Alt+T` |

如果想先测试程序流程，不调用有道 API，可以设置：

```json
{
  "provider": "mock"
}
```

---

## 使用方法

### 方式一：手动启动

1. 双击运行项目目录里的 `translator.ahk`。
2. 在任意软件里选中一段文本。
3. 按配置的快捷键，默认是：

   ```text
   Ctrl+Alt+T
   ```

4. 等待翻译结果窗口弹出。
5. 在结果窗口中可以：
   - 点击“复制译文”复制翻译结果
   - 点击“打开保存文件”打开当天的翻译记录
   - 点击“手动输入”手动输入文本翻译
   - 按 `Enter` / `Esc` 隐藏窗口

第一次使用时可能会稍慢，因为 `translator.ahk` 会启动 Python/PySide6 常驻进程。常驻进程启动后，后续翻译通常会更快。

---

## 桌面 AHK 图标 / 快捷方式

当前项目代码**不会自动创建桌面图标**。

如果你的桌面上已经有一个可以直接点击的 AHK 图标，通常是以下情况之一：

- 你之前手动创建过 `translator.ahk` 的桌面快捷方式。
- Windows 已经把 `.ahk` 文件关联给 AutoHotkey，所以脚本本身可以双击运行。
- 某些编辑器或工具帮你创建过快捷方式。

也就是说：桌面图标目前不是程序自动生成的。如果换一台电脑或重新部署项目，需要手动创建。

### 手动创建桌面快捷方式

推荐做法：

1. 找到项目中的：

   ```text
   D:\translate_tool\translator.ahk
   ```

2. 右键 `translator.ahk`。
3. 选择“发送到” → “桌面快捷方式”。
4. 之后双击桌面上的快捷方式即可启动翻译工具。

### 设置开机自启

如果希望每次开机后自动启动翻译快捷键：

1. 按 `Win + R`。
2. 输入：

   ```text
   shell:startup
   ```

3. 回车后会打开 Windows 启动目录。
4. 把 `translator.ahk` 的快捷方式复制到这个目录。
5. 下次开机后，Windows 会自动运行该快捷方式。

注意：这里只是 Windows 的启动项配置，不是当前程序自动创建的。

---

## 工作流程

整体流程如下：

```text
用户选中文本
  ↓
按 AutoHotkey 快捷键
  ↓
translator.ahk 复制选中文本
  ↓
translator.ahk 确认 resident_app.py 常驻进程是否存活
  ↓
如果未启动，则启动 resident_app.py
  ↓
translator.ahk 写入 data/requests/request_xxx.json
  ↓
resident_app.py 轮询请求目录并读取请求
  ↓
translate.py 读取 config.json 和 .env
  ↓
translate.py 调用有道 API 或 mock 翻译
  ↓
translate.py 保存翻译记录
  ↓
resident_app.py 弹出结果窗口
```

---

## 文件说明

| 文件/目录 | 说明 |
| --- | --- |
| `translator.ahk` | AutoHotkey v2 快捷键入口 |
| `resident_app.py` | PySide6 常驻 GUI 程序 |
| `translate.py` | 翻译核心逻辑 |
| `requirements.txt` | Python 依赖列表 |
| `.env` | 有道 API Key，本地创建，不建议提交 |
| `config.json` | 用户配置文件，可选 |
| `data/` | 运行时数据目录，保存请求、日志和翻译记录 |
| `data/requests/` | AHK 写入的翻译请求目录 |
| `data/processed/` | 已处理或失败请求归档目录 |
| `data/perf.log` | 性能日志 |
| `data/resident.log` | 常驻进程日志 |

---

## 常见问题

### 1. 第一次翻译为什么比较慢？

第一次翻译时，`translator.ahk` 需要启动 `resident_app.py`，而 `resident_app.py` 会加载 Python、PySide6 和界面组件，所以会慢一些。

常驻进程启动后，后续翻译不需要反复冷启动，一般会快很多。

### 2. 按快捷键没有反应怎么办？

可以检查：

- 是否安装的是 AutoHotkey v2。
- `translator.ahk` 是否正在运行。
- 当前快捷键是否被其他软件占用。
- 是否真的选中了文本。
- `config.json` 中的 `hotkey` 写法是否正确。

### 3. 提示没有配置 `YOUDAO_APP_KEY` 或 `YOUDAO_APP_SECRET`

说明 `.env` 文件不存在，或字段名不正确。

请确认项目根目录有 `.env` 文件，并且包含：

```env
YOUDAO_APP_KEY=你的有道应用ID
YOUDAO_APP_SECRET=你的有道应用密钥
```

### 4. 如何不调用有道 API，只测试窗口和流程？

把 `config.json` 中的 `provider` 改成：

```json
{
  "provider": "mock"
}
```

然后重新运行 `translator.ahk`。

### 5. 如何退出工具？

可以在系统托盘找到 AutoHotkey 图标，右键退出 `translator.ahk`。

如果需要结束 Python 常驻进程，可以在任务管理器中结束对应的 `pythonw.exe` / `python.exe` 进程。

### 6. 翻译记录保存在哪里？

默认保存到：

```text
D:/translate_tool/data
```

每天一个文件，例如：

```text
2026-05-25.txt
```

可以通过 `config.json` 的 `save_dir` 修改保存目录。

---

## 开发备注

Python 语法检查：

```bash
python -m py_compile translate.py resident_app.py
```

AutoHotkey 脚本需要使用 AutoHotkey v2 运行。
