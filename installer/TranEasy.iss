#define AppVersion "0.1.0"
[Setup]
AppId={{D7C627CB-0B2B-4D88-B2AC-3AA19FD389E1}
AppName=TranEasy
AppVersion={#AppVersion}
AppPublisher=TranEasy
DefaultDirName={localappdata}\Programs\TranEasy
DefaultGroupName=TranEasy
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=TranEasy-Setup-v{#AppVersion}
SetupIconFile=..\assets\transeasy-icon.ico
UninstallDisplayIcon={app}\TranEasy.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=TranEasy.exe,ScreenTransResident.exe
RestartApplications=no
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\ScreenTrans\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installed.mode"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\TranEasy\TranEasy"; Filename: "{app}\TranEasy.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\transeasy-icon.ico"
Name: "{userdesktop}\TranEasy"; Filename: "{app}\TranEasy.exe"; WorkingDir: "{app}"; IconFilename: "{app}\assets\transeasy-icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\TranEasy.exe"; Description: "Launch TranEasy"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeUninstall(): Boolean;
var ResultCode: Integer;
begin
  Result := True;
  if FileExists(ExpandConstant('{app}\TranEasy.exe')) then
  begin
    Result := Exec(ExpandConstant('{app}\TranEasy.exe'), '--exit', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if Result then Result := ResultCode = 0;
    if not Result then MsgBox('Please exit TranEasy before uninstalling.', mbError, MB_OK);
  end;
end;
