; Inno Setup script for a per-user Windows install (no administrator rights needed).
; Build after PyInstaller with: iscc /DAppVersion=0.1.0 packaging\windows\inat2exif.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{5E0C4A55-8F86-4B6E-9B0B-2C1D8E4A7F21}
AppName=inat2exif
AppVersion={#AppVersion}
AppPublisher=inat2exif
DefaultDirName={localappdata}\Programs\inat2exif
DefaultGroupName=inat2exif
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=inat2exif-{#AppVersion}-windows-x64-setup
SetupIconFile=..\icons\inat2exif.ico
UninstallDisplayIcon={app}\inat2exif.exe
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\build\dist\inat2exif\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\inat2exif"; Filename: "{app}\inat2exif.exe"
Name: "{autodesktop}\inat2exif"; Filename: "{app}\inat2exif.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\inat2exif.exe"; Description: "{cm:LaunchProgram,inat2exif}"; Flags: nowait postinstall skipifsilent
