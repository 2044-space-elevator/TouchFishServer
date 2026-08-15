; ============================================================
;  TouchFishServer - Windows 安装脚本 (Inno Setup 6)
;
;  编译:
;    iscc.exe installer.iss
;
;  前置条件: 先用 PyInstaller 生成 dist 目录
;    pyinstaller main.py --collect-all captcha --collect-all werkzeug --paths . --name TouchFishServer
;
;  相对路径均相对于本 .iss 文件所在目录 (packaging/windows)
; ============================================================

#define MyAppName "TouchFishServer"
#define MyAppVersion "dev-build"
#define MyAppPublisher "TouchFish"
#define MyAppExeName "TouchFishServer.exe"

#define MyAppSourceDir "..\..\dist"
#define MyAppOutputDir "..\.."
#define MyAppOutputFilename "TouchFishServer_windows-setup"

[Setup]
AppId={{7B8A9C0D-1E2F-4A3B-8C9D-0E1F2A3B4C5D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#MyAppOutputDir}
OutputBaseFilename={#MyAppOutputFilename}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
