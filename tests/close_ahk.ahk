#Requires AutoHotkey v2.0
DetectHiddenWindows true
if A_Args.Length != 1 {
    ExitApp 1
}
try {
    WinClose "ahk_pid " A_Args[1]
} catch {
    ExitApp 2
}
ExitApp 0
