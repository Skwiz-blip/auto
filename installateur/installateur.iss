; Installateur Inno Setup de « Contrôle KIK » (compilé par construire.bat).
; Installation sans droits administrateur, dans %LOCALAPPDATA%\Programs\Controle KIK.
; La lecture des documents (PP-OCR) et le lecteur de QR sont embarqués : rien d'autre à installer.
; La clé API Claude se colle ensuite dans l'application (Paramètres).

#define Nom "Contrôle KIK"
#define Version "1.0"

[Setup]
AppId={{6B7E1C52-3F0A-4F4B-9A61-2C8E5D1B7A90}
AppName={#Nom}
AppVersion={#Version}
AppPublisher=KIK EXPERIENCE
DefaultDirName={autopf}\Controle KIK
DefaultGroupName={#Nom}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=sortie
OutputBaseFilename=Setup-Controle-KIK
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#Nom}

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "dist\Controle KIK\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#Nom}"; Filename: "{app}\Controle KIK.exe"
Name: "{autodesktop}\{#Nom}"; Filename: "{app}\Controle KIK.exe"; Tasks: bureau

[Run]
Filename: "{app}\Controle KIK.exe"; Description: "Ouvrir {#Nom}"; Flags: nowait postinstall skipifsilent
