-- Laboratorio exclusivamente. Ejecutar en una sesión que estableció antes:
-- SET @iot43_seed_confirm = 1;
-- Un esquema de producción no es elegible aunque la variable esté activa.
START TRANSACTION;
SET @iot43_lab = COALESCE(@iot43_seed_confirm, 0)=1
    AND DATABASE() REGEXP '(_lab|_test|_sandbox)$';

INSERT INTO iot43_modelos (sku, nombre, descripcion, publicado, activo)
SELECT 'LAB-NODE01','Montacargas virtual','Unidad de laboratorio para demostrar la plataforma.',TRUE,TRUE
WHERE @iot43_lab AND NOT EXISTS (SELECT 1 FROM iot43_modelos WHERE sku='LAB-NODE01');
SET @iot43_model = (SELECT id FROM iot43_modelos WHERE sku='LAB-NODE01' AND @iot43_lab);

INSERT INTO vehiculos (nombre,descripcion,activo)
SELECT 'NODE/01 | Laboratorio 4.3','Simulación sin dispositivo físico',TRUE
WHERE @iot43_model IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM vehiculos WHERE nombre='NODE/01 | Laboratorio 4.3');
SET @iot43_vehicle = (SELECT MIN(id_vehiculo) FROM vehiculos
    WHERE nombre='NODE/01 | Laboratorio 4.3' AND descripcion='Simulación sin dispositivo físico'
      AND @iot43_model IS NOT NULL);

INSERT INTO comandos (id_vehiculo,nombre,codigo,descripcion,activo)
SELECT @iot43_vehicle, seed.nombre,seed.codigo,'Comando virtual de laboratorio',TRUE
FROM (
    SELECT 1 codigo,'GI' nombre UNION ALL SELECT 2,'AVANZAR' UNION ALL SELECT 3,'VELOCIDAD'
    UNION ALL SELECT 4,'SUBIR_GRUA' UNION ALL SELECT 5,'RETROCEDER' UNION ALL SELECT 6,'DEMO'
    UNION ALL SELECT 7,'BAJAR_GRUA' UNION ALL SELECT 8,'GIRAR_DERECHA'
) seed
WHERE @iot43_vehicle IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM comandos c
    WHERE c.id_vehiculo=@iot43_vehicle AND (c.codigo=seed.codigo OR c.nombre=seed.nombre)
);

INSERT INTO iot43_capacidades (id_modelo,codigo,control,invitado)
SELECT @iot43_model,c.codigo,TRUE,(c.codigo IN (1,2,5,8))
FROM comandos c WHERE c.id_vehiculo=@iot43_vehicle AND @iot43_model IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM iot43_capacidades f WHERE f.id_modelo=@iot43_model AND f.codigo=c.codigo);

INSERT INTO iot43_unidades (id_vehiculo,id_modelo,serie,nombre,estado,fisico)
SELECT @iot43_vehicle,@iot43_model,'NODE01-LAB-43','Montacargas virtual','exhibicion',FALSE
WHERE @iot43_vehicle IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM iot43_unidades WHERE serie='NODE01-LAB-43' OR id_vehiculo=@iot43_vehicle
);
INSERT IGNORE INTO iot43_migraciones (version) SELECT '002_seed_lab' WHERE @iot43_lab;
COMMIT;
SELECT @iot43_lab AS laboratorio_autorizado, @iot43_vehicle AS vehiculo_virtual;
