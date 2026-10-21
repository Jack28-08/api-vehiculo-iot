# Operación 2.6 en EC2 Windows

Destino: Windows Server 2025 x64. Proyecto: `C:\IoT\api-vehiculo-iot`.
Estos comandos se ejecutan **dentro de EC2**, en Windows PowerShell elevado.
Los instaladores rechazan una ejecución sin `-InstallOnThisServer` y Windows de escritorio.
No instalarlos en la PC de desarrollo. No hay un canal remoto configurado por estos scripts.

El hostname elegido por el alumno es `184-72-72-255.sslip.io`. El 20 de octubre
de 2026, una consulta DNS desde la PC devolvió `184.72.72.255`, su IP elástica.
**No se ha comprobado todavía un certificado ni el acceso público HTTPS/WSS.**
Las direcciones objetivo son:

- Swagger: `https://184-72-72-255.sslip.io/docs`.
- Salud: `https://184-72-72-255.sslip.io/health` y `/health/db`.
- Monitor: `wss://184-72-72-255.sslip.io/ws/vehiculos/{id_vehiculo}`.

## 1. Actualizar conservando configuración y trabajo local

Primero detener Uvicorn manual con **Ctrl+C** en su terminal. No matar procesos
desconocidos ni mantener a la vez Uvicorn manual y el servicio.
Si ya existe el servicio, detenerlo antes de actualizar dependencias:

```powershell
Set-Location -LiteralPath C:\IoT\api-vehiculo-iot
if (Get-Service CaddyVehiculoIoT -ErrorAction SilentlyContinue) {
    Stop-Service CaddyVehiculoIoT
}
if (Get-Service ApiVehiculoIoT -ErrorAction SilentlyContinue) {
    Stop-Service ApiVehiculoIoT
}
git status --short
git remote -v
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Git no pudo actualizar; conserva y revisa los cambios locales.' }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'No se completaron las dependencias.' }
```

El remoto debe ser `https://github.com/Jack28-08/api-vehiculo-iot.git`. `--ff-only`
rechaza historias divergentes; no usa reset, clean, force ni reemplaza cambios en conflicto.
Si Git se detiene por un cambio local, revisarlo; no descartarlo automáticamente.
`.env` y `.venv` permanecen locales. **No copiar `.env.example` sobre el `.env`
existente en EC2.** Las claves ya configuradas se conservan.

El entorno virtual utiliza Python base instalado en
`C:\Users\Administrator\AppData\Local\Programs\Python\Python314`.
El instalador obtiene la ruta real mediante `sys.base_prefix`; no supone
que copiar la carpeta `.venv` de otro equipo cree un entorno válido.

