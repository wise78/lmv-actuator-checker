; LMV Actuator Checker -- Inno Setup installer script.
;
; Compile with:  ISCC.exe installer\sqm45_checker.iss
; Input is the PyInstaller --onedir output at scripts\desktop\dist\LMV Actuator Checker\
; -- build that first (see desktop/BUILD.md), this script does not build it.
;
; Version is duplicated here from sqm45/_version.py rather than read at
; compile time (Inno's preprocessor has no clean "read this Python file" verb
; without an external tool). Keep the two in step when cutting a release --
; desktop/build.py prints a reminder for exactly this.

#define MyAppName "LMV Actuator Checker"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "LMV Actuator Checker Project"
#define MyAppExeName "LMV Actuator Checker.exe"
#define SourceDir "..\scripts\desktop\dist\LMV Actuator Checker"

[Setup]
; AppId is unchanged across the SQM45 Checker -> LMV Actuator Checker rename
; on purpose -- this is the same product under a broader name (it now covers
; every LMV actuator, not just the SQM45), not a fork. Keeping the AppId
; lets Inno recognise an existing install and upgrade it in place instead of
; installing a second copy alongside the old one.
AppId={{B7B2E4B0-7B7D-4C8C-9C7A-4B0F4B7B2E4B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user by default (no admin prompt) -- a technician's own laptop is the
; expected install target, not a shared machine needing elevation.
PrivilegesRequired=lowest
OutputBaseFilename=LMVActuatorCheckerSetup-{#MyAppVersion}
OutputDir=output
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\scripts\desktop\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; ARM64 builds are out of scope for now -- pythonnet/clr_loader's WebView2
; bridge is x64-only in the toolchain used to build this.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Recurse the entire onedir output -- the exe, its _internal/ dependency
; folder, and the bundled templates/static/demo data all travel together.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app writes NOTHING under {app} at runtime (savedialog.py's whole point
; is that every export goes to a folder the operator chooses, normally
; Documents\LMV Actuator Checker -- see sqm45/savedialog.py). Nothing extra to clean
; up here beyond what Inno already removes; this section is left as a marker
; for anyone tempted to add app-local state later; ; don't, without also
; updating this comment and the uninstaller.
