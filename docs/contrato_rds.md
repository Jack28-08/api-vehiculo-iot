# Contrato del esquema RDS

Estado: **complete**. Inspección UTC: 2026-10-16T04:32:46.768674+00:00.

Inspección de solo lectura. No se consultaron filas de negocio ni se ejecutaron procedimientos. Se omiten configuración de conexión, credenciales y cuentas DEFINER.

Esquema inspeccionado: vehiculo_iot.

Tablas: **6** (esperadas: 6). Procedimientos: **21** (esperados: 21). Vistas: 0.

Objetos visibles para la cuenta utilizada; no acredita acceso global.

## BASE TABLE: comandos

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_comando | int | NO | — | auto_increment |
| 2 | id_vehiculo | int | NO | — |  |
| 3 | nombre | varchar(60) | NO | — |  |
| 4 | codigo | int | NO | — |  |
| 5 | descripcion | varchar(255) | YES | — |  |
| 6 | activo | tinyint(1) | NO | 1 |  |

Claves y relaciones:

- FOREIGN KEY `fk_comando_vehiculo` (id_vehiculo) → vehiculo_iot.vehiculos.id_vehiculo; ON UPDATE CASCADE; ON DELETE CASCADE
- PRIMARY KEY `PRIMARY` (id_comando)
- UNIQUE `uq_comando_codigo` (id_vehiculo, codigo)
- UNIQUE `uq_comando_nombre` (id_vehiculo, nombre)

## BASE TABLE: demo_movimientos

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_demo_movimiento | int | NO | — | auto_increment |
| 2 | id_demo | int | NO | — |  |
| 3 | id_comando | int | NO | — |  |
| 4 | orden | int | NO | — |  |
| 5 | duracion_ms | int unsigned | NO | — |  |
| 6 | pausa_ms | int unsigned | NO | 0 |  |

Claves y relaciones:

- FOREIGN KEY `fk_mov_comando` (id_comando) → vehiculo_iot.comandos.id_comando; ON UPDATE CASCADE; ON DELETE RESTRICT
- FOREIGN KEY `fk_mov_demo` (id_demo) → vehiculo_iot.demos.id_demo; ON UPDATE CASCADE; ON DELETE CASCADE
- PRIMARY KEY `PRIMARY` (id_demo_movimiento)
- UNIQUE `uq_demo_orden` (id_demo, orden)

## BASE TABLE: demos

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_demo | int | NO | — | auto_increment |
| 2 | id_vehiculo | int | NO | — |  |
| 3 | nombre | varchar(80) | NO | — |  |
| 4 | descripcion | varchar(255) | YES | — |  |
| 5 | activo | tinyint(1) | NO | 1 |  |

Claves y relaciones:

- FOREIGN KEY `fk_demo_vehiculo` (id_vehiculo) → vehiculo_iot.vehiculos.id_vehiculo; ON UPDATE CASCADE; ON DELETE CASCADE
- PRIMARY KEY `PRIMARY` (id_demo)

## BASE TABLE: estado_vehiculo

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_estado | int | NO | — | auto_increment |
| 2 | id_vehiculo | int | NO | — |  |
| 3 | id_comando | int | YES | — |  |
| 4 | fecha_actualizacion | timestamp | YES | CURRENT_TIMESTAMP | DEFAULT_GENERATED on update CURRENT_TIMESTAMP |
| 5 | conectado | tinyint(1) | NO | 0 |  |

Claves y relaciones:

- FOREIGN KEY `fk_estado_comando` (id_comando) → vehiculo_iot.comandos.id_comando; ON UPDATE CASCADE; ON DELETE SET NULL
- FOREIGN KEY `fk_estado_vehiculo` (id_vehiculo) → vehiculo_iot.vehiculos.id_vehiculo; ON UPDATE CASCADE; ON DELETE CASCADE
- UNIQUE `id_vehiculo` (id_vehiculo)
- PRIMARY KEY `PRIMARY` (id_estado)

## BASE TABLE: historial_comandos

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_historial | bigint | NO | — | auto_increment |
| 2 | id_vehiculo | int | NO | — |  |
| 3 | id_comando | int | YES | — |  |
| 4 | fecha_hora | timestamp | YES | CURRENT_TIMESTAMP | DEFAULT_GENERATED |
| 5 | origen | varchar(30) | NO | MANUAL |  |

