-- Esquema complementario. Ejecutar únicamente tras revisar 000_preflight.sql.
-- MySQL 8.0. Ninguna sentencia altera tablas ni procedimientos de 4.1/4.2.
CREATE TABLE IF NOT EXISTS iot43_migraciones (
    version VARCHAR(25) PRIMARY KEY,
    ejecutada_en TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_cuentas (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    correo VARCHAR(254) NOT NULL UNIQUE,
    nombre VARCHAR(100) NOT NULL,
    hash_contrasena VARCHAR(240) CHARACTER SET ascii NOT NULL,
    rol ENUM('administrador','comprador') NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    INDEX ix_iot43_cuenta_activa (rol, activo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_modelos (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    sku VARCHAR(40) NOT NULL UNIQUE,
    nombre VARCHAR(100) NOT NULL,
    descripcion VARCHAR(255),
    publicado BOOLEAN NOT NULL DEFAULT FALSE,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    INDEX ix_iot43_modelo_publico (publicado, activo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_capacidades (
    id_modelo BIGINT UNSIGNED NOT NULL,
    codigo INT NOT NULL,
    control BOOLEAN NOT NULL,
    invitado BOOLEAN NOT NULL,
    PRIMARY KEY (id_modelo, codigo),
    CONSTRAINT fk_iot43_capacidad_modelo FOREIGN KEY (id_modelo) REFERENCES iot43_modelos(id),
    CONSTRAINT ck_iot43_capacidad_invitado CHECK (invitado = 0 OR control = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_unidades (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_vehiculo INT NULL UNIQUE,
    id_modelo BIGINT UNSIGNED NOT NULL,
    serie VARCHAR(60) NOT NULL UNIQUE,
    nombre VARCHAR(100) NOT NULL,
    estado ENUM('inventario','exhibicion','vendida','inactiva') NOT NULL DEFAULT 'inventario',
    fisico BOOLEAN NOT NULL DEFAULT FALSE,
    id_cliente BIGINT UNSIGNED NULL,
    id_titularidad BIGINT UNSIGNED NULL,
    version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    CONSTRAINT fk_iot43_unidad_vehiculo FOREIGN KEY (id_vehiculo) REFERENCES vehiculos(id_vehiculo),
    CONSTRAINT fk_iot43_unidad_modelo FOREIGN KEY (id_modelo) REFERENCES iot43_modelos(id),
    CONSTRAINT fk_iot43_unidad_cliente FOREIGN KEY (id_cliente) REFERENCES iot43_cuentas(id),
    CONSTRAINT ck_iot43_unidad_titular CHECK (
        (id_cliente IS NULL AND id_titularidad IS NULL AND estado <> 'vendida')
        OR (id_cliente IS NOT NULL AND id_titularidad IS NOT NULL AND estado IN ('vendida','inactiva'))
    ),
    CONSTRAINT ck_iot43_unidad_fisica CHECK (fisico=0 OR id_vehiculo IS NOT NULL),
    INDEX ix_iot43_unidad_cliente (id_cliente, estado),
    INDEX ix_iot43_unidad_exhibicion (id_modelo, estado, fisico)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_titularidad (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_unidad BIGINT UNSIGNED NOT NULL,
    id_cliente BIGINT UNSIGNED NOT NULL,
    id_actor BIGINT UNSIGNED NOT NULL,
    inicio DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    fin DATETIME(6),
    unidad_vigente BIGINT UNSIGNED GENERATED ALWAYS AS (IF(fin IS NULL, id_unidad, NULL)) STORED,
    CONSTRAINT fk_iot43_tit_unidad FOREIGN KEY (id_unidad) REFERENCES iot43_unidades(id),
    CONSTRAINT fk_iot43_tit_cliente FOREIGN KEY (id_cliente) REFERENCES iot43_cuentas(id),
    CONSTRAINT fk_iot43_tit_actor FOREIGN KEY (id_actor) REFERENCES iot43_cuentas(id),
    UNIQUE KEY uq_iot43_tit_vigente (unidad_vigente),
    INDEX ix_iot43_tit_historial (id_unidad, inicio)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_reservas (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_unidad BIGINT UNSIGNED NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    unidad_vigente BIGINT UNSIGNED GENERATED ALWAYS AS (IF(ended_at IS NULL, id_unidad, NULL)) STORED,
    CONSTRAINT fk_iot43_reserva_unidad FOREIGN KEY (id_unidad) REFERENCES iot43_unidades(id),
    UNIQUE KEY uq_iot43_reserva_vigente (unidad_vigente),
    INDEX ix_iot43_reserva_expiracion (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_sesiones (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    token_hash BINARY(32) NOT NULL UNIQUE,
    id_cuenta BIGINT UNSIGNED NULL,
    id_reserva BIGINT UNSIGNED NULL,
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_iot43_sesion_cuenta FOREIGN KEY (id_cuenta) REFERENCES iot43_cuentas(id),
    CONSTRAINT fk_iot43_sesion_reserva FOREIGN KEY (id_reserva) REFERENCES iot43_reservas(id),
    CONSTRAINT ck_iot43_sesion_identidad CHECK ((id_cuenta IS NULL) <> (id_reserva IS NULL)),
    INDEX ix_iot43_sesion_cuenta (id_cuenta, revoked_at),
    INDEX ix_iot43_sesion_reserva (id_reserva, revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_tickets (
    token_hash BINARY(32) PRIMARY KEY,
    id_sesion BIGINT UNSIGNED NOT NULL,
    id_unidad BIGINT UNSIGNED NOT NULL,
    version_unidad BIGINT UNSIGNED NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6),
    CONSTRAINT fk_iot43_ticket_sesion FOREIGN KEY (id_sesion) REFERENCES iot43_sesiones(id),
    CONSTRAINT fk_iot43_ticket_unidad FOREIGN KEY (id_unidad) REFERENCES iot43_unidades(id),
    INDEX ix_iot43_ticket_expiracion (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_demos (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_modelo BIGINT UNSIGNED NOT NULL,
    id_unidad BIGINT UNSIGNED NULL,
    id_titularidad BIGINT UNSIGNED NULL,
    nombre VARCHAR(80) NOT NULL,
    descripcion VARCHAR(255),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    CONSTRAINT fk_iot43_demo_modelo FOREIGN KEY (id_modelo) REFERENCES iot43_modelos(id),
    CONSTRAINT fk_iot43_demo_unidad FOREIGN KEY (id_unidad) REFERENCES iot43_unidades(id),
    CONSTRAINT fk_iot43_demo_titular FOREIGN KEY (id_titularidad) REFERENCES iot43_titularidad(id),
    CONSTRAINT ck_iot43_demo_propiedad CHECK ((id_unidad IS NULL) = (id_titularidad IS NULL)),
    INDEX ix_iot43_demo_modelo (id_modelo, id_unidad, activo),
    INDEX ix_iot43_demo_titular (id_titularidad, id_unidad)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_pasos (
    id_demo BIGINT UNSIGNED NOT NULL,
    orden SMALLINT UNSIGNED NOT NULL,
    codigo INT NOT NULL,
    duracion_ms INT UNSIGNED NOT NULL,
    pausa_ms INT UNSIGNED NOT NULL,
    PRIMARY KEY (id_demo, orden),
    CONSTRAINT fk_iot43_paso_demo FOREIGN KEY (id_demo) REFERENCES iot43_demos(id),
    CONSTRAINT ck_iot43_paso_limites CHECK (orden BETWEEN 1 AND 30 AND
        duracion_ms BETWEEN 1 AND 5000 AND pausa_ms <= 5000)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS iot43_auditoria (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    id_actor BIGINT UNSIGNED NULL,
    accion VARCHAR(60) NOT NULL,
    recurso VARCHAR(40) NOT NULL,
    id_recurso BIGINT UNSIGNED NULL,
    resultado VARCHAR(32) NOT NULL,
    fecha DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_iot43_audit_actor FOREIGN KEY (id_actor) REFERENCES iot43_cuentas(id),
    INDEX ix_iot43_audit_fecha (fecha, id),
    INDEX ix_iot43_audit_recurso (recurso, id_recurso)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO iot43_migraciones (version) VALUES ('001_schema');
