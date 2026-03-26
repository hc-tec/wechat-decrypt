; Inno Setup script (UTF-8)

#define MyAppName "WeChat Data Service"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "WeChatDataService"
#define MyAppExeName "WeChatDataServiceGUI.exe"

[Setup]
AppId={{3D2B1E1C-9C60-4E12-9A71-9C2BFF7C0C7B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\WeChatDataService
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=WeChatDataServiceSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面图标"; GroupDescription: "附加任务:"; Flags: unchecked
Name: "autostart"; Description: "开机自启（登录后后台运行）"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "..\dist\WeChatDataService\*"; DestDir: "{app}\Service"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\WeChatDataServiceGUI\*"; DestDir: "{app}\GUI"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\GUI\{#MyAppExeName}"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\GUI\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "WeChatDataService"; ValueData: """{app}\GUI\{#MyAppExeName}"" --autostart"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\GUI\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

