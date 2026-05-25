#Requires AutoHotkey v2.0
#SingleInstance Force

global APP_TITLE := "translate_tool"
global BASE_DIR := A_ScriptDir
global CONFIG_PATH := BASE_DIR "\config.json"
global DEFAULT_HOTKEY := "Ctrl+Alt+T"
global DATA_DIR := BASE_DIR "\data"
global REQUEST_DIR := DATA_DIR "\requests"
global STATE_FILE := DATA_DIR "\app_state.json"
global RESIDENT_VERSION := "2026-05-25-resident-keepalive-1"

hotkeyText := LoadHotkeyFromConfig(CONFIG_PATH, DEFAULT_HOTKEY)
ahkHotkey := ConvertHotkeyToAhk(hotkeyText)

try {
    Hotkey ahkHotkey, TranslateSelectedText
} catch Error as err {
    MsgBox "配置里的 hotkey 无法注册：" hotkeyText "`n`n将回退到默认热键：" DEFAULT_HOTKEY "`n`n错误信息：" err.Message, APP_TITLE
    Hotkey ConvertHotkeyToAhk(DEFAULT_HOTKEY), TranslateSelectedText
}


TranslateSelectedText(*) {
    oldClip := A_Clipboard
    A_Clipboard := ""

    ; 复制当前选中文字
    Send "^c"

    if !ClipWait(1) {
        MsgBox "没有获取到选中文字。请先选中文字，再按配置的翻译快捷键。", APP_TITLE
        A_Clipboard := oldClip
        return
    }

    selectedText := A_Clipboard

    if Trim(selectedText) = "" {
        MsgBox "选中的内容为空。", APP_TITLE
        A_Clipboard := oldClip
        return
    }

    EnsureResidentApp()

    requestId := BuildRequestId()
    WriteTranslateRequest(requestId, selectedText, "selection")

    ToolTip "正在翻译，请稍候..."
    Sleep 250
    ToolTip

    A_Clipboard := oldClip
}


EnsureResidentApp() {
    DirCreate DATA_DIR
    DirCreate REQUEST_DIR

    if IsResidentAlive() {
        return
    }

    TryStopResidentIfVersionMismatch()

    pythonScript := BASE_DIR "\resident_app.py"
    pythonExe := ResolvePythonGuiExecutable()
    cmd := '"' pythonExe '" "' pythonScript '"'
    Run cmd

    Loop 20 {
        Sleep 200
        if IsResidentAlive() {
            return
        }
    }

    MsgBox "常驻翻译进程启动失败，请检查 resident_app.py 或 Python 环境。", APP_TITLE
}


IsResidentAlive() {
    if !FileExist(STATE_FILE) {
        return false
    }

    modified := FileGetTime(STATE_FILE, "M")
    nowTs := DateDiff(A_Now, modified, "Seconds")
    if Abs(nowTs) > 10 {
        return false
    }

    return GetResidentVersion() = RESIDENT_VERSION
}


TryStopResidentIfVersionMismatch() {
    if !FileExist(STATE_FILE) {
        return
    }

    pid := GetResidentPid()
    if pid = "" {
        return
    }

    version := GetResidentVersion()
    if version = RESIDENT_VERSION {
        return
    }

    try {
        RunWait A_ComSpec ' /c taskkill /PID ' pid ' /T /F', , "Hide"
    } catch {
    }

    Sleep 300
}


GetResidentPid() {
    if !FileExist(STATE_FILE) {
        return ""
    }

    try {
        content := FileRead(STATE_FILE, "UTF-8")
        if RegExMatch(content, '"pid"\s*:\s*(\d+)', &match) {
            return match[1]
        }
    } catch {
    }

    return ""
}


GetResidentVersion() {
    if !FileExist(STATE_FILE) {
        return ""
    }

    try {
        content := FileRead(STATE_FILE, "UTF-8")
        if RegExMatch(content, '"version"\s*:\s*"([^"]+)"', &match) {
            return Trim(match[1])
        }
    } catch {
    }

    return ""
}


BuildRequestId() {
    rand := Random(1000, 9999)
    return FormatTime(A_Now, "yyyyMMdd_HHmmss") "_" A_TickCount "_" rand
}


