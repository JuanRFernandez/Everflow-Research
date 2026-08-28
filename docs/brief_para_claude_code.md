# Brief para la sesión de Claude Code — repo `JuanRFernandez/Everflow-Research`
**Fecha: 28.08.2026 · De: la sesión de Cowork (Gmail + Drive + Asana) · Para: vos, que tocás el repo**

---

## 0. Por qué te escribo

Cambió la arquitectura del workbook y cambió el estado comercial. Antes de que corras nada, leé esto entero. Lo que hacías hasta v09 —bajar un `.xlsx`, escribirlo, subir una versión nueva— **ya no aplica y no debe volver a aplicarse**.

---

## 1. Estado del workbook

El Partner Database **ya no es un archivo**. Es un Google Sheet nativo, vivo:

- **ID:** `1rp2OwQI0dTx697r70ldQerZWxSQdoLPdA7WvQhvhXNQ`
- Nombre: `2026-08-27_EFE_Alpine_Partner_Database_v10`
- En la carpeta `05_PARTNERS_AGENCIES_B2B` **no hay ningún `.xlsx`** y no vuelve a haberlo. El resolver de la CLI no va a encontrar archivo, y está bien: hay que sacarlo.

Estructura verificada hoy contra un export:

- Pestaña `PARTNERS`: encabezado en fila 1, datos en filas **2–410** (409 filas, sin huecos), **40 columnas A–AN**.
- IDs `EFE-0001`…`EFE-0505`, no correlativos, 409 únicos.
- La corrección de v09 sobrevivió entera: escuelas en `EFE-0440..0505`, hoteles en `EFE-0359..0439`, teléfonos en E.164.
- Otras pestañas: `READ_ME`, `DASHBOARD`, `RESORTS_SBI`, `PRICING_BENCH`, `REGULATORY`, `_SOURCES`, `_GAPS_ROUND2`, `CHANGELOG` (última fila usada: 13), `CHANGELOG_DETAIL` (1916 filas de auditoría).

Mapa de columnas, por si lo tenés hardcodeado en otro orden:

```
A  ID                     O  LinkedIn_URL            AB Follow_Up_Days
B  Entity_Name            P  Instagram_Handle        AC Next_Follow_Up  (FÓRMULA)
C  Category               Q  Segment_Tier            AD Email_Sent
D  Subcategory            R  Star_Rating_or_Class    AE Call_Made
E  Resort_Base            S  Capacity_Keys_or_Beds   AF WhatsApp_Sent
F  Region_Valley          T  B2B_Program_Exists      AG Meeting_Booked
G  Country                U  Commission_or_Partner_Terms  AH Agreement_Signed
H  Website_URL            V  Languages_Served        AI Status
I  General_Email          W  Owner_or_Group_Affiliation   AJ Next_Action
J  Sales_B2B_Email        X  Strategic_Fit_Note      AK Source_URL
K  Phone                  Y  Priority_Score          AL Date_Verified
L  WhatsApp               Z  Contacted               AM Round
M  Contact_Person_Name    AA Contact_Date            AN Material_Sent
N  Contact_Person_Role
```

---

## 2. Defectos estructurales encontrados (se están arreglando desde el Sheet, no desde el repo)

Todos tienen la misma causa: **rangos con la fila final escrita a mano**.

| Defecto | Detalle |
|---|---|
| Validación de datos capada en 251 | 5 reglas (`Y`, `Z`, `T`, `AD:AH`, `AI`). Las filas 252–410 no tienen desplegable. |
| **No hay formato condicional en ninguna pestaña** | La banda crema de las columnas CRM es relleno estático `Z2:AJ251`. `AN` nunca lo tuvo. |
| DASHBOARD capado en `$400` | 74 fórmulas. Las filas 401–410 (`EFE-0496..0505`, escuelas de Südtirol) no las cuenta nadie. |
| 9 filas sin la fórmula de `AC` | Filas 32, 33, 85, 163, 166, 168, 193, 194, 196. Se perdieron en un `promote`. |
| DASHBOARD lista 9 de 21 status | Quedan afuera ~20 filas, incluidas 4 con `Status = "Duplicate of EFE-0182 / 0208 / 0210 / 0212"`. |

