# Servicios Windows de la API y del proxy HTTPS/WSS

Estos archivos se ejecutan **dentro de EC2 Windows Server**, en PowerShell
elevado. No deben instalarse desde la PC de desarrollo. Ambos instaladores
requieren `-InstallOnThisServer`, rechazan Windows de escritorio y comprueban
administrador antes de modificar archivos, ACL, servicios o firewall. El
parser local y las pruebas de plantillas no acreditan una instalación en EC2.

## Primera instalación

El proyecto debe existir en `C:\IoT\api-vehiculo-iot`, con `.venv`, las
dependencias fijadas y el `.env` privado ya preparados. No se genera ni se
muestra una contraseña o clave. Detén primero Uvicorn manual con **Ctrl+C**.
Comprueba que la API funcione con un solo worker y RDS accesible desde EC2.

```powershell
Set-Location 'C:\IoT\api-vehiculo-iot'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Install-ApiService.ps1 -InstallOnThisServer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Install-CaddyService.ps1 -Hostname '184-72-72-255.sslip.io' -InstallOnThisServer -OpenFirewall
```

`ExecutionPolicy Bypass` solo afecta esos procesos. El hostname del ejemplo
corresponde a la IP elástica indicada para esta actividad; la resolución DNS
se vuelve a comprobar en el servidor. Usar ese nombre no significa que ya
existan certificado, HTTPS o WSS funcionando. El instalador necesita registros
A que apunten exclusivamente a `-ExpectedPublicIPv4` (por defecto
`184.72.72.255`); rechaza AAAA porque este despliegue no configura IPv6.
Para otro dominio, pásalo con `-Hostname` y configura antes su DNS público.
No se utilizan wildcard, certificados internos ni omisión de validación TLS.

Los Security Groups de EC2 deben permitir TCP 80/443 desde los clientes.
`-OpenFirewall` es opcional: añade únicamente las reglas locales
`CaddyVehiculoIoT-TCP-80` y `CaddyVehiculoIoT-TCP-443`, acotadas a `caddy.exe`.
Una regla del mismo nombre pero distinta se conserva y causa error.
No modifica RDP, MySQL, Security Groups, ni abre 8000 al público. Una vez
comprobado el proxy, retira en AWS solo la regla pública antigua de 8000.
Conserva RDP restringido y MySQL permitido desde el SG de EC2.

## Qué instala y protege

| Componente | Configuración |
| --- | --- |
| `ApiVehiculoIoT` | WinSW 2.12.0, LocalService, inicio automático, recuperación a 5 s. Uvicorn en `127.0.0.1:8000`, un worker, sin reload. |
| API: `C:\IoT\servicios\api` | Wrapper `api-iot.exe`, XML y logs con rotación de 10 MiB y cinco archivos. |
| `CaddyVehiculoIoT` | WinSW 2.12.0 y Caddy 2.11.7, LocalService, inicio automático, recuperación a 5 s y dependencia SCM de la API. |
| Caddy: `C:\IoT\servicios\caddy` | `caddy.exe`, `caddy-service.exe`, XML y Caddyfile. `data` conserva certificados, claves TLS y cuentas ACME fuera de Git; `logs` conserva registros rotados. |

Los directorios de servicio admiten SYSTEM/Administrators con control total
y LocalService con lectura/ejecución; `data` y `logs` conceden modificación a
LocalService. `.env` conserva sus bytes y recibe ACL protegida: control para
SYSTEM/Administrators, lectura para LocalService. Se guarda su ACL anterior
en un archivo `env-acl-*.sddl` fuera del repositorio. No es una copia de su
contenido. Se conservan copias fechadas de XML y Caddyfile anteriores.

La API obtiene lectura/ejecución del proyecto y del Python base identificado
por `sys.base_prefix`, incluida la instalación del perfil de Administrator.
Los directorios ancestros reciben solo el permiso de atravesar, sin herencia
ni lectura del perfil completo. Políticas de denegación explícita o archivos
con ACL propia pueden impedir el arranque: se debe comprobar el servicio real
y, si hace falta, usar Python instalado para todos los usuarios. No se cambia
una política de dominio ni se conceden permisos de escritura al código.
LocalService es una identidad compartida: estos dos servicios no están
aislados entre sí. En un servidor compartido conviene una cuenta dedicada.

Caddy valida el Caddyfile antes de reemplazar la configuración y detener
el proxy anterior. `reverse_proxy 127.0.0.1:8000` realiza el upgrade WebSocket
automáticamente. El almacenamiento TLS es explícito y persistente, sin
depender del perfil de quien ejecute `validate`. El administrador de Caddy
está desactivado y no se usa `--resume`: los cambios se aplican reiniciando
el servicio desde su archivo de configuración.