## 2. Validar e instalar la API

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\validate_windows_deploy.ps1
if ($LASTEXITCODE -ne 0) { throw 'Fallo la validacion de scripts.' }
.\deploy\windows\Install-ApiService.ps1 -InstallOnThisServer
```

El servicio `ApiVehiculoIoT` usa WinSW 2.12.0 con hash fijo, `LocalService`,
un worker Uvicorn, sin reload, `127.0.0.1:8000`, inicio automático y recuperación.
El XML utiliza `serviceaccount/domain/user` de WinSW v2. El instalador aplica
lectura al proyecto y al Python base, permisos privados al `.env` y escritura
solo en los directorios operativos necesarios. Las copias de ACL/configuración
y los logs quedan fuera del repositorio, en `C:\IoT\servicios\api`.

La comprobación de `/health/db` en este paso **sí conecta con RDS**. Requiere
que RDS esté disponible y que su grupo de seguridad permita MySQL desde el
grupo de EC2. Nunca abrir 3306 al mundo. Este paso no crea tablas ni ejecuta
procedimientos de escritura.

Si el servicio falla, revisar la cuenta, permisos de recorrido al Python base,
dependencias, `.env` y logs locales; no pegar secretos ni trazas completas en
capturas. Las pruebas de parser no acreditan que `LocalService` ya haya
ejecutado correctamente el servicio en EC2.

## 3. Instalar Caddy y habilitar HTTPS/WSS

```powershell
$hostnameIoT = '184-72-72-255.sslip.io'
Resolve-DnsName -Name $hostnameIoT -Type A
.\deploy\windows\Install-CaddyService.ps1 -Hostname $hostnameIoT -InstallOnThisServer -OpenFirewall
```

El instalador verifica la integridad de binarios oficiales y valida la
configuración antes de arrancar el servicio `CaddyVehiculoIoT`. El proxy
envía HTTP y WebSocket a `127.0.0.1:8000`. Usa almacenamiento persistente
fuera de Git para certificados y cuentas ACME, cuenta `LocalService`, logs,
inicio automático y recuperación. Los encabezados de solicitud se eliminan
de los logs configurados para evitar registrar `X-API-Key`.

`-OpenFirewall` prepara únicamente TCP 80/443 en el firewall **de EC2**.
No cambia grupos de seguridad de AWS ni RDP. Confirmar en AWS que TCP80/443
llegan a esta instancia y que no hay otra aplicación ocupándolos. Caddy
necesita acceso saliente a DNS y HTTPS para solicitar y renovar certificados.
No desactivar la validación TLS del cliente ni usar `tls internal` como
sustituto de un certificado público.

El servicio DNS compartido admite certificados para nombres con la IP, pero
las cuotas ACME, el DNS y el acceso entrante pueden impedir la emisión. No
repetir instalaciones ciegamente ante un límite de emisión. El almacenamiento
persistente de Caddy debe conservarse al actualizar; no borrar sus claves o
cuentas. Consultar los logs locales y corregir la causa.

Después de comprobar HTTPS desde fuera de EC2, retirar **solo la antigua regla
entrante pública de TCP8000** del grupo de seguridad de EC2; no tocar RDP ni
las reglas de MySQL entre grupos. Uvicorn debe seguir escuchando únicamente
en loopback. No hace falta exponer 8000 para Caddy.

## 4. Comprobaciones públicas y datos reales

Desde un equipo distinto de EC2, sin desactivar TLS:

```powershell
$baseIoT = 'https://184-72-72-255.sslip.io'
Invoke-RestMethod "$baseIoT/health"
Invoke-RestMethod "$baseIoT/health/db"
(Invoke-WebRequest "$baseIoT/docs" -UseBasicParsing).StatusCode
Invoke-RestMethod "$baseIoT/vehiculos"
```

Seleccionar vehículo activo, comando activo que le pertenezca, DEMO del mismo
vehículo con pasos válidos y otro vehículo distinto. Consultar sus GET: no
reutilizar IDs antiguos sin comprobarlos. Las rutas son las mismas por HTTPS;
Swagger envía `X-API-Key` al utilizar **Authorize**. GET y WSS son públicos.
Operador y administrador ejecutan movimientos y controlan DEMO; solo el
administrador gestiona y elimina recursos.

Los verificadores Python reciben el destino y escriben archivos **nuevos**;
no sobrescriben las evidencias de 2.5. Consultar `--help` antes de ejecutarlos:

```powershell
.\.venv\Scripts\python.exe -m scripts.check_http_reads --help
.\.venv\Scripts\python.exe -m scripts.check_websocket_reads --help
.\.venv\Scripts\python.exe -m scripts.verify_public --help
```

Las verificaciones de escritura requieren habilitación expresa y confirmación
del mismo destino. El caso individual agrega una fila real al historial;
los casos DEMO cambian el estado sin insertar historial. No ejecutar a la vez
otros clientes que escriban en ese vehículo. Para demostrar que una DEMO no
cambia el historial, comparar todas sus filas y `COUNT(*)` mediante una
conexión RDS de solo lectura: el GET máximo de diez no basta por sí solo.
Ejecutar esa lectura desde EC2 evita ampliar el grupo de seguridad de RDS.

Los resultados Python son evidencia técnica Python. Las **tres pruebas de
Thunder Client** y sus capturas se preparan en [la guía de Thunder](thunder_client.md).
No presentar resultados locales, capturas antiguas ni una licencia no
comprobada como cumplimiento de ese requisito.

### Comandos de verificadores para después de instalar

Desde un cliente con este proyecto y sus dependencias, las dos primeras
órdenes siguientes solo leen la API pública. Generan nombres nuevos y no
necesitan acceso directo a MySQL:

```powershell
$baseIoT = 'https://184-72-72-255.sslip.io'
$evidenciaIoT = Join-Path $PWD ('evidencias\publica-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $evidenciaIoT -ErrorAction Stop | Out-Null
.\.venv\Scripts\python.exe -m scripts.check_http_reads --base-url $baseIoT --output "$evidenciaIoT\get.json"
.\.venv\Scripts\python.exe -m scripts.check_websocket_reads --base-url $baseIoT --output "$evidenciaIoT\wss.json"
```

El escenario completo **sí hace escrituras reales**: un POST individual, una
DEMO completa, un segundo inicio que debe rechazarse con `409` y otra DEMO
que se cancela. Son cinco POST previstos; no es una repetición automática de
la prueba anterior. El individual incrementa el historial en una fila. El
snapshot de la DEMO se toma después, y debe conservarse íntegro también al
cancelar. El verificador no repite POST ante timeout. Si el segundo inicio
llega cuando la primera DEMO ya acabó y es aceptado por una carrera, conserva
el UUID inesperado, falla y exige revisar el estado antes de otra prueba.
No modifica los pasos de una DEMO para prolongarla artificialmente.

Para esta comprobación, ejecutar **dentro de EC2** con su `.env` privado y
acceso RDS existente. Elegir los cuatro IDs reales obtenidos por GET:

```powershell
$idVehiculoIoT = [int](Read-Host 'ID de vehiculo activo de esta practica')
$idOtroVehiculoIoT = [int](Read-Host 'ID de otro vehiculo existente para aislamiento')
$idComandoIoT = [int](Read-Host 'ID de comando activo del primer vehiculo')
$idDemoIoT = [int](Read-Host 'ID de DEMO activa del primer vehiculo')
.\.venv\Scripts\python.exe -m scripts.verify_public --scenario all --base-url $baseIoT --vehicle-id $idVehiculoIoT --other-vehicle-id $idOtroVehiculoIoT --command-id $idComandoIoT --demo-id $idDemoIoT --output "$evidenciaIoT\controles.json" --allow-writes --confirm-target $baseIoT --rds-readback
```

Usa dos WSS del vehículo y un tercero del otro, compara los pasos reales y
las filas completas por RDS, verifica reconexión y observa tres segundos
sin pasos tras cancelar (configurable con `--quiet-seconds`). Esa ventana
finita no acredita un tiempo infinito sin mensajes. La duración/pausa se
contrasta como metadatos y espera mínima combinada; no hay eventos separados
para medir la frontera entre ambas ni actuación de motores. La tolerancia de
recepción de 100 ms queda explícita en la evidencia.
Si se ejecuta desde EC2, usar una URL pública no demuestra por sí solo origen
externo: conservar también las comprobaciones desde la PC y Thunder Client.

## 5. Persistencia, actualización y recuperación

```powershell
Get-Service ApiVehiculoIoT, CaddyVehiculoIoT
Get-NetTCPConnection -State Listen -LocalPort 8000,80,443 |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

Para acreditar independencia de la sesión: cerrar RDP y VS Code, consultar
HTTPS/WSS desde fuera y conservar evidencia. Para acreditar arranque automático,
realizar un reinicio planificado de EC2 y repetir ambas comprobaciones. Estas
operaciones no se ejecutaron desde la PC de preparación. No reiniciar durante
una DEMO ni confundir la reconexión del cliente con reanudación de tareas.

Tras un cierre forzado o reinicio, sockets y tareas en memoria se pierden.
El cliente se reconecta y recibe `estado_inicial` desde el último estado
confirmado en RDS; la DEMO anterior **no se reanuda**. No iniciar otro worker
para escalar: reservas y monitores pertenecen a un proceso. Reiniciar la API
después de cambiar claves porque la configuración se almacena en caché.

Para actualizar otra vez: revisar trabajo local, detener la API, actualizar
por fast-forward, instalar dependencias y volver a ejecutar su instalador.
Si existe Caddy, detenerlo primero porque depende del servicio API y volver
a iniciarlo con `Start-Service CaddyVehiculoIoT` cuando la API esté comprobada.
Reconfigurar Caddy solo si cambia su configuración o la versión fijada;
conservar siempre su almacenamiento persistente. Si falla una actualización,
conservar logs y configuración anterior; no usar force push ni borrar datos.

## Material para el examen y límites reales

- [Matriz de los 24 requisitos](examen_2_6.md): distingue evidencia previa, preparación y pendientes.
- [Guía de estudio de las 21 correspondencias SP/API](guia_estudio_21_sp.md).
- [Contrato](contrato_rds.md), [mapa HTTP/SP](mapa_endpoints_sp.md) y [eventos JSON](websocket.md).
- [Validación de preparación](verificacion_preparacion_2_6.json): resultados locales, sin inventar pruebas públicas.

Las capturas reportadas de EC2 prueban, según el alumno, conectividad a RDS,
seis tablas/21SP y API manual con health correctos. La instalación efectiva
de los servicios, ACL bajo `LocalService`, emisión del certificado, firewall,
WSS público, persistencia tras reinicio, Thunder Client y reporte Word/PDF
requieren ejecución y evidencias nuevas. No se exige hardware físico:
un ESP podría suscribirse al WSS de su vehículo, validar el JSON y procesar
sus eventos; `conectado` no demuestra una conexión física por una petición HTTP.

Fuentes oficiales consultadas el 20 de octubre de 2026:
[Caddy HTTPS](https://caddyserver.com/docs/automatic-https),
[proxy y WebSocket](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
[servicio Windows](https://caddyserver.com/docs/running#windows-service),
[DNS y TLS de sslip.io/nip.io](https://nip.io/),
[retos ACME](https://letsencrypt.org/docs/challenge-types/).
