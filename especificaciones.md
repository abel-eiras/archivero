# Especificación: Gestor de facturas por correo para farmacias

Versión del documento: 0.1 (diseño previo a codificación)

## 1. Visión y alcance

Aplicación web local, instalada en el ordenador de cada farmacia, que vigila uno o varios buzones de correo, detecta facturas de proveedores recibidas, las clasifica, extrae sus datos relevantes, las archiva en una estructura de carpetas fija y permite conciliarlas con los movimientos bancarios. Incluye un dashboard con estadísticas y métricas.

Alcance incluido: lectura de correo, procesado con modelo de lenguaje, archivo ordenado, base de datos local, dashboard, conciliación bancaria por importación de ficheros, actualización automática desde GitHub.

Fuera de alcance: facturas emitidas por la farmacia a clientes finales, conexión automática a webs de bancos, versión multicliente alojada en servidor, contabilidad completa (esto es contabilidad básica de control).

Usuario objetivo: farmacias, con perfil no técnico. Prioridad absoluta a Windows; Mac como secundario; Linux solo de forma incidental.

## 2. Principios de diseño

1. SQLite es la única fuente de verdad. El dashboard y toda la lógica leen de la base de datos, nunca de los archivos archivados.
2. Los archivos en carpetas son una copia ordenada para la gestoría. La app anota la ruta de cada archivo pero nunca depende de que siga existiendo.
3. Cada instalación es una isla: su propia base de datos, sus propias claves y su propia carpeta de salida. No hay servidor central ni usuarios que gestionar.
4. Esquema flexible al inicio: pocos campos fijos y un campo JSON para datos extra, para afinar con los betas sin migraciones traumáticas.
5. El modelo se equivoca a veces: toda factura pasa por un estado de pendiente de revisión hasta que el usuario la confirma, o hasta que supera un umbral de confianza configurable.
6. Nada se pierde: escritura primero en carpeta temporal, reintentos ante fallos de la unidad de destino, y copia de seguridad de la base antes de cada migración.

## 3. Arquitectura general

Un único programa que arranca un servidor local y un proceso en segundo plano.

- Backend en Python con FastAPI.
- Interfaz web ligera servida por el propio backend, accesible solo en localhost para que nadie de la red de la farmacia pueda abrir el panel.
- Proceso en segundo plano que consulta los buzones de forma periódica.
- Base de datos SQLite local.
- Empaquetado como ejecutable para Windows y Mac, y opcionalmente como contenedor Docker para quien tenga servidor.
- Un pequeño actualizador independiente para reemplazar el ejecutable.

Bloques funcionales: conexión al correo, procesado con el modelo, almacenamiento y archivo, interfaz con dashboard, conciliación bancaria, seguridad y actualizaciones.

## 4. Módulo de correo

Dos modos de conexión detrás de una misma interfaz interna:

- IMAP genérico con usuario y contraseña de aplicación, para servidores corporativos y cualquier proveedor que lo permita.
- OAuth para Gmail y Outlook, necesario porque estos proveedores están eliminando el acceso con contraseña simple.

Comportamiento:

- Se admiten varias cuentas de correo por instalación, por ejemplo administración y titular.
- Solo se procesan mensajes nuevos. Se guarda el identificador de cada mensaje en SQLite para no reprocesar nunca el mismo correo.
- Un mensaje se considera candidato si viene de una dirección de la lista de remitentes configurada, o si trae un PDF adjunto cuyo contenido parece una factura.
- Reglas por remitente configurables: por ejemplo, un proveedor concreto siempre es de una categoría concreta.

## 5. Módulo de procesado con el modelo de lenguaje

Proveedor por defecto: API gratuita de Google AI Studio, con la clave de cada usuario. El proveedor del modelo debe ser configurable, porque el nivel gratuito puede usar los datos enviados para mejorar los modelos de Google, y algunos clientes preferirán una clave de pago u otro proveedor.

Flujo por cada PDF candidato:

1. Extraer el texto del PDF. Si no tiene capa de texto, aplicar OCR, o enviar el documento directamente al modelo si admite visión.
2. Pedir al modelo una respuesta estructurada en JSON con el tipo de documento, la categoría, el proveedor y los datos de la factura.
3. Validar el JSON: comprobar que base imponible más IVA cuadra con el total, que las fechas son coherentes y que los campos obligatorios existen.
4. Calcular el hash del contenido del PDF para detectar duplicados aunque lleguen con otro nombre o por otro correo.
5. Asignar un nivel de confianza y decidir si la factura queda confirmada o pendiente de revisión.

Tipos de documento iniciales: factura de compra de mercancía, factura de gasto o material, factura de servicios, alquiler, abono o factura rectificativa, y otros.

