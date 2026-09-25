; Instalador do Gateway Fiscal (Inno Setup 6). Construído por install\construir_instalador.ps1,
; que prepara build\app (programa) com build\app\runtime (Python incluído, com as dependências).
;
; Instalação sem perguntas (ex.: por um técnico ou em massa):
;   set GATEWAY_ADMIN_PASSWORD=...      (a senha NÃO vai na linha de comando: o Inno regista-a)
;   GatewayFiscal-Setup.exe /VERYSILENT /EMPRESA="Hotel X, Lda" /NIF=5000000000 [/PORTA=8000]
;   [/UTILIZADOR=admin] [/DIR=C:\GatewayFiscal] [/CURRENTUSER]  (sem administrador: arranca ao iniciar sessão)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Comprimento do caminho relativo mais longo do programa (calculado na construção).
#ifndef MaxRelPath
  #define MaxRelPath "200"
#endif

[Setup]
AppId={{8C5B2E47-3E0F-4C6B-9D1F-6A2B7C4E9F10}
AppName=Gateway Fiscal
AppVersion={#AppVersion}
AppVerName=Gateway Fiscal {#AppVersion}
AppPublisher=Gateway Fiscal
DefaultDirName={code:DefaultInstallDir}
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
UsePreviousAppDir=yes
OutputDir=..\dist
OutputBaseFilename=GatewayFiscal-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Gateway Fiscal
SetupLogging=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\Portuguese.isl"

[Tasks]
Name: "firewall"; Description: "Permitir o acesso ao painel a partir de outros computadores da rede (firewall)"; Check: IsAdminInstallMode; Flags: unchecked

[Files]
Source: "..\build\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "parar.ps1"; Flags: dontcopy

[Run]
Filename: "http://localhost:{code:GetPort}/configurar/"; Description: "Abrir o painel na configuração automática"; Flags: shellexec postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install\desinstalar.ps1"" -Pasta ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoverArranque"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\staticfiles"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
var
  CompanyPage: TInputQueryWizardPage;
  IsUpgrade: Boolean;

function SetEnvironmentVariable(lpName: String; lpValue: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function DefaultInstallDir(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := 'C:\GatewayFiscal'
  else
    Result := ExpandConstant('{localappdata}\GatewayFiscal');
end;

function ParamOr(Name, Default: String): String;
begin
  Result := ExpandConstant('{param:' + Name + '|' + Default + '}');
end;

function GetPort(Param: String): String;
begin
  Result := Trim(CompanyPage.Values[4]);
end;

function ExistingInstall(): Boolean;
begin
  Result := FileExists(AddBackslash(WizardDirValue) + '.env');
end;

procedure InitializeWizard();
begin
  CompanyPage := CreateInputQueryPage(wpSelectDir, 'Empresa e administrador',
    'Primeira configuração do Gateway',
    'Estes dados criam a empresa e o utilizador administrador do painel. Depois da instalação, ' +
    'o painel abre na configuração automática, onde se liga à base de dados (com a sua autorização).');
  CompanyPage.Add('Nome da empresa:', False);
  CompanyPage.Add('NIF da empresa:', False);
  CompanyPage.Add('Utilizador administrador:', False);
  CompanyPage.Add('Senha do administrador (mínimo 10 caracteres):', True);
  CompanyPage.Add('Porta do painel:', False);
  CompanyPage.Values[0] := ParamOr('EMPRESA', '');
  CompanyPage.Values[1] := ParamOr('NIF', '');
  CompanyPage.Values[2] := ParamOr('UTILIZADOR', 'admin');
  { Instalação silenciosa: a senha vem da variável de ambiente, nunca da linha de comando. }
  CompanyPage.Values[3] := GetEnv('GATEWAY_ADMIN_PASSWORD');
  CompanyPage.Values[4] := ParamOr('PORTA', GetPreviousData('Porta', '8000'));
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'Porta', GetPort(''));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Numa atualização a empresa e o administrador já existem. }
  Result := (PageID = CompanyPage.ID) and ExistingInstall();
end;

function IsAlnum(S: String): Boolean;
var
  I: Integer;
  C: Char;
begin
  Result := Length(S) > 0;
  for I := 1 to Length(S) do begin
    C := S[I];
    if not (((C >= '0') and (C <= '9')) or ((C >= 'A') and (C <= 'Z')) or ((C >= 'a') and (C <= 'z'))) then
      Result := False;
  end;
end;

function ValidationError(): String;
var
  Port: Integer;
begin
  Result := '';
  Port := StrToIntDef(GetPort(''), 0);
  if Length(AddBackslash(WizardDirValue)) + StrToInt('{#MaxRelPath}') > 250 then
    Result := 'O caminho da pasta de instalação é demasiado longo para o Windows. ' +
      'Escolha uma pasta mais curta, por exemplo C:\GatewayFiscal.'
  else if (Port < 1) or (Port > 65535) then
    Result := 'Porta inválida (1 a 65535).'
  else if not ExistingInstall() then begin
    if Trim(CompanyPage.Values[0]) = '' then
      Result := 'Indique o nome da empresa.'
    else if (not IsAlnum(Trim(CompanyPage.Values[1]))) or (Length(Trim(CompanyPage.Values[1])) > 20) then
      Result := 'NIF inválido (só letras e números, até 20).'
    else if Trim(CompanyPage.Values[2]) = '' then
      Result := 'Indique o utilizador administrador.'
    else if Length(CompanyPage.Values[3]) < 10 then
      Result := 'A senha do administrador tem de ter pelo menos 10 caracteres.';
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Error: String;
begin
  Result := True;
  if CurPageID = CompanyPage.ID then begin
    Error := ValidationError();
    if Error <> '' then begin
      MsgBox(Error, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := ValidationError();
  if Result <> '' then Exit;
  IsUpgrade := ExistingInstall();
  if IsUpgrade then begin
    { Para o Gateway desta pasta para os ficheiros poderem ser substituídos. }
    ExtractTemporaryFile('parar.ps1');
    Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\parar.ps1') +
      '" -Pasta "' + WizardDirValue + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
end;

function RunStep(Caption, Exe, Params: String): Boolean;
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Caption;
  Log('Passo: ' + Caption + ' | ' + Exe);
  Result := Exec(Exe, Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  if not Result then begin
    Log('Falhou com o código ' + IntToStr(Code));
    SuppressibleMsgBox('Falhou: ' + Caption + ' (código ' + IntToStr(Code) + ').' + #13#10 +
      'Veja o registo em ' + ExpandConstant('{app}') + '\logs\gateway.log e o registo da instalação.',
      mbError, MB_OK, IDOK);
  end;
end;

function HasOdbcDriver(): Boolean;
begin
  Result := RegKeyExists(HKLM64, 'SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server') or
            RegKeyExists(HKLM64, 'SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  App, Py, Pyw, Port: String;
  Code: Integer;
begin
  if CurStep = ssPostInstall then begin
    App := ExpandConstant('{app}');
    Py := App + '\runtime\python.exe';
    Pyw := App + '\runtime\pythonw.exe';
    Port := GetPort('');
    if not RunStep('A gerar a configuração...', Py, '"' + App + '\install\gerar_env.py" --saida "' + App +
      '\.env" --dados "' + App + '\dados" --porta ' + Port) then Exit;
    if not RunStep('A preparar a base de dados...', Py, 'manage.py migrate --noinput -v 0') then Exit;
    if not RunStep('A preparar o painel...', Py, 'manage.py collectstatic --noinput -v 0') then Exit;
    if not IsUpgrade then begin
      { A senha passa por variável de ambiente (não aparece na linha de comando nem no registo). }
      SetEnvironmentVariable('GATEWAY_ADMIN_PASSWORD', CompanyPage.Values[3]);
      try
        if not RunStep('A criar a empresa e o administrador...', Py, 'manage.py gateway_bootstrap --empresa "' +
          Trim(CompanyPage.Values[0]) + '" --nif "' + Trim(CompanyPage.Values[1]) + '" --utilizador "' +
          Trim(CompanyPage.Values[2]) + '"') then Exit;
      finally
        SetEnvironmentVariable('GATEWAY_ADMIN_PASSWORD', '');
      end;
    end;
    if WizardIsTaskSelected('firewall') then
      Exec('powershell.exe', '-NoProfile -Command "Get-NetFirewallRule -DisplayName ''Gateway Fiscal'' ' +
        '-ErrorAction SilentlyContinue | Remove-NetFirewallRule; New-NetFirewallRule -DisplayName ''Gateway Fiscal'' ' +
        '-Direction Inbound -Protocol TCP -LocalPort ' + Port + ' -Action Allow -Profile Private,Domain | Out-Null"',
        '', SW_HIDE, ewWaitUntilTerminated, Code);
    RunStep('A arrancar o Gateway...', 'powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + App +
      '\install\tarefa.ps1" -Pasta "' + App + '" -Python "' + Pyw + '" -Porta ' + Port);
  end;
  if (CurStep = ssDone) and not HasOdbcDriver() then
    SuppressibleMsgBox('Para ligar a bases de dados SQL Server instale também o "ODBC Driver 18 for SQL Server" ' +
      'da Microsoft (gratuito). Para outros sistemas (PostgreSQL, MySQL, Oracle) não é preciso.',
      mbInformation, MB_OK, IDOK);
end;