Claves y relaciones:

- FOREIGN KEY `fk_historial_comando` (id_comando) → vehiculo_iot.comandos.id_comando; ON UPDATE CASCADE; ON DELETE SET NULL
- FOREIGN KEY `fk_historial_vehiculo` (id_vehiculo) → vehiculo_iot.vehiculos.id_vehiculo; ON UPDATE CASCADE; ON DELETE CASCADE
- PRIMARY KEY `PRIMARY` (id_historial)

## BASE TABLE: vehiculos

| Orden | Columna | Tipo completo | Nulo | Predeterminado | Extra |
| --- | --- | --- | --- | --- | --- |
| 1 | id_vehiculo | int | NO | — | auto_increment |
| 2 | nombre | varchar(80) | NO | — |  |
| 3 | modelo | varchar(80) | YES | — |  |
| 4 | descripcion | varchar(255) | YES | — |  |
| 5 | microcontrolador | varchar(50) | YES | — |  |
| 6 | fecha_registro | timestamp | YES | CURRENT_TIMESTAMP | DEFAULT_GENERATED |
| 7 | activo | tinyint(1) | NO | 1 |  |

Claves y relaciones:

- PRIMARY KEY `PRIMARY` (id_vehiculo)

## Procedimiento: sp_actualizar_comando

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_comando | IN | int |
| 2 | p_nombre | IN | varchar(60) |
| 3 | p_codigo | IN | int |
| 4 | p_descripcion | IN | varchar(255) |
| 5 | p_activo | IN | tinyint(1) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_actualizar_comando`(
    IN p_id_comando INT,
    IN p_nombre VARCHAR(60),
    IN p_codigo INT,
    IN p_descripcion VARCHAR(255),
    IN p_activo BOOLEAN
)
BEGIN
    UPDATE comandos
       SET nombre=p_nombre, codigo=p_codigo,
           descripcion=p_descripcion, activo=p_activo
     WHERE id_comando=p_id_comando;
END
```

## Procedimiento: sp_actualizar_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo | IN | int |
| 2 | p_nombre | IN | varchar(80) |
| 3 | p_descripcion | IN | varchar(255) |
| 4 | p_activo | IN | tinyint(1) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_actualizar_demo`(
    IN p_id_demo INT,
    IN p_nombre VARCHAR(80),
    IN p_descripcion VARCHAR(255),
    IN p_activo BOOLEAN
)
BEGIN
    UPDATE demos
       SET nombre=p_nombre, descripcion=p_descripcion, activo=p_activo
     WHERE id_demo=p_id_demo;
END
```

## Procedimiento: sp_actualizar_estado

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |
| 2 | p_id_comando | IN | int |
| 3 | p_conectado | IN | tinyint(1) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_actualizar_estado`(
    IN p_id_vehiculo INT,
    IN p_id_comando INT,
    IN p_conectado BOOLEAN
)
BEGIN
    INSERT INTO estado_vehiculo(id_vehiculo,id_comando,conectado)
    VALUES(p_id_vehiculo,p_id_comando,p_conectado)
    ON DUPLICATE KEY UPDATE
      id_comando=VALUES(id_comando),
      conectado=VALUES(conectado),
      fecha_actualizacion=CURRENT_TIMESTAMP;
END
```

## Procedimiento: sp_actualizar_movimiento_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo_movimiento | IN | int |
| 2 | p_id_comando | IN | int |
| 3 | p_orden | IN | int |
| 4 | p_duracion_ms | IN | int |
| 5 | p_pausa_ms | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_actualizar_movimiento_demo`(
    IN p_id_demo_movimiento INT,
    IN p_id_comando INT,
    IN p_orden INT,
    IN p_duracion_ms INT,
    IN p_pausa_ms INT
)
BEGIN
    UPDATE demo_movimientos
       SET id_comando=p_id_comando, orden=p_orden,
           duracion_ms=p_duracion_ms, pausa_ms=p_pausa_ms
     WHERE id_demo_movimiento=p_id_demo_movimiento;
END
```

