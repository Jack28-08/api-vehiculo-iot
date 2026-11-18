# Protocolo de control continuo NODE/01

El vehículo 1 puede recibir una consigna efímera de dos ejes por un canal de equipo autenticado. La API conserva en memoria el permiso vigente, con caducidad de 750 ms y renovación recomendada cada 200 ms. Las renovaciones, los latidos y las liberaciones no crean registros ni llaman a RDS. El proceso usa un worker de Uvicorn, igual que la configuración WinSW de este repositorio. Varios workers o servidores requerirían coordinación compartida antes de habilitar el canal.

## Contrato HTTP

Las rutas se encuentran bajo `/vehiculos/1/control`. El navegador consulta la capacidad por GET. Los POST requieren `X-API-Key` con el rol de operador o administrador. El token del dispositivo nunca se entrega al navegador. La ventana de evaluación conserva el alcance anterior para movimientos, inicio y cancelación de DEMO y DETENER.

| Ruta | Cuerpo de solicitud | Resultado |
| --- | --- | --- |
| `GET /capacidad` | — | `habilitado`, `disponible`, `diagonales`, `demo_remota`, `barrera`, `ttl_ms`, `renovar_ms` |
| `POST /permiso` | `barrera`, `gesto`, `secuencia: 1`, `longitudinal`, `lateral` | `barrera`, `gesto`, `secuencia`, `ttl_ms`. El permiso comienza en neutral. |
| `POST /vector` | `barrera`, `gesto`, `secuencia`, `longitudinal`, `lateral` | Mismo formato que el permiso. Actualiza la consigna y su vencimiento. |
| `POST /liberar` | `barrera`, `gesto` | `liberado: true`, `barrera`. Descarta el permiso y gira la barrera. |

La barrera es un UUID opaco del servidor. Es `null` si el control continuo está deshabilitado. El cliente crea un UUID de gesto nuevo para cada interacción y envía secuencias enteras crecientes. Cada eje toma los valores `-1`, `0` o `1`. El eje longitudinal positivo representa avanzar y el lateral positivo representa girar a la derecha. Para volver a neutral se usa liberar. La API rechaza dos ejes activos si las diagonales están deshabilitadas.

Un gesto anterior queda invalidado al detener, liberar, vencer o reiniciar el canal de equipo. Liberar también invalida la barrera si aún no llegó la adquisición. Liberar con una barrera anterior es idempotente y deja intacto cualquier permiso nuevo. El servidor rechaza con 409 vectores de otro gesto, repetidos, fuera de orden o con barrera vencida. La adquisición requiere un latido de equipo reciente y responde 503 cuando se pierde el canal.

## Canal del dispositivo

El equipo conecta al endpoint privado `/ws/dispositivos/1` con `Authorization: Bearer <credencial del equipo>`. Usa TLS validado de extremo a extremo. El monitor público `/ws/vehiculos/1` conserva el protocolo previo de lectura.

El equipo envía `{"tipo":"latido","reto":1}` con un reto entero positivo de 32 bits que aumenta durante la conexión. Cada latido válido recibe una única respuesta con este formato:

```json
{"tipo":"control","reto":1,"secuencia":1,"vigencia_ms":0,"longitudinal":0,"lateral":0,"codigo":0,"pulso":0}
```

La secuencia de respuesta aumenta durante el socket y es independiente de las secuencias HTTP. Un permiso activo envía ejes, tiempo restante y `codigo` y `pulso` iguales a cero. Un pulso envía ejes cero, el código de catálogo entre 1 y 8 y un `pulso` positivo creciente en el socket. Los latidos del mismo pulso conservan ese identificador. La vigencia máxima es 750 ms y el servidor nunca la calcula más allá del fin programado del pulso. En neutral los ejes, `codigo`, `pulso` y `vigencia_ms` son cero.

Una conexión nueva invalida permiso y pulso y espera un latido antes de informar `disponible`. Un latido inválido, obsoleto o ausente cierra el canal. El firmware valida reto, secuencia, antigüedad y pulso, neutraliza al vencer el plazo o perder conexión y evita reactivar un pulso terminado aunque llegue en una respuesta nueva. `disponible` acredita un latido autenticado reciente. Si el equipo no recibe un pulso breve antes de vencer, no lo ejecuta en una reconexión. La actuación física exige verificación aparte.

## DETENER y operaciones existentes

`POST /vehiculos/1/detener` invalida permiso y pulso antes de esperar la cancelación de DEMO o el snapshot de RDS. El siguiente latido procesado después de invalidar recibe neutral. Una respuesta ya enviada puede estar en tránsito. El plazo local del firmware limita el efecto si falla la entrega de neutral. El HTTP 200 conserva el significado de detención lógica.

Cuando el canal está habilitado, `/vehiculos/1/movimientos` y `/vehiculos/1/demos/{id}/iniciar` requieren `X-IoT-Barrier` con la barrera vigente de capacidad. Una barrera ausente o vieja devuelve 409 antes de consultar RDS. La API vuelve a comprobarla al escribir y al entregar el pulso. Los clientes autorizados en los dos orígenes CORS existentes pueden solicitar ese encabezado. Las rutas conservan cuerpos, respuestas y permisos previos.

El movimiento individual usa el código confirmado del catálogo. Las duraciones nominales de los códigos 1 a 8 son 700, 900, 250, 1000, 900, 350, 1000 y 700 ms. Cada paso DEMO toma su duración y pausa del plan obtenido de RDS. La pausa deja la consigna en neutral y el paso siguiente lleva otro identificador de pulso. Inicio, cancelación, fin y error conservan sus eventos públicos. La cancelación invalida el pulso antes de esperar una escritura en curso. Una DEMO con permiso perdido termina sin reiniciar pasos en una sesión nueva. Cambiar el catálogo invalida solicitudes con su barrera anterior.

La coordinación por vehículo impide que una DEMO, un pulso o un permiso de joystick válido compitan por el canal. Se conserva una fila de historial por movimiento individual confirmado y ninguna por paso DEMO. Los GET y los 21 procedimientos almacenados mantienen su contrato. Si DETENER llega mientras un commit está en proceso, ese commit puede quedar en estado e historial, pero su pulso queda invalidado.

## Configuración local y pruebas

La configuración se carga de forma diferida desde el entorno del proceso y el `.env` privado que ya utiliza el backend. `IOT_CONTINUOUS_ENABLED`, `IOT_CONTINUOUS_DIAGONALS` e `IOT_CONTINUOUS_REMOTE_DEMO` tienen valor inicial `false`. La última opción habilita el código 6, que inicia una DEMO autónoma del equipo y necesita confirmar que DETENER puede interrumpirla. Por defecto se rechaza el código 6 antes de registrar un movimiento y se rechaza el inicio de un plan que lo incluya. `IOT_CONTINUOUS_DEVICE_TOKEN` es una credencial independiente, vacía por defecto. El servidor exige entre 32 y 256 caracteres ASCII sin espacios. El proceso debe reiniciarse para volver a cargar cambios. Conservar la credencial exclusivamente en el entorno privado y el equipo aprovisionado.

Ejecutar `python -m unittest discover -s tests -v` desde el entorno virtual. Los escenarios usan transporte ASGI en memoria, dobles y reloj controlado. Las capacidades quedan desactivadas por defecto. Quedan pendientes el despliegue del firmware y del backend y la prueba física supervisada.
