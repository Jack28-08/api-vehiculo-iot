# Protocolo de control continuo NODE/01

El vehículo 1 puede recibir una consigna efímera de dos ejes por un canal de equipo autenticado. La API conserva en memoria el permiso vigente, con caducidad de 750 ms y renovación recomendada cada 200 ms. Las renovaciones, los latidos y las liberaciones no crean registros ni llaman a RDS. El proceso usa un worker de Uvicorn, igual que la configuración WinSW de este repositorio. Varios workers o servidores requerirían coordinación compartida antes de habilitar el canal.

## Contrato HTTP

Las rutas se encuentran bajo `/vehiculos/1/control`. El navegador consulta la capacidad por GET. Los POST requieren `X-API-Key` con el rol de operador o administrador. El token del dispositivo nunca se entrega al navegador. La ventana de evaluación conserva el alcance anterior para movimientos, inicio y cancelación de DEMO y DETENER.

| Ruta | Cuerpo de solicitud | Resultado |
| --- | --- | --- |
| `GET /capacidad` | — | `habilitado`, `disponible`, `diagonales`, `barrera`, `ttl_ms`, `renovar_ms` |
| `POST /permiso` | `barrera`, `gesto`, `secuencia: 1`, `longitudinal`, `lateral` | `barrera`, `gesto`, `secuencia`, `ttl_ms`. El permiso comienza en neutral. |
| `POST /vector` | `barrera`, `gesto`, `secuencia`, `longitudinal`, `lateral` | Mismo formato que el permiso. Actualiza la consigna y su vencimiento. |
| `POST /liberar` | `barrera`, `gesto` | `liberado: true`, `barrera`. Descarta el permiso y gira la barrera. |

La barrera es un UUID opaco del servidor. Es `null` si el control continuo está deshabilitado. El cliente crea un UUID de gesto nuevo para cada interacción y envía secuencias enteras crecientes. Cada eje toma los valores `-1`, `0` o `1`. El eje longitudinal positivo representa avanzar y el lateral positivo representa girar a la derecha. Para volver a neutral se usa liberar. La API rechaza dos ejes activos si las diagonales están deshabilitadas.

Un gesto anterior queda invalidado al detener, liberar, vencer o reiniciar el canal de equipo. Liberar también invalida la barrera si aún no llegó la adquisición. Liberar con una barrera anterior es idempotente y deja intacto cualquier permiso nuevo. El servidor rechaza con 409 vectores de otro gesto, repetidos, fuera de orden o con barrera vencida. La adquisición requiere un latido de equipo reciente y responde 503 cuando se pierde el canal.

## Canal del dispositivo

El equipo conecta al endpoint privado `/ws/dispositivos/1` con `Authorization: Bearer <credencial del equipo>`. Usa TLS validado de extremo a extremo. El monitor público `/ws/vehiculos/1` conserva el protocolo previo de lectura.

El equipo envía `{"tipo":"latido","reto":1}` con un reto entero positivo de 32 bits que aumenta durante la conexión. Cada latido válido recibe una única respuesta con este formato:

```json
{"tipo":"control","reto":1,"secuencia":1,"vigencia_ms":0,"longitudinal":0,"lateral":0}
```

La secuencia de respuesta aumenta durante el socket y es independiente de las secuencias HTTP. Si existe un permiso activo, la respuesta contiene los ejes vigentes y su tiempo restante. Si la consigna es neutral, ambos ejes y la vigencia son cero. No se reintentan consignas anteriores. Una conexión nueva invalida el permiso anterior y espera un latido antes de informar `disponible`. Un latido inválido, obsoleto o ausente cierra el canal. La presencia que muestra `disponible` procede del latido autenticado. El firmware debe validar reto, secuencia y antigüedad, y neutralizar al vencer el plazo o perder conexión. La respuesta prueba el intercambio de protocolo y la actuación física exige verificación aparte.

## DETENER y operaciones existentes

`POST /vehiculos/1/detener` invalida el permiso antes de esperar la cancelación de DEMO o el snapshot de RDS. El siguiente latido procesado después de invalidar recibe neutral. Una respuesta ya enviada puede estar en tránsito. El plazo local del firmware limita el efecto si falla la entrega de neutral. El HTTP 200 conserva el significado de detención lógica.

DEMO y las mutaciones existentes conservan la coordinación por vehículo. Una DEMO o escritura activa impide adquirir un permiso. Un permiso vigente impide iniciar DEMO y ejecutar mutaciones legacy para ese vehículo. Los GET, el historial y los 21 procedimientos almacenados mantienen su contrato. Esta implementación conserva la ejecución lógica de DEMO y de los mandos auxiliares. Su actuación física a través del nuevo canal queda pendiente del firmware activo y de una validación en banco.

## Configuración local y pruebas

La configuración se carga de forma diferida desde el entorno del proceso y el `.env` privado que ya utiliza el backend. `IOT_CONTINUOUS_ENABLED` e `IOT_CONTINUOUS_DIAGONALS` tienen valor inicial `false`. `IOT_CONTINUOUS_DEVICE_TOKEN` es una credencial independiente, vacía por defecto. El servidor exige entre 32 y 256 caracteres ASCII sin espacios. El proceso debe reiniciarse para volver a cargar cambios. Conservar la credencial exclusivamente en el entorno privado y el equipo aprovisionado.

Ejecutar `python -m unittest discover -s tests -v` desde el entorno virtual. Los escenarios de control continuo usan transporte ASGI en memoria, dobles y reloj controlado. La rama deja las dos capacidades desactivadas por defecto y no acredita un despliegue ni una prueba física.