Categorías: genéricas y abiertas, no específicas de farmacia. Ejemplos de partida: compras de mercancía para vender, gastos de material para uso propio, suministros como agua, luz, gas e internet, servicios profesionales, alquiler, seguros, impuestos y tasas, otros. La lista final se definirá con los betas.

## 6. Almacenamiento y archivo

El almacenamiento se resuelve en dos capas independientes, para que nada dependa de cómo sincronice cada sistema operativo.

Capa 1, obligatoria: carpeta de salida configurable. El usuario elige una ruta y la app escribe ahí con rutas normales. En Windows y Mac, Google Drive para escritorio, OneDrive o Dropbox montan una carpeta o unidad, que para la app es una carpeta más. Así la sincronización con la gestoría la hace la aplicación oficial del servicio.

Capa 2, opcional y posterior: conexión directa con la API de Google Drive mediante OAuth, para quien no tenga el cliente de escritorio, o para Linux. El usuario autoriza, elige una carpeta de Drive y la app sube los archivos creando la estructura.

Ambas capas se implementan detrás de una misma interfaz interna de almacenamiento, con dos implementaciones: carpeta local y Drive por API.

Estructura de carpetas fija: año, mes, categoría y proveedor. El nombre del archivo lleva la fecha, el proveedor y el número de factura.

Reglas de robustez:

- Escribir primero en una carpeta temporal y mover el archivo al final.
- Reintentar si la unidad de destino falla o va lenta, sin perder nunca la factura.
- Guardar en SQLite la ruta final de cada archivo, sabiendo que puede dejar de ser válida si alguien lo mueve o renombra. En ese caso el dashboard sigue funcionando y solo falla el botón de abrir el PDF, que avisa de que no lo encuentra.

## 7. Base de datos

SQLite local como única fuente de verdad. El esquema es deliberadamente genérico en la fase inicial y se afina con los primeros betas.

Tablas previstas:

- Facturas: campos fijos más un campo JSON de datos extra.
- Proveedores: nombre, NIF, categoría habitual y reglas asociadas.
- Cuentas de correo: configuración de cada buzón, sin guardar secretos en la base.
- Mensajes procesados: identificador del mensaje de correo, para no reprocesarlo nunca.
- Movimientos bancarios: fecha, importe, concepto, hash para evitar duplicados.
- Vínculos entre movimientos y facturas: relación de muchos a muchos con el importe asignado en cada vínculo.
- Reglas: asociaciones aprendidas, por ejemplo que cierto texto de concepto bancario corresponde siempre a cierto proveedor.
- Versión del esquema, para las migraciones.

Campos fijos de una factura: proveedor, NIF, número de factura, fecha de emisión, fecha de vencimiento, base imponible, IVA, total, moneda, tipo de documento, categoría, estado de revisión, nivel de confianza, hash del PDF, ruta del archivo e identificador del mensaje de correo origen.

Todo lo demás, como forma de pago o recargo de equivalencia, va en el campo JSON hasta que los betas indiquen qué merece columna propia.

El Excel de contabilidad se genera o actualiza a partir de SQLite. Nunca es la base de datos.

## 8. Conciliación bancaria

Sin automatizar el acceso a ningún banco: el usuario importa ficheros de extracto.

Formatos soportados:

- Norma 43 de la AEB, fichero de texto de ancho fijo que casi todos los bancos españoles ofrecen.
- CAMT.053 de ISO 20022, el equivalente XML moderno.
- CSV o Excel genérico, donde el usuario mapea las columnas una sola vez. El mapeo se guarda por banco.

Cada movimiento importado se guarda con un hash para que reimportar el mismo extracto no duplique nada.

Emparejamiento automático: un conciliador propone vínculos comparando importe, cercanía de la fecha al vencimiento, y coincidencias de proveedor o número de factura en el concepto. Lo que case con alta confianza se marca solo y lo dudoso va a una pantalla de revisión.

Emparejamiento manual: el usuario selecciona un movimiento, marca las facturas que lo componen, y la app comprueba si la suma cuadra. Si hay diferencia, por ejemplo por un abono o un descuento, permite guardar igualmente y anota la diferencia como pendiente.

Casos que el diseño debe admitir desde el principio: un pago que cubre varias facturas, una factura pagada en varios plazos y los abonos, que restan. Por eso la relación es de muchos a muchos. Los detalles finos se cierran con casos reales en los betas.

## 9. Dashboard e interfaz

Interfaz web local que lee exclusivamente de SQLite. Pantallas previstas:

