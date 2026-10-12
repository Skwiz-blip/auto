; Installateur Inno Setup de « KYM » (Know Your Merchant), compilé par construire.bat.
; Installation sans droits administrateur, dans %LOCALAPPDATA%\Programs\KYM (une version
; précédente, « Contrôle KIK », est mise à jour sur place : même AppId).
; La lecture des documents (PP-OCR) et le lecteur de QR sont embarqués : rien d'autre à installer.
; La clé API Claude se colle ensuite dans l'application (Paramètres).

#define Nom "KYM"
#define Version "1.1"

[Setup]
AppId={{6B7E1C52-3F0A-4F4B-9A61-2C8E5D1B7A90}
AppName={#Nom}
AppVersion={#Version}
AppPublisher=KIK EXPERIENCE
DefaultDirName={autopf}\KYM
DefaultGroupName={#Nom}
UsePreviousGroup=no
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=sortie
OutputBaseFilename=Setup-KYM
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
Source: "dist\KYM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ancienne version « Contrôle KIK » : exe et raccourcis remplacés par ceux de KYM
[InstallDelete]
Type: files; Name: "{app}\Controle KIK.exe"
Type: files; Name: "{autodesktop}\Contrôle KIK.lnk"
Type: filesandordirs; Name: "{autoprograms}\Contrôle KIK"

[Icons]
Name: "{group}\{#Nom}"; Filename: "{app}\KYM.exe"
Name: "{autodesktop}\{#Nom}"; Filename: "{app}\KYM.exe"; Tasks: bureau

[Run]
Filename: "{app}\KYM.exe"; Description: "Ouvrir {#Nom}"; Flags: nowait postinstall skipifsilent
