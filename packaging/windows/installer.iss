; ============================================================
;  TouchFishServer - Windows 安装脚本 (Inno Setup 6)
;
;  编译:
;    iscc.exe installer.iss
;
;  前置条件: 先用 PyInstaller 生成 dist 目录
;    pyinstaller main.py --collect-all captcha --collect-all werkzeug --paths . --name TouchFishServer
;
;  PyInstaller onedir 产物位于 dist\TouchFishServer\（内含
;  TouchFishServer.exe 与 _internal\）。本脚本直接打包该内层目录内容，
;  使安装后 {app}\TouchFishServer.exe 位于安装根目录，不再多套一层。
;
;  相对路径均相对于本 .iss 文件所在目录 (packaging/windows)
;  简体中文语言文件 ChineseSimplified.isl 已随仓库提供
;  （来自 https://github.com/jrsoftware/issrc/tree/main/Files/Languages ，
;   见 Languages/ChineseSimplified.isl），因此不依赖本机 Inno Setup
;   安装是否包含该语言文件。
; ============================================================

#define MyAppName "TouchFishServer"
#define MyAppVersion "dev-build"
#define MyAppPublisher "TouchFish"
#define MyAppExeName "TouchFishServer.exe"

#define MyAppSourceDir "..\..\dist\TouchFishServer"
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
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
