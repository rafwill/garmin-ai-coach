# Bitacora de tiempo - Garmin AI Coach

Este archivo registra el tiempo invertido por dia en el proyecto.

## Criterio de registro
- Unidad recomendada: horas y minutos (hh:mm).
- Alcance: tiempo total de trabajo en el proyecto por dia.
- Recuperacion historica: calculo por transcript de sesion, usando inicio de sesion -> ultima actividad registrada.
- Para cada transcript se computa su duracion y se agrega al dia de inicio de la sesion.
- En dias sin sesiones registradas en transcript se muestra 00:00.

## Protocolo de actualizacion (OBLIGATORIO)

### Al iniciar sesion
1. Consultar el session store para obtener la sesion activa: `created_at` del dia de hoy.
2. Revisar si el dia anterior tiene valor correcto en el registro:
   - Si el dia anterior tiene valor de la sesion store (`updated_at` fiable): verificar y corregir si difiere.
   - Si el dia anterior solo tiene estimacion o calculo de transcript: dejarlo como esta salvo que haya datos mejores.
3. Si hay dias recientes con `00:00` y el session store muestra sesiones en esos dias, actualizar con la duracion calculada.

### Al cerrar sesion
1. Consultar el session store: `NOW - created_at` de la sesion actual = duracion real de la sesion de hoy.
2. Actualizar la fila del dia de hoy con ese valor y fuente `session store (HH:MM - HH:MM)`.
3. Recalcular y actualizar el **Total acumulado**.
4. Hacer commit del BITACORA.md actualizado.

### Fuente de datos
- `session store` (preferida): query `SELECT created_at, updated_at FROM sessions WHERE date(created_at) = 'YYYY-MM-DD'` — fiable solo si `updated_at` no fue sobreescrito por un reindex posterior.
- `transcript`: calculo manual inicio -> ultima actividad — usado para dias historicos sin session store fiable.
- Si ambas fuentes difieren, prevalece la que tenga mayor precision temporal.

## Registro diario

| Fecha | Tiempo | Fuente |
|---|---:|---|
| 15-06-2026 | 00:00 | sin sesiones registradas |
| 16-06-2026 | 09:24 | sesion inicio->ultima actividad |
| 17-06-2026 | 00:22 | sesion inicio->ultima actividad |
| 18-06-2026 | 03:44 | sesion inicio->ultima actividad |
| 19-06-2026 | 00:00 | sin sesiones registradas |
| 20-06-2026 | 00:02 | sesion inicio->ultima actividad |
| 21-06-2026 | 00:00 | sin sesiones registradas |
| 22-06-2026 | 00:00 | sin sesiones registradas |
| 23-06-2026 | 00:00 | sin sesiones registradas |
| 24-06-2026 | 00:00 | sin sesiones registradas |
| 25-06-2026 | 00:00 | sin sesiones registradas |
| 26-06-2026 | 00:00 | sin sesiones registradas |
| 27-06-2026 | 00:00 | sin sesiones registradas |
| 28-06-2026 | 00:00 | sin sesiones registradas |
| 29-06-2026 | 00:00 | sin sesiones registradas |
| 30-06-2026 | 00:00 | sin sesiones registradas |
| 01-07-2026 | 00:00 | sin sesiones registradas |
| 02-07-2026 | 00:00 | sin sesiones registradas |
| 03-07-2026 | 00:00 | sin sesiones registradas |
| 04-07-2026 | 00:00 | sin sesiones registradas |
| 05-07-2026 | 00:00 | sin sesiones registradas |
| 06-07-2026 | 04:41 | sesion inicio->ultima actividad |
| 07-07-2026 | 00:00 | sin sesiones registradas |
| 08-07-2026 | 04:58 | sesion inicio->ultima actividad |
| 09-07-2026 | 03:04 | session store (inicio 14:37 → ultima sesion indexada 17:41) |

## Total acumulado
- 23:15

## Plantilla para nuevas entradas

Usar este formato para agregar una nueva fila:

| DD-MM-AAAA | hh:mm | Fuente (sesion inicio->ultima actividad o manual justificado) |