- Panel principal con gasto por mes, por categoría y por proveedor, vencimientos próximos y facturas pendientes de pago.
- Bandeja de revisión: facturas con baja confianza o campos dudosos, donde el usuario corrige categoría, proveedor o importes antes de confirmar.
- Listado de facturas con filtros y búsqueda, y botón para abrir el PDF archivado.
- Conciliación bancaria: importación de extractos, propuestas automáticas y marcado manual.
- Configuración: cuentas de correo, remitentes, carpeta de salida, clave del modelo, reglas por proveedor.
- Exportación a Excel de contabilidad básica.

## 10. Seguridad

- Contraseñas de correo, tokens OAuth y clave de la API se guardan en el almacén de credenciales del sistema, que en Windows es el Credential Manager, accesible con la librería keyring. Nunca en archivos de texto ni en SQLite.
- La interfaz web escucha solo en localhost.
- Privacidad: en el nivel gratuito de Google AI Studio los datos enviados pueden usarse para mejorar los modelos. La app lo avisa en la configuración y permite cambiar de proveedor o usar una clave de pago.
- Las instalaciones son independientes: no hay datos de clientes en ningún servidor del desarrollador.

## 11. Actualizaciones

- Código versionado en GitHub y releases publicadas con ejecutables para cada sistema.
- La app consulta periódicamente la API de releases y, si hay una versión nueva, ofrece instalarla desde el propio programa.
- En Windows un ejecutable no puede sobrescribirse mientras corre, así que un actualizador independiente espera al cierre, reemplaza los archivos y relanza la app.
- Con repositorio público no se necesitan credenciales. Con repositorio privado habría que gestionar un token, lo que complica la distribución.
- Verificación de integridad obligatoria: hash publicado en la release y comprobado antes de instalar.
- Migraciones de base de datos con número de versión del esquema, y copia automática de la base antes de migrar.

## 12. Fases de desarrollo y puntos de revisión

Cada fase termina con un punto de revisión antes de pasar a la siguiente.

**Fase 0. Preparación con casos reales.** Reunir 30 a 50 facturas reales de proveedores variados, anonimizadas si hace falta, y extractos bancarios de dos o tres bancos. Revisión: con esa muestra se fija la lista inicial de categorías y se comprueba que el modelo extrae bien los campos antes de construir nada más.

**Fase 1. Núcleo de procesado.** Script que, dado un PDF, extrae texto u OCR, llama al modelo, valida el JSON y devuelve los datos. Sin correo ni interfaz. Revisión: porcentaje de campos correctos sobre la muestra y decisión sobre el umbral de confianza.

**Fase 2. Base de datos y archivo.** Esquema SQLite inicial, detección de duplicados por hash, carpeta de salida con la estructura fija y escritura segura. Revisión: procesar la muestra completa y comprobar carpetas, nombres y filas.

**Fase 3. Correo.** Conexión IMAP genérica con varias cuentas, detección de candidatas y reglas por remitente. Revisión: una semana de uso real en un buzón propio sin perder ni duplicar facturas.

**Fase 4. Interfaz web y dashboard.** FastAPI, listado, bandeja de revisión, configuración y métricas principales. Revisión: sesión de uso con una persona no técnica de una farmacia.

**Fase 5. Empaquetado, credenciales y actualizador.** Ejecutable para Windows, almacén de credenciales, releases en GitHub y autoactualización con verificación. Revisión: instalación limpia en un Windows ajeno y actualización de una versión a otra.

**Fase 6. Conciliación bancaria.** Importadores de Norma 43, CAMT.053 y CSV con mapeo, emparejamiento automático y manual muchos a muchos. Revisión: conciliar un mes real completo, incluyendo pagos agrupados y abonos.

**Fase 7. Beta cerrado.** Dos o tres farmacias usando la app unas semanas. Revisión: con los datos reales se redefine el esquema definitivo, las categorías y el dashboard.

**Fase 8. OAuth de Gmail y Outlook.** Autenticación moderna para esos proveedores. Se deja después del beta de IMAP porque requiere registrar aplicaciones en Google y Microsoft y puede exigir verificación. Revisión: conexión real con una cuenta de cada tipo.

**Fase 9. Subida directa a Google Drive por API.** Segunda capa de almacenamiento para quien no tenga el cliente de escritorio. Revisión: subida con estructura de carpetas correcta en una carpeta compartida.

## 13. Decisiones abiertas

- Lista final de categorías y campos extra: se decide con los betas.
- Repositorio público o privado en GitHub, porque condiciona el actualizador.
- Licencia y modelo de distribución del software.
- Estrategia de OCR para facturas escaneadas: local o mediante el propio modelo con visión.
- Umbral de confianza por defecto para confirmar facturas sin revisión manual.
- Si el OAuth de Gmail y Outlook se adelanta, dado que muchas farmacias usan esos proveedores.
