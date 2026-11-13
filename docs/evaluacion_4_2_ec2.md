# Ventana de evaluación 4.2 en EC2

La aplicación publicada sigue en [GitHub Pages](https://jack28-08.github.io/control-iot-jack/).
La actualización agrega un único GET de sesión a FastAPI. No cambia Caddy,
RDS, las tablas, los SP ni el contrato de las cuatro operaciones existentes.
El modo está **cerrado por defecto**: desplegar código no lo habilita.

## Qué se habilita

Mientras existe una ventana vigente, `GET /evaluacion/vehiculos/1` emite un
token opaco y su fecha `expires_at`. NODE/01 lo guarda solo en memoria y
envía `Authorization: Bearer` a los cuatro POST existentes del vehículo 1:
movimiento individual, inicio DEMO, cancelación DEMO y detención lógica.
Los tokens duran hasta diez minutos y nunca sobreviven al fin de la ventana,
a un cambio de configuración observado o a un reinicio. Las rutas de
administración, otros vehículos y las claves permanentes se mantienen
separados. El origen autorizado es `https://jack28-08.github.io`; una
herramienta externa puede falsificar Origin. **La ventana es acceso público
acotado en tiempo, no identificación personal del evaluador.** CORS no
autentica. Usar una ventana corta y supervisada.

Hay cupos móviles por minuto, por IP y globales (emisión 8/48, movimiento
45/120, inicio 4/12, detención 30/90 y cancelación 24/72) y como máximo
256 sesiones en memoria. DETENER y cancelar tienen presupuestos propios:
saturar movimientos no consume sus cupos. Un 429 no se reintenta como
movimiento. El navegador conserva la prioridad de envío de DETENER, sin
garantizar recepción de red ni actuación física. La API usa un solo proceso;
la IP se obtiene del cliente ASGI con Uvicorn confiando exclusivamente en
el proxy Caddy de loopback. No exponer 8000 ni confiar en proxys arbitrarios.

## Actualizar la API, todavía sin abrir evaluación

Dentro de EC2, en PowerShell elevado. Si el servidor usa Uvicorn manual,
detenerlo con Ctrl+C primero; los comandos siguientes son para la instalación
con ambos servicios existentes. Revisar el estado y hacer la actualización
sin DEMO activa. No sustituir ni imprimir `.env`.

```powershell
$ErrorActionPreference = 'Stop'
Set-Location C:\IoT\api-vehiculo-iot
git status --short
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'No se pudo actualizar la referencia remota' }
git merge --ff-only origin/codex/evaluacion-iot-4-2
if ($LASTEXITCODE -ne 0) { throw 'Detenerse y revisar la rama o los cambios locales' }
Stop-Service CaddyVehiculoIoT
Stop-Service ApiVehiculoIoT
Start-Service ApiVehiculoIoT
Start-Service CaddyVehiculoIoT
Invoke-RestMethod http://127.0.0.1:8000/health
```

Desde fuera de EC2, verificar certificado y el estado independiente de
[`/health`](https://184-72-72-255.sslip.io/health) y
[`/health/db`](https://184-72-72-255.sslip.io/health/db), y abrir WSS
`wss://184-72-72-255.sslip.io/ws/vehiculos/1` sin mandar movimientos.
La comprobación local de los cuatro POST usa servicios simulados y no
sustituye esa verificación pública. Si HTTPS no responde, revisar primero
servicios, DNS y conectividad. Un timeout sin respuesta HTTP no diagnostica RDS.

## Abrir 20 minutos y cerrar

Solo en EC2, cuando ya esté verificado el sitio y comience la evaluación.
La ubicación está fuera de Git, dentro del directorio protegido del
servicio. No se escribe ninguna clave. El identificador de ventana cambia
en cada apertura.

```powershell
$windowPath42 = 'C:\IoT\servicios\api\evaluacion-4-2.json'
$windowStart42 = [DateTimeOffset]::UtcNow
[ordered]@{
    id = [Guid]::NewGuid().ToString()
    starts_at = $windowStart42.ToString('o')
    ends_at = $windowStart42.AddMinutes(20).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $windowPath42 -Encoding UTF8
```

Abrir GitHub Pages sin introducir clave. Comprobar que la barra indica acceso
de evaluación y que la petición de sesión recibe 200. **No incluir el cuerpo
de la sesión, encabezados de autorización ni claves en las capturas.** Los
escritos de control quedan reservados para el usuario durante la evaluación,
no para pruebas automáticas contra AWS.

Al terminar, detener o cancelar cualquier DEMO activa y luego cerrar:

```powershell
Remove-Item -LiteralPath 'C:\IoT\servicios\api\evaluacion-4-2.json'
```

La solicitud siguiente comprueba el archivo y devuelve 403 sin reiniciar.
Eliminar el archivo no revierte operaciones ya confirmadas ni cancela por
sí solo una DEMO en curso; si se cierra primero, usar la vía autorizada
normal del operador para detener. Ante error o configuración ilegible, la
ventana queda cerrada. La clave de operador existente sigue funcionando
fuera de la evaluación.