Esto se resuelve con un Apps Script pegado en la propia planilla (menú `EFE ▸ Reparar estructura`), con rangos abiertos (`Z2:Z` en vez de `Z2:Z251`). **No lo hagas vos desde el repo.** Sólo tenelo en cuenta: si tu código asume que el DASHBOARD llega hasta la 400, esa suposición muere.

---

## 3. Lo que pasó con los hoteles (contexto comercial, importa para priorizar)

El 27.08 a mediodía salieron 5 mails a trade desks del corredor tirolés desde `jfernandez@everflowexperience.com`, **sin adjuntos** (política de primer contacto). En menos de 24 h respondieron 3. Verificado contra Gmail, no contra un resumen:

| Fila | ID | Hotel | Qué pasó |
|---|---|---|---|
| 262 | EFE-0264 | Kempinski Das Tirol | Isabel Zengerle. **Meeting + site inspection martes 01.09 11:00**, Ladies in Red desk. Confirmado por ambas partes. |
| 265 | EFE-0267 | Schlosshotel Kitzbühel | Sara Sponring. **Primera comisión sobre la mesa: 10–15 % sobre la logis según temporada.** Ofreció la semana del 05.10; Juan propuso jueves 08.10 14:00, sin confirmar todavía. |
| 266 | EFE-0268 | Interalpen-Hotel Tyrol | Susanne Assmann. **Visita lunes 21.09 09:30**, ella recibe en la llegada y manda invite. Aclaró que no es un hotel de esquí clásico. |
| 268 | EFE-0270 | Klosterbräu & SPA | Silencio. Follow-up 10.09. Ojo: la nota de la fila dice que está **cerrado por incendio hasta diciembre 2026**. |
| 270 | EFE-0272 | Jagdhof | Silencio. Follow-up 10.09. Tiene desk dedicado a agencias — es el aliado natural nº 1. |

Los cambios de CRM de esas tres filas los aplica Juan a mano. **No los toques.**

---

## 4. La regla nueva

1. **El Google Sheet es el master.** No se emiten más archivos `vNN`. Ninguno.
2. Si necesitás leer, **exportá una copia temporal, leela y borrala**. Esa copia no es una versión de nada y no se guarda en Drive.
3. **Ningún tool escribe en el Sheet.** La salida es siempre una propuesta de cambios que aplica Juan.
4. **La versión es una fila del `CHANGELOG`**, no un archivo. La próxima es la fila 14.
5. Cuando haya tiempo: migrar lectura/escritura a la **Google Sheets API contra el ID**, no contra una carpeta. El `sheet_id` va en `config.yaml` y el `folder_id` deja de usarse. Eso mata el resolver, el ciclo bajar/subir y la pérdida de formato nativo.

---

## 5. El bug de las "filas doradas" — esto es lo que más importa arreglar

Hoy el enricher congela la **fila entera** cuando `Contacted = YES`. Resultado: en cuanto Juan contacta a un hotel, esa fila deja de enriquecerse para siempre — justo cuando pasa a ser importante. Las 5 filas de arriba están congeladas y les falta WhatsApp en 4, LinkedIn en 4, y a Klosterbräu además el `Sales_B2B_Email`.

**La regla correcta se define por columna, no por fila.** Reemplazá el guard de fila dorada por esto:

```yaml
# Nunca escribir. Ninguna herramienta, en ninguna fila.
PROTECTED_COLS: [U, Z, AA, AB, AC, AD, AE, AF, AG, AH, AI, AJ, AN]

# El enricher puede escribir, pero SÓLO si la celda está vacía.
RESEARCH_COLS: [H, I, J, K, L, O, P, Q, R, S, T, V, W, X, AK, AL]

# Sólo las crea `promote`.
IDENTITY_COLS: [A, B, C, D, E, F, G]

# El enricher si está vacío; si Juan escribió algo, gana lo de Juan.
CONTACT_COLS: [M, N]
```

`AC` es una fórmula matriz: **ni la leas como dato ni la escribas nunca.**