WriteTranslateRequest(requestId, text, source) {
    DirCreate REQUEST_DIR

    payload := '{'
        . '"id":"' JsonEscape(requestId) '",' 
        . '"text":"' JsonEscape(text) '",' 
        . '"source":"' JsonEscape(source) '",' 
        . '"created_at":"' JsonEscape(FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss")) '"'
        . '}'

    tempPath := REQUEST_DIR "\request_" requestId ".tmp"
    finalPath := REQUEST_DIR "\request_" requestId ".json"

    if FileExist(tempPath) {
        FileDelete tempPath
    }
    if FileExist(finalPath) {
        FileDelete finalPath
    }

    FileAppend payload, tempPath, "UTF-8"
    FileMove tempPath, finalPath
    return finalPath
}


JsonEscape(text) {
    value := text
    value := StrReplace(value, "\", "\\")
    value := StrReplace(value, '"', '\"')
    value := StrReplace(value, "`r", "\r")
    value := StrReplace(value, "`n", "\n")
    value := StrReplace(value, "`t", "\t")
    return value
}


ResolvePythonGuiExecutable() {
    localAppData := EnvGet("LocalAppData")
    candidates := [
        localAppData "\Programs\Python\Python313\pythonw.exe",
        localAppData "\Programs\Python\Python312\pythonw.exe",
        localAppData "\Programs\Python\Python311\pythonw.exe",
        localAppData "\Programs\Python\Python310\pythonw.exe",
        localAppData "\Programs\Python\Python39\pythonw.exe",
        localAppData "\Programs\Python\Python313\python.exe",
        localAppData "\Programs\Python\Python312\python.exe",
        localAppData "\Programs\Python\Python311\python.exe",
        localAppData "\Programs\Python\Python310\python.exe",
        localAppData "\Programs\Python\Python39\python.exe"
    ]

    for candidate in candidates {
        if FileExist(candidate) {
            return candidate
        }
    }

    return "python"
}


LoadHotkeyFromConfig(configPath, fallbackHotkey) {
    if !FileExist(configPath) {
        return fallbackHotkey
    }

    try {
        content := FileRead(configPath, "UTF-8")
        if RegExMatch(content, '"hotkey"\s*:\s*"([^"]+)"', &match) {
            value := Trim(match[1])
            if value != "" {
                return value
            }
        }
    } catch {
    }

    return fallbackHotkey
}


ConvertHotkeyToAhk(hotkeyText) {
    normalized := StrReplace(Trim(hotkeyText), " ", "")
    if normalized = "" {
        return "^!t"
    }

    parts := StrSplit(normalized, "+")
    key := parts[parts.Length]
    modifiers := ""

    Loop parts.Length - 1 {
        part := StrLower(parts[A_Index])
        switch part {
            case "ctrl", "control":
                modifiers .= "^"
            case "alt":
                modifiers .= "!"
            case "shift":
                modifiers .= "+"
            case "win", "windows":
                modifiers .= "#"
            default:
                throw Error("不支持的修饰键：" parts[A_Index])
        }
    }

    lowerKey := StrLower(key)
    specialKeys := Map(
        "enter", "Enter",
        "esc", "Esc",
        "escape", "Esc",
        "tab", "Tab",
        "space", "Space",
        "delete", "Delete",
        "del", "Delete",
        "backspace", "Backspace",
        "insert", "Insert",
        "ins", "Insert",
        "home", "Home",
        "end", "End",
        "pgup", "PgUp",
        "pageup", "PgUp",
        "pgdn", "PgDn",
        "pagedown", "PgDn",
        "up", "Up",
        "down", "Down",
        "left", "Left",
        "right", "Right"
    )

    if RegExMatch(lowerKey, "^f([1-9]|1[0-2])$") {
        return modifiers . StrUpper(lowerKey)
    }

    if specialKeys.Has(lowerKey) {
        return modifiers . specialKeys[lowerKey]
    }

    if StrLen(key) = 1 {
        return modifiers . StrLower(key)
    }

    throw Error("不支持的主按键：" key)
}









