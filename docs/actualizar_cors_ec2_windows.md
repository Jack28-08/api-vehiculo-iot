# Actualizar CORS en EC2 Windows y reiniciar únicamente FastAPI

Ejecuta estos pasos **dentro de EC2 Windows Server**, en una misma ventana de
Windows PowerShell 5.1 abierta como administrador. Son para una API ya instalada
en `C:\IoT\api-vehiculo-iot`, con el servicio `ApiVehiculoIoT` y su entorno
`.venv`. La corrección de CORS no necesita nuevas dependencias.

El cambio permite los orígenes `https://jack28-08.github.io` y
`http://localhost:5500`, los métodos `GET`, `POST`, `OPTIONS` y los encabezados
`X-API-Key`, `Content-Type`, `Accept`. El origen no incluye la ruta del sitio.

## 1. Comprobar que el reinicio aislado sea posible

El instalador de este repositorio configura en el XML de Caddy
`<depend>ApiVehiculoIoT</depend>` y WinSW registra esa dependencia en Windows.
Si el Caddy instalado conserva esa dependencia y está activo, Windows impide
reiniciar únicamente la API mediante `Restart-Service` sin forzar los
dependientes. El bloque siguiente detecta ese caso **antes de actualizar Git**.

WinSW 2.12.0 tampoco ofrece aquí un reinicio exclusivo del hijo Uvicorn:
`restart` detiene e inicia el servicio y sus dependientes activos; `restart!`
invoca ese mismo reinicio. No utilices esos comandos como alternativa.
Fuentes: [plantilla local de servicios](../deploy/windows/Windows-ServiceCommon.ps1),
[código oficial de WinSW 2.12.0](https://github.com/winsw/winsw/blob/v2.12.0/src/WinSW/Program.cs#L414-L509)
y [administración de servicios de Microsoft](https://learn.microsoft.com/en-us/powershell/scripting/samples/managing-services).

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Set-Location -LiteralPath 'C:\IoT\api-vehiculo-iot'
$identidadIoT = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalIoT = New-Object Security.Principal.WindowsPrincipal($identidadIoT)
if (-not $principalIoT.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Abre PowerShell como administrador dentro de EC2 Windows Server.'
}
if ((Get-CimInstance Win32_OperatingSystem).ProductType -eq 1) {
    throw 'Este procedimiento es para Windows Server, no para la PC de desarrollo.'
}

function Assert-ApiOnlyRestartIoT {
    $servicioIoT = Get-Service -Name 'ApiVehiculoIoT' -ErrorAction Stop
    if ($servicioIoT.Status -ne 'Running') {
        throw 'ApiVehiculoIoT debe estar Running para este procedimiento.'
    }
    $dependientesIoT = @($servicioIoT.DependentServices | Where-Object { $_.Status -ne 'Stopped' })
    if ($dependientesIoT.Count -gt 0) {
        $dependientesIoT | Select-Object Name, Status | Format-Table -AutoSize
        throw 'Hay dependientes activos: no es posible reiniciar solo FastAPI. No se reinicio ningun servicio; conserva el estado.'
    }
}

Assert-ApiOnlyRestartIoT
Get-Service -Name 'ApiVehiculoIoT' | Select-Object Name, Status
```

**Si aparece el error de dependientes activos, detente aquí.** En particular,
con `CaddyVehiculoIoT` activo y dependiente, el requisito de mantenerlo intacto
impide completar ese reinicio. Hace falta autorizar por separado una ventana
de mantenimiento que permita operar el proxy o cambiar su dependencia; este
procedimiento no realiza esas acciones ni reinstala servicios.

## 2. Establecer una ventana sin DEMO ni escrituras

Coordina con los operadores que nadie inicie DEMO ni envíe escrituras durante
la actualización. Confirma que cualquier DEMO anterior haya terminado en sus
monitores existentes (`demo_fin`, `demo_cancelada` o `demo_error`). Si no puedes
asegurarlo, espera y no continúes. `/health` no informa si hay una DEMO activa;
la ausencia de eventos durante unos segundos tampoco demuestra que terminó.

El reinicio cerrará HTTP en curso y las conexiones WSS. Las tareas DEMO en
memoria no se reanudan: al reconectarse, el cliente recibe el último estado
persistido. No se envía automáticamente ningún POST de DETENER, cancelar o
iniciar DEMO. Esta confirmación es operativa; no consulta RDS.

## 3. Actualizar Git conservando el entorno existente

El repositorio debe estar en `master`, con el remoto esperado y sin cambios
locales. Ante cualquier error, detén el procedimiento y revisa la causa; no
descartes cambios con `reset`, `clean`, `checkout` ni `restore`.
El `.env` existente solo se comprueba por su presencia y exclusión de Git:
no se lee, muestra, copia ni reemplaza. Tampoco se recrea `.venv`, se generan
claves o se ejecuta `pip install`.

```powershell
if (-not (Test-Path -LiteralPath '.env' -PathType Leaf)) {
    throw 'Falta el .env privado existente. No copies .env.example sobre configuracion de produccion.'
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe' -PathType Leaf)) {
    throw 'Falta el Python del entorno virtual existente.'
}

$remotoIoT = git remote get-url origin
if ($LASTEXITCODE -ne 0 -or $remotoIoT -notin @(
    'https://github.com/Jack28-08/api-vehiculo-iot.git',
    'git@github.com:Jack28-08/api-vehiculo-iot.git'
)) {
    throw 'El remoto origin no coincide con el repositorio esperado. Revisalo sin publicar credenciales.'
}
$ramaIoT = git branch --show-current
if ($LASTEXITCODE -ne 0 -or $ramaIoT -cne 'master') {
    throw 'La copia de EC2 debe estar en master; revisa su rama antes de continuar.'
}
$cambiosIoT = @(git status --porcelain=v1 --untracked-files=normal)
if ($LASTEXITCODE -ne 0 -or $cambiosIoT.Count -ne 0) {
    throw 'Hay cambios locales o Git fallo. Revisa git status; no descartes trabajo local.'
}
git ls-files --error-unmatch -- .env *> $null
if ($LASTEXITCODE -eq 0) {
    throw 'El .env aparece versionado. Detente y revisa su proteccion sin mostrarlo.'
}
git check-ignore -q -- .env
if ($LASTEXITCODE -ne 0) {
    throw 'El .env no esta excluido de Git. Detente y revisa la configuracion.'
}

$revisionAnteriorIoT = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'No se pudo identificar la revision actual.' }
git pull --ff-only origin master
if ($LASTEXITCODE -ne 0) {
    throw 'Git no pudo actualizar por fast-forward. Conserva y revisa el estado local.'
}
git diff --quiet $revisionAnteriorIoT HEAD -- requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw 'Tambien cambiaron las dependencias. Revisa ese alcance antes de reiniciar; no se instalaron paquetes.'
}
```

## 4. Ejecutar pruebas aisladas y reiniciar el nombre literal de la API

La suite utiliza dobles locales y no necesita RDS. Si falla, conserva la salida
local y no ejecutes el reinicio. El código actualizado todavía no está cargado
en el proceso que ya estaba ejecutándose. No publiques salidas que contengan
configuración privada.

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw 'Fallaron las pruebas aisladas. No se reinicio la API.'
}

# Revalidar: un dependiente pudo haber arrancado mientras se ejecutaban las pruebas.
Assert-ApiOnlyRestartIoT
Restart-Service -Name 'ApiVehiculoIoT' -ErrorAction Stop
$servicioIoT = Get-Service -Name 'ApiVehiculoIoT'
$servicioIoT.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(90))
Get-Service -Name 'ApiVehiculoIoT' | Select-Object Name, Status
```