Se eliminan encabezados completos, URI y encabezados de respuesta de los
logs HTTP **y de los logs de ejecución** de Caddy: `X-API-Key` no es un
encabezado que Caddy oculte automáticamente. Uvicorn usa `--no-access-log`.
Los logs conservan estado, método, tiempos y errores operativos, sin guardar
cuerpos ni claves API. No habilites `debug` ni `log_credentials` para capturas.

## Binarios oficiales e integridad

Los instaladores fijan versiones y SHA-256 en el código; no descargan
`latest` al instalar ni aceptan automáticamente un hash obtenido del mismo
archivo sin una referencia previa. Cada archivo se verifica antes de
ejecutarse. Un archivo existente con otro hash se conserva y causa error.
No se publica ningún EXE ni ZIP en Git.

| Asset | SHA-256 esperado |
| --- | --- |
| WinSW 2.12.0 `WinSW-x64.exe` | `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da` |
| Caddy 2.11.7 `caddy_2.11.7_windows_amd64.zip` | `0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06` |
| `caddy.exe` extraído del ZIP anterior | `5f93a9bd555f3aac9d33af2756ba14b3519d8cca4ffd55256bdaeab444c9db2a` |

El ZIP se contrastó con el digest SHA-256 del asset oficial de GitHub y se
recalculó localmente; también se calculó el hash de su ejecutable extraído.
El archivo oficial `checksums.txt` usa SHA-512. Esta automatización valida
los hashes fijados mediante HTTPS; **no afirma verificar la firma Sigstore**.
Cambiar versiones requiere revisar la publicación oficial, comprobar el nuevo
asset y actualizar deliberadamente los hashes. No se sustituye un binario
existente desconocido o en uso.

Fuentes oficiales consultadas el 20 de octubre de 2026:
[Caddy 2.11.7](https://github.com/caddyserver/caddy/releases/tag/v2.11.7),
[verificación de firmas](https://caddyserver.com/docs/signature-verification),
[WinSW 2.12.0](https://github.com/winsw/winsw/releases/tag/v2.12.0) y
[XML v2 de WinSW](https://github.com/winsw/winsw/blob/v2.12.0/doc/xmlConfigFile.md).
El XML usa `serviceaccount/domain/user`; no se usan comandos exclusivos de WinSW v3.

## Actualizar y operar

Actualiza Git con `pull --ff-only` y conserva `.env`; no uses `reset --hard`
ni recrees el entorno o las claves. Antes de actualizar la API, detén el
proxy dependiente. Los instaladores rechazan un ID de servicio con otro
ejecutable o cuenta y no terminan procesos ajenos que ocupen los puertos.

```powershell
Stop-Service CaddyVehiculoIoT
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Install-ApiService.ps1 -InstallOnThisServer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\Install-CaddyService.ps1 -Hostname '184-72-72-255.sslip.io' -InstallOnThisServer
Get-Service ApiVehiculoIoT,CaddyVehiculoIoT
```

WinSW v2 vuelve a leer el XML al iniciar. No hace falta desinstalar para
actualizar argumentos o Caddyfile; los valores de inicio y recuperación se
aplican de nuevo. Si una actualización falla después de detener un servicio,
queda detenido para revisar el error; no se promete rollback automático.
`Start-Service`, `Stop-Service` y `Restart-Service` permiten operar cada uno.
Un reinicio o actualización corta WSS; reconecta para obtener el último estado
persistido. No se reanudan DEMO ni conexiones que vivían en memoria.

## Comprobaciones pendientes dentro y fuera de EC2

El instalador de API exige `/health` y `/health/db` correctos y escucha solo
loopback. El de Caddy valida la configuración e inicia el servicio; la emisión
ACME es asíncrona, por lo que **no declara HTTPS listo**. Después hay que
comprobar desde otro equipo el certificado, la redirección HTTP a HTTPS,
Swagger, dos WSS y las pruebas públicas. TCP 80/443 deben ser alcanzables para
los desafíos HTTP-01/TLS-ALPN; DNS resuelto no acredita alcance ni emisión.
No repitas solicitudes de certificado ante un error persistente: corrige DNS,
conectividad o permisos y revisa límites de la CA.

Faltan evidencias reales de instalación bajo LocalService, salida de RDP y
reinicio de EC2 con servicios automáticos. La validación realizada en la PC
solo abarca parser Windows PowerShell 5.1, guardias, plantillas XML y Caddyfile
mediante `caddy adapt`/`validate`, sin iniciar servidores ni solicitar certificados.

Referencias de operación:
[servicios Windows](https://caddyserver.com/docs/running#windows-service),
[HTTPS automático](https://caddyserver.com/docs/automatic-https),
[proxy WebSocket](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy),
[filtros de logs](https://caddyserver.com/docs/caddyfile/directives/log),
[almacenamiento explícito](https://caddyserver.com/docs/caddyfile/options#storage) y
[permisos de atravesar](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/security-policy-settings/bypass-traverse-checking).