## Procedimiento: sp_actualizar_vehiculo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |
| 2 | p_nombre | IN | varchar(80) |
| 3 | p_modelo | IN | varchar(80) |
| 4 | p_descripcion | IN | varchar(255) |
| 5 | p_microcontrolador | IN | varchar(50) |
| 6 | p_activo | IN | tinyint(1) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_actualizar_vehiculo`(
    IN p_id_vehiculo INT,
    IN p_nombre VARCHAR(80),
    IN p_modelo VARCHAR(80),
    IN p_descripcion VARCHAR(255),
    IN p_microcontrolador VARCHAR(50),
    IN p_activo BOOLEAN
)
BEGIN
    UPDATE vehiculos
       SET nombre=p_nombre, modelo=p_modelo, descripcion=p_descripcion,
           microcontrolador=p_microcontrolador, activo=p_activo
     WHERE id_vehiculo=p_id_vehiculo;
END
```

## Procedimiento: sp_agregar_movimiento_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo | IN | int |
| 2 | p_id_comando | IN | int |
| 3 | p_orden | IN | int |
| 4 | p_duracion_ms | IN | int |
| 5 | p_pausa_ms | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_agregar_movimiento_demo`(
    IN p_id_demo INT,
    IN p_id_comando INT,
    IN p_orden INT,
    IN p_duracion_ms INT,
    IN p_pausa_ms INT
)
BEGIN
    INSERT INTO demo_movimientos
      (id_demo,id_comando,orden,duracion_ms,pausa_ms)
    VALUES
      (p_id_demo,p_id_comando,p_orden,p_duracion_ms,p_pausa_ms);
END
```

## Procedimiento: sp_crear_comando

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |
| 2 | p_nombre | IN | varchar(60) |
| 3 | p_codigo | IN | int |
| 4 | p_descripcion | IN | varchar(255) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_crear_comando`(
    IN p_id_vehiculo INT,
    IN p_nombre VARCHAR(60),
    IN p_codigo INT,
    IN p_descripcion VARCHAR(255)
)
BEGIN
    INSERT INTO comandos(id_vehiculo,nombre,codigo,descripcion)
    VALUES(p_id_vehiculo,p_nombre,p_codigo,p_descripcion);
    SELECT LAST_INSERT_ID() AS id_comando;
END
```

## Procedimiento: sp_crear_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |
| 2 | p_nombre | IN | varchar(80) |
| 3 | p_descripcion | IN | varchar(255) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_crear_demo`(
    IN p_id_vehiculo INT,
    IN p_nombre VARCHAR(80),
    IN p_descripcion VARCHAR(255)
)
BEGIN
    INSERT INTO demos(id_vehiculo,nombre,descripcion)
    VALUES(p_id_vehiculo,p_nombre,p_descripcion);
    SELECT LAST_INSERT_ID() AS id_demo;
END
```

## Procedimiento: sp_crear_vehiculo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_nombre | IN | varchar(80) |
| 2 | p_modelo | IN | varchar(80) |
| 3 | p_descripcion | IN | varchar(255) |
| 4 | p_microcontrolador | IN | varchar(50) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_crear_vehiculo`(
    IN p_nombre VARCHAR(80),
    IN p_modelo VARCHAR(80),
    IN p_descripcion VARCHAR(255),
    IN p_microcontrolador VARCHAR(50)
)
BEGIN
    INSERT INTO vehiculos(nombre, modelo, descripcion, microcontrolador)
    VALUES(p_nombre, p_modelo, p_descripcion, p_microcontrolador);
    SELECT LAST_INSERT_ID() AS id_vehiculo;
END
```

## Procedimiento: sp_eliminar_comando

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_comando | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_eliminar_comando`(IN p_id_comando INT)
BEGIN
    DELETE FROM comandos WHERE id_comando=p_id_comando;
END
```

## Procedimiento: sp_eliminar_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_eliminar_demo`(IN p_id_demo INT)
BEGIN
    DELETE FROM demos WHERE id_demo=p_id_demo;
END
```

## Procedimiento: sp_eliminar_movimiento_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo_movimiento | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_eliminar_movimiento_demo`(IN p_id_demo_movimiento INT)
BEGIN
    DELETE FROM demo_movimientos
     WHERE id_demo_movimiento=p_id_demo_movimiento;
