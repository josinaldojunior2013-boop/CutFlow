#define AppName "CapCut Light Renderer"
#define AppVersion "1.0.0"

[Setup]
AppId={{9D94A890-7F1F-4D3A-90E3-CC7EF25640B4}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\CapCut Light Renderer
DefaultGroupName={#AppName}
OutputDir=dist\installer
OutputBaseFilename=CapCutLightRenderer-Setup
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CapCutLightRenderer.exe

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "dist\CapCutLightRenderer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\CapCutLightRenderer.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CapCutLightRenderer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CapCutLightRenderer.exe"; Description: "Abrir {#AppName}"; Flags: nowait postinstall skipifsilent