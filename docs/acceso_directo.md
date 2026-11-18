# Acceso directo de NODE/01

GitHub Pages puede solicitar una sesión de control del vehículo 1 al abrir la aplicación. Guarda este ajuste en el entorno del servicio o en el `.env` privado, una vez validado el canal del dispositivo.

```dotenv
IOT_CONTINUOUS_ENABLED=true
IOT_DIRECT_ENABLED=true
```

FastAPI lee ambos ajustes al iniciar el uso en cada proceso. Se conserva el modo al reiniciar si la configuración sigue instalada. Para desactivarlo, establece `IOT_DIRECT_ENABLED=false` y reinicia FastAPI. Las sesiones se emiten y validan en un solo proceso. El reinicio borra los tokens en memoria y la aplicación obtiene otro automáticamente mientras el modo siga habilitado.

## Contrato

- `GET /evaluacion/vehiculos/1` responde con `token`, `expires_at` y `modo: "directo"`. El token opaco dura diez minutos y el servidor guarda su hash. El navegador lo mantiene en memoria y lo renueva antes de vencer.
- El Bearer admite movimientos, inicio y cancelación de DEMO, DETENER, y las rutas `control/permiso`, `control/vector` y `control/liberar`. El alcance se aplica por ruta y por vehículo. La clave de administrador sigue reservada a la gestión. El dispositivo se autentica con su propia credencial.
- Los tokens se vinculan al cliente y al origen de Pages. Cualquier persona que conozca la URL puede solicitar movimientos mientras la infraestructura esté disponible. Alimenta el vehículo únicamente durante su uso supervisado.
- El modo directo exige que el control gestionado esté configurado. El permiso físico sigue sujeto a la presencia del dispositivo, su barrera y su vencimiento breve. Las cuotas de movimientos, vectores, liberación y DETENER son independientes.
- Si se desactiva este modo, vuelve a aplicar el mecanismo temporal de evaluación en sus rutas originales cuando exista una ventana vigente.

## Comprobación

Tras desplegar ambos componentes, abre Pages en una ventana privada y en otro dispositivo. Confirma que la autorización aparece automáticamente y que se habilita el mando cuando el canal se encuentra disponible. Reinicia FastAPI durante la ventana de mantenimiento y verifica que ambas sesiones se recuperen. Realiza los ensayos de actuación en banco con las cargas aisladas.

Las pruebas locales ejecutan la aplicación ASGI con dobles. Cubren dos clientes, caducidad, reapertura, persistencia del ajuste en el archivo, pérdida de sesiones al reinstanciar el servicio y rechazo del Bearer en gestión, otros vehículos y el WSS del dispositivo.

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_evaluation -q
```