END
```

## Procedimiento: sp_eliminar_vehiculo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_eliminar_vehiculo`(IN p_id_vehiculo INT)
BEGIN
    DELETE FROM vehiculos WHERE id_vehiculo=p_id_vehiculo;
END
```

## Procedimiento: sp_obtener_comandos_vehiculo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_comandos_vehiculo`(IN p_id_vehiculo INT)
BEGIN
    SELECT * FROM comandos
     WHERE id_vehiculo=p_id_vehiculo
     ORDER BY codigo;
END
```

## Procedimiento: sp_obtener_demos

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_demos`(IN p_id_vehiculo INT)
BEGIN
    SELECT * FROM demos
     WHERE id_vehiculo=p_id_vehiculo
     ORDER BY id_demo;
END
```

## Procedimiento: sp_obtener_estado

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_estado`(IN p_id_vehiculo INT)
BEGIN
    SELECT e.id_vehiculo, v.nombre AS vehiculo,
           e.id_comando, c.nombre AS comando, c.codigo,
           e.conectado, e.fecha_actualizacion
      FROM estado_vehiculo e
      JOIN vehiculos v ON v.id_vehiculo=e.id_vehiculo
 LEFT JOIN comandos c ON c.id_comando=e.id_comando
     WHERE e.id_vehiculo=p_id_vehiculo;
END
```

## Procedimiento: sp_obtener_historial

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_historial`(IN p_id_vehiculo INT)
BEGIN
    SELECT h.id_historial, h.id_vehiculo,
           c.nombre AS comando, c.codigo,
           h.fecha_hora, h.origen
      FROM historial_comandos h
 LEFT JOIN comandos c ON c.id_comando=h.id_comando
     WHERE h.id_vehiculo=p_id_vehiculo
     ORDER BY h.fecha_hora DESC, h.id_historial DESC;
END
```

## Procedimiento: sp_obtener_movimientos_demo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_demo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_movimientos_demo`(IN p_id_demo INT)
BEGIN
    SELECT dm.id_demo_movimiento, dm.orden,
           dm.id_comando, c.nombre AS comando, c.codigo,
           dm.duracion_ms, dm.pausa_ms
      FROM demo_movimientos dm
      JOIN comandos c ON c.id_comando=dm.id_comando
     WHERE dm.id_demo=p_id_demo
     ORDER BY dm.orden;
END
```

## Procedimiento: sp_obtener_vehiculo

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_vehiculo`(IN p_id_vehiculo INT)
BEGIN
    SELECT * FROM vehiculos WHERE id_vehiculo = p_id_vehiculo;
END
```

## Procedimiento: sp_obtener_vehiculos

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| — | Sin parámetros visibles | — | — |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_obtener_vehiculos`()
BEGIN
    SELECT * FROM vehiculos ORDER BY id_vehiculo;
END
```

## Procedimiento: sp_registrar_historial

| Orden | Nombre | Modo | Tipo completo |
| --- | --- | --- | --- |
| 1 | p_id_vehiculo | IN | int |
| 2 | p_id_comando | IN | int |
| 3 | p_origen | IN | varchar(30) |

Definición obtenida mediante SHOW CREATE PROCEDURE.

- Cuenta DEFINER omitida.

```sql
CREATE PROCEDURE `sp_registrar_historial`(
    IN p_id_vehiculo INT,
    IN p_id_comando INT,
    IN p_origen VARCHAR(30)
)
BEGIN
    INSERT INTO historial_comandos(id_vehiculo,id_comando,origen)
    VALUES(p_id_vehiculo,p_id_comando,p_origen);
END
```

## Fuentes y alcance

Metadatos: information_schema.TABLES, COLUMNS, TABLE_CONSTRAINTS, KEY_COLUMN_USAGE, REFERENTIAL_CONSTRAINTS, ROUTINES y PARAMETERS. Las consultas se ejecutan en una transacción READ ONLY y los procedimientos solo se describen; nunca se invocan.

El tipo completo de los parámetros procede de DTD_IDENTIFIER, según la [documentación MySQL](https://dev.mysql.com/doc/refman/8.0/en/information-schema-parameters-table.html). La definición depende de los [permisos de SHOW CREATE PROCEDURE](https://dev.mysql.com/doc/refman/8.0/en/show-create-procedure.html).