Se usa únicamente `ApiVehiculoIoT`, sin comodines ni `-Force`. Si Windows
rechaza el reinicio por una dependencia, detente y conserva el estado; no
repitas con opciones que afecten al proxy. No reinicies EC2 ni RDS, ejecutes
SQL, cambies AWS/firewall, o ejecutes los instaladores de API/Caddy para este
cambio de CORS.

## 5. Comprobar salud y CORS con GET y OPTIONS

Ejecuta el bloque primero en EC2. Hace GET a `/health`, que no abre conexión a
RDS, y preflight OPTIONS a `/vehiculos`, interceptado por el middleware CORS.
Aunque el preflight declara `POST`, **no envía un POST real** ni necesita una
clave de API. No uses `/health/db` ni los verificadores de escritura.

```powershell
function Test-CorsIoT {
    param([Parameter(Mandatory = $true)][string]$BaseUrl)

    $saludIoT = Invoke-RestMethod -Method Get -Uri "$BaseUrl/health" -TimeoutSec 15
    if ($saludIoT.status -ne 'ok') { throw 'La API no devolvio salud correcta.' }

    foreach ($origenIoT in @('https://jack28-08.github.io', 'http://localhost:5500')) {
        $getIoT = Invoke-WebRequest -UseBasicParsing -Method Get -Uri "$BaseUrl/health" -Headers @{
            Origin = $origenIoT
        } -TimeoutSec 15
        if ($getIoT.StatusCode -ne 200 -or $getIoT.Headers['Access-Control-Allow-Origin'] -cne $origenIoT) {
            throw "GET sin permiso CORS para $origenIoT"
        }

        foreach ($metodoIoT in @('GET', 'POST', 'OPTIONS')) {
            $preflightIoT = Invoke-WebRequest -UseBasicParsing -Method Options -Uri "$BaseUrl/vehiculos" -Headers @{
                Origin = $origenIoT
                'Access-Control-Request-Method' = $metodoIoT
                'Access-Control-Request-Headers' = 'X-API-Key, Content-Type, Accept'
            } -TimeoutSec 15
            if ($preflightIoT.StatusCode -ne 200 -or $preflightIoT.Headers['Access-Control-Allow-Origin'] -cne $origenIoT) {
                throw "Preflight sin permiso CORS para $origenIoT y $metodoIoT"
            }
            $metodosIoT = @($preflightIoT.Headers['Access-Control-Allow-Methods'] -split ',' | ForEach-Object { $_.Trim() })
            foreach ($requeridoIoT in @('GET', 'POST', 'OPTIONS')) {
                if ($requeridoIoT -notin $metodosIoT) { throw "Falta metodo CORS: $requeridoIoT" }
            }
            $cabecerasIoT = @($preflightIoT.Headers['Access-Control-Allow-Headers'] -split ',' | ForEach-Object { $_.Trim() })
            foreach ($requeridaIoT in @('X-API-Key', 'Content-Type', 'Accept')) {
                if ($requeridaIoT -notin $cabecerasIoT) { throw "Falta encabezado CORS: $requeridaIoT" }
            }
            [pscustomobject]@{ Base = $BaseUrl; Origin = $origenIoT; Preflight = $metodoIoT; Status = $preflightIoT.StatusCode }
        }
    }
}

Test-CorsIoT -BaseUrl 'http://127.0.0.1:8000'
Test-CorsIoT -BaseUrl 'https://184-72-72-255.sslip.io'
```

Para comprobar también la ruta pública desde tu PC, copia únicamente la
definición de `Test-CorsIoT` en PowerShell de esa PC y ejecuta
`Test-CorsIoT -BaseUrl 'https://184-72-72-255.sslip.io'`. Mantén la validación TLS
normal. Si loopback funciona y HTTPS falla, conserva el error y revisa la
operación del proxy por separado; no cambies Caddy en este procedimiento.

Finalmente recarga el frontend y revisa su GET en la pestaña Network. Las
pruebas anteriores verifican salud y encabezados; no acreditan disponibilidad
de RDS ni ejecución de controles, DEMO o movimientos. Este documento contiene
instrucciones: su existencia no demuestra un despliegue real en EC2.
