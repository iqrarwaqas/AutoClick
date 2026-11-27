; Inno Setup script for AutoClick.
; Build the app first (pyinstaller packaging/AutoClick.spec), then from the repo root:
;   iscc /DAppVersion=1.0.0 installer\AutoClick.iss
; Output: dist\AutoClick-Setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: it is how a new installer finds and upgrades the old install.
AppId={{A4F82D91-6C3E-4B18-9E5A-2D7C0F41B836}
AppName=AutoClick
AppVersion={#AppVersion}
AppPublisher=Synapsify
AppPublisherURL=https://github.com/iqrarwaqas/AutoClick
DefaultDirName={autopf}\AutoClick
DefaultGroupName=AutoClick
DisableProgramGroupPage=yes
; Per-user install (no admin prompt), so updates can install silently.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AutoClick-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayIcon={app}\AutoClick.exe
SetupIconFile=..\packaging\icon.ico

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Remove the old version's libraries so nothing stale is left after an update.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\AutoClick\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AutoClick"; Filename: "{app}\AutoClick.exe"
Name: "{autodesktop}\AutoClick"; Filename: "{app}\AutoClick.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoClick.exe"; Description: "Start AutoClick"; Flags: nowait postinstall skipifsilent
; In-app updates run the installer with /SILENT /RELAUNCH=1: start the new version afterwards.
Filename: "{app}\AutoClick.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