Con esa separación, `Contacted = YES` deja de ser candado y pasa a ser **prioridad**: son los negocios vivos, se enriquecen primero.

Dos reglas más que hacen falta:

- **Vacío normalizado.** Tratar `""`, `"TBD"`, `"-"`, `"n/a"`, `"N/A"` como celda vacía.
- **Validación por tipo de columna.** `I` y `J` tienen que matchear un regex de email. Si el contenido no es un email, cuenta como vacío y se re-enriquece. Eso solo destraba dos casos reales:
  - `J268` (Klosterbräu) dice literalmente `TBD`.
  - `J270` (Jagdhof) dice `Kai Schweigkofler — Travel Agency Support desk`, que es el nombre repetido de `M`+`N`, no una dirección.

---

## 6. Formato de salida — esto es un contrato, respetalo exacto

El Sheet tiene un Apps Script con un panel que come bloques de texto plano. **Una línea por celda:**

```
J262   isabel.zengerle@kempinski.com
X262 += | GM: Axel Bethke
DASHBOARD!B30 = =COUNTA(PARTNERS!$G$2:$G)-SUM(B21:B29)
```

- Primer token: referencia A1. Sin prefijo de hoja se asume `PARTNERS`.
- `+=` agrega al final de lo que ya hay en la celda (para `X`, notas).
- Las líneas vacías y las que empiezan con `#` se ignoran — usalas para agrupar y comentar.

Agregá un emisor `--emit-paste` que produzca exactamente eso. Seguí emitiendo además el CSV de auditoría de siempre (`Timestamp, Run_ID, Row, Entity_ID, Entity_Name, Column, Field, Old_Value, New_Value, Confidence, Data_Class, Source_URL, Fetched_At, Extractor, Note`) para `CHANGELOG_DETAIL`, pero el entregable principal ahora es el bloque pegable.

---

## 7. Lo que te pido, en orden

1. **Sacar el resolver de archivos.** `config.yaml` pasa a `sheet_id: 1rp2OwQI0dTx697r70ldQerZWxSQdoLPdA7WvQhvhXNQ`. Mientras no haya credenciales de Google, la lectura es desde un export temporal que el propio comando descarga y borra.
2. **Implementar la propiedad por columna** de la sección 5, con tests. El repo ya tiene 305 tests; sumá los casos: intento de escritura en columna protegida → falla; celda con `TBD` → cuenta como vacía; celda con email válido → no se pisa.
3. **`efe enrich --rows contacted --cols J,K,L,O --only-empty --emit-paste`.** Primer target: las filas 262, 265, 266, 268, 270. Lo que falta concretamente es WhatsApp y LinkedIn en casi todas, y el `Sales_B2B_Email` real de Klosterbräu y Jagdhof.
4. **Después, las 28 filas restantes con `Contacted = YES`.** Son 33 en total hoy.
5. **Actualizar el banner del `READ_ME`**, que sigue diciendo v07 y 39 columnas. Son 40 y va por v10. Esto sí sale como líneas del bloque pegable.

---

## 8. Lo que NO tenés que hacer

- No escribas en el Sheet. No hay credenciales todavía y no es tu rol.
- No emitas ningún archivo `vNN`, ni `.xlsx`, ni lo subas a Drive.
- No toques ninguna columna CRM, en ninguna fila, ni siquiera si está vacía.
- No inventes contactos. La regla de la casa sigue siendo: si no lo encontrás en una fuente verificable, va `TBD` y se anota en `_GAPS_ROUND2`.
- No reordenes ni renumeres IDs. Eso ya se hizo en v09 y quedó bien.

---

## 9. Contexto de negocio, para que priorices bien

EverFlow Experience es la escuela de esquí y agencia de Juan, con sede en Múnich, registrada como empresa unipersonal y como intermediario de viajes (comisión, no organizador). La prioridad número uno ahora mismo son **las partnerships con hoteles** — ahí está la plata, y la de Schlosshotel es la primera comisión concreta que aparece. Todo lo que acelere encontrar el contacto de trade correcto de un hotel vale; todo lo que produzca otra versión de un archivo, no.
