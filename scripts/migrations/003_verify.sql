-- Solo lectura. Volver a ejecutar también 000_preflight.sql y comparar su salida.
SELECT version, ejecutada_en FROM iot43_migraciones ORDER BY version;
SELECT TABLE_NAME,CONSTRAINT_NAME,CONSTRAINT_TYPE
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA=DATABASE() AND LEFT(TABLE_NAME,6)='iot43_'
ORDER BY TABLE_NAME,CONSTRAINT_NAME;

SELECT 'titularidad de unidad inconsistente' AS comprobacion,COUNT(*) AS incidentes
FROM iot43_unidades u LEFT JOIN iot43_titularidad t ON t.id=u.id_titularidad
WHERE (u.id_titularidad IS NOT NULL AND (t.id IS NULL OR t.id_unidad<>u.id OR t.id_cliente<>u.id_cliente OR t.fin IS NOT NULL))
UNION ALL SELECT 'titularidad vigente sin unidad consistente',COUNT(*)
FROM iot43_titularidad t JOIN iot43_unidades u ON u.id=t.id_unidad
WHERE t.fin IS NULL AND (u.id_titularidad IS NULL OR u.id_titularidad<>t.id)
UNION ALL SELECT 'reserva activa incompatible',COUNT(*)
FROM iot43_reservas r JOIN iot43_unidades u ON u.id=r.id_unidad
WHERE r.ended_at IS NULL AND r.expires_at>UTC_TIMESTAMP(6) AND (u.estado<>'exhibicion' OR u.fisico OR u.id_cliente IS NOT NULL)
UNION ALL SELECT 'DEMO privada no corresponde a su titularidad',COUNT(*)
FROM iot43_demos d JOIN iot43_titularidad t ON t.id=d.id_titularidad
WHERE t.id_unidad<>d.id_unidad;
