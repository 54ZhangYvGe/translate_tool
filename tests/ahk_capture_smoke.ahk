#Requires AutoHotkey v2.0
if A_Args.Length != 1 {
    ExitApp 1
}
; AHK intentionally ignores its own injected input at the default send level.
SendLevel 1
SendEvent "^!{F11}"
hwnd := WinWait("截图翻译 ahk_pid " A_Args[1], , 8)
if !hwnd {
    ExitApp 2
}
ControlSend "{Esc}", , "ahk_id " hwnd
if !WinWaitClose("ahk_id " hwnd, , 5) {
    ExitApp 3
}
ExitApp 0
