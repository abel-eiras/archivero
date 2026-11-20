# Archivero — Gestor Automático de Facturas y Banco para Farmacias

Repositorio oficial: **[https://github.com/abel-eiras/archivero](https://github.com/abel-eiras/archivero)** · **Licencia:** [MIT](LICENSE)

**Archivero** es un programa sencillo que se instala en el ordenador de tu farmacia. Se encarga de vigilar tu correo electrónico, detectar las facturas que te envían tus proveedores en PDF, leer sus datos e importes automáticamente, guardarlas ordenadas por carpetas para tu gestoría y comprobar cuáles están ya pagadas en el banco.

> **Pensado para farmacéuticos sin conocimientos informáticos:** No necesitas saber nada de programación ni de contabilidad complicada. Todo funciona en tu propio ordenador, con botones claros en español y sin cuotas de servidores externos.

---

## Índice de esta Guía

1. [¿Qué hace este programa por tu farmacia?](#1-qué-hace-este-programa-por-tu-farmacia)
2. [Cómo instalar el programa (Windows y Mac)](#2-cómo-instalar-el-programa-windows-y-mac)
3. [Configuración inicial en 5 minutos (solo el primer día)](#3-configuración-inicial-en-5-minutos-solo-el-primer-día)
4. [Cómo usar el programa en el día a día](#4-cómo-usar-el-programa-en-el-día-a-día)
5. [Cómo quedan ordenadas las carpetas para tu gestoría](#5-cómo-quedan-ordenadas-las-carpetas-para-tu-gestoría)
6. [Seguridad, privacidad y preguntas frecuentes](#6-seguridad-privacidad-y-preguntas-frecuentes)
7. [Información técnica para instaladores y desarrolladores](#7-información-técnica-para-instaladores-y-desarrolladores)

---

## 1. ¿Qué hace este programa por tu farmacia?

- **Recoge tus facturas solo:** Revisa el correo de la farmacia (puedes poner más de uno, por ejemplo el de administración y el del titular) y descarga los PDFs de facturas nuevas sin tocar ni borrar tus correos.
- **También admite facturas sueltas:** Si escaneas una factura en papel o la descargas de la web de un proveedor, puedes añadirla con el botón **"+ Añadir Facturas en PDF"**.
- **Lee todos los datos e importes:** Extrae el nombre del proveedor, NIF, número de factura, fecha de factura, fecha de vencimiento, Base sin IVA, IVA, **Recargo de Equivalencia** e **IRPF**, y comprueba que la suma cuadre al céntimo con el Total.
- **No duplica nunca nada:** Si un proveedor te envía dos veces el mismo PDF (aunque le cambie el nombre al archivo), el programa reconoce que es el mismo documento y no lo mete dos veces.
- **Te avisa si algo no cuadra:** Si una factura viene borrosa o la suma de Base + IVA no coincide con el Total, la deja apartada en la **Bandeja de Revisión** para que la confirmes viendo el PDF al lado.
- **Ordena los PDFs para tu gestoría:** Crea él solo las carpetas por **Año → Mes → Categoría → Proveedor** y les pone un nombre limpio (`Fecha_Proveedor_NumeroFactura.pdf`).
- **Cruza las facturas con el banco:** Subes el archivo de movimientos de tu banco (Norma 43, Excel, CSV o XML) y el programa marca qué facturas están ya pagadas, incluyendo remesas que agrupan varias facturas y abonos que restan.
- **Genera el Excel contable en un clic:** Descarga un archivo Excel (`.xlsx`) con todas las facturas, desgloses de IVA/Recargo, gastos por mes y estado de pago listo para tu gestoría.

---

## 2. Cómo instalar el programa (Windows y Mac)

### Opción A: Con el programa descargable (Recomendado para farmacias)

1. Entra en la página de descargas del proyecto: **[https://github.com/abel-eiras/archivero/releases](https://github.com/abel-eiras/archivero/releases)**.
2. En la versión más reciente (arriba del todo), haz clic en el archivo para tu ordenador:
   - En **Windows**: descarga `Archivero.exe`.
   - En **Mac**: descarga el archivo para Mac (`Archivero-Mac`).
3. Haz doble clic sobre el programa descargado.
4. Abre tu navegador de Internet habitual (Chrome, Edge, Safari) y entra en la dirección:
   👉 **`http://127.0.0.1:8000`**

*(Consejo: guarda esa dirección en la barra de Favoritos / Marcadores de tu navegador para abrirla siempre con un clic).*

---

## 3. Configuración inicial en 5 minutos (solo el primer día)

Al abrir el programa, haz clic en el menú izquierdo en **Configuración Fácil**. Solo tienes que rellenar tres cosas:

### Paso 1: Elegir la carpeta para tu gestoría
En el apartado **1. Lectura de Facturas y Carpeta para la Gestoría**:
- En la casilla **"Carpeta donde se archivarán las facturas para tu gestoría"**, escribe o pega la ruta de la carpeta de tu ordenador donde quieres que se vayan guardando los PDFs ordenados.
- **Truco para no tener que enviar correos a la gestoría:** Si tienes instalado en el ordenador *Google Drive para escritorio*, *OneDrive* o *Dropbox*, elige una carpeta que esté dentro de tu Drive o Dropbox compartida con la gestoría. Así, cada vez que el programa archive una factura, a tu gestoría le aparecerá automáticamente en su ordenador.

### Paso 2: Configurar la Lectura Inteligente de Facturas (Google, OpenAI, Claude o Modo sin Internet)

#### ¿Por qué el programa pide una "Clave API" y no sirve la suscripción mensual de ChatGPT, Claude o Gemini?
Es muy importante conocer esta diferencia:
- Las **suscripciones mensuales de chat** (las versiones de pago de *ChatGPT*, *Claude* o *Gemini*) son para que una persona chatee dentro de su página web o aplicación. No dan acceso a que un programa automático lea documentos por su cuenta.
- Para que un programa de ordenador lea documentos automáticamente, estas empresas ofrecen lo que llaman **Clave de conexión (API Key)**.
- **La gran ventaja para tu farmacia:** Con una Clave API **no pagas ninguna cuota mensual fija**. Google ofrece una modalidad gratuita con límites de uso, y en las modalidades de pago por uso solo pagas por las facturas que procesas: para el volumen normal de una farmacia suelen ser **unos céntimos al mes**. Los precios los fija cada proveedor y pueden cambiar; consúltalos en su web antes de elegir.

#### Comparativa rápida de opciones disponibles en el programa

| Proveedor en el programa | Coste para una farmacia | ¿Mis facturas son privadas? | ¿Lee facturas escaneadas / fotos? |
| :--- | :--- | :--- | :--- |
| **Google AI Studio (Plan Gratuito)** | **Gratis** (con límites diarios de uso que fija Google) | Google avisa de que puede usar los textos para mejorar sus sistemas | **Sí** (visión directa del PDF) |
| **Google AI Studio (Con facturación activada)** | **Unos céntimos** al mes, según uso (sin cuota mensual) | **100% Privado por contrato** (nunca se usan para entrenar) | **Sí** (visión directa del PDF) |
| **OpenAI (API)** | **Unos céntimos** al mes, según uso (pago por adelantado de saldo) | **100% Privado por contrato** | **Sí** (con OCR / texto) |
| **Anthropic (API de Claude)** | **Unos céntimos** al mes, según uso (pago por adelantado de saldo) | **100% Privado por contrato** | **Sí** (lectura nativa de PDF) |
| **Modo básico sin Internet** | **0,00 € (Gratis)** | **100% en tu ordenador** (nada sale a Internet) | Solo facturas digitales con texto claro |

#### Cómo conseguir tu clave paso a paso según el proveedor que prefieras

1. **Opción recomendada: Google AI Studio — Gratis o privado por unos céntimos**
   - Entra con cualquier cuenta de Google/Gmail en **[https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)**.
   - Haz clic en el botón azul **"Create API key"** (Crear clave de API).
   - Copia el código que empieza por `AIza...`.
   - En el programa, ve a **Configuración Fácil**, elige *Google AI Studio*, pega la clave, pulsa **"Probar Clave"** y haz clic en **Guardar Opciones**.
   - *(Opcional para máxima privacidad):* Si en la misma web de Google AI Studio pulsas en *"Set up Billing"* (Configurar facturación), tu clave pasa automáticamente al nivel privado empresarial donde Google garantiza que ningún dato de la farmacia se revisa ni entrena modelos, pagando solo unos céntimos al año.

2. **Opción alternativa: OpenAI (creadores de ChatGPT)**
   - Entra en **[https://platform.openai.com/api-keys](https://platform.openai.com/api-keys)** y crea una cuenta o inicia sesión.
   - En el menú *Settings → Billing*, añade un poco de saldo (para una farmacia, el gasto suele ser de unos céntimos al mes).
   - En *API Keys*, pulsa **"Create new secret key"**, copia la clave que empieza por `sk-...`, elige *OpenAI* en **Configuración Fácil** y pégala.

3. **Opción alternativa: Anthropic (creadores de Claude)**
   - Entra en **[https://console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)**.
   - En *Billing* carga un poco de saldo y en *API Keys* pulsa **"Create Key"**.
   - Copia la clave que empieza por `sk-ant-...`, selecciona *Anthropic (Claude API)* en **Configuración Fácil** y pégala.

### Paso 3: Conectar el correo de la farmacia
En el apartado **2. Correo Electrónico de la Farmacia**:
1. Pon un nombre para reconocerlo (por ejemplo: `Correo General Farmacia`).
2. Escribe tu dirección de correo electrónico.
3. Elige el tipo de cuenta:
   - Si es un correo del Colegio de Farmacéuticos o de tu propio dominio: elige la primera opción, abre *"Datos del servidor de correo"* y pon el servidor entrante que te dio tu proveedor (por ejemplo `imap.tuservidor.es`, puerto `993`).
   - Si es **Gmail** o **Outlook/Hotmail**: elige la opción correspondiente e introduce tu contraseña de aplicación o autorización.
4. Pulsa **Guardar Cuenta de Correo**.

---

## 4. Cómo usar el programa en el día a día

### A. Probar el programa el primer día con datos de ejemplo
Arriba a la derecha verás el botón **"Cargar Datos de Ejemplo"**. Si lo pulsas, el programa creará varias facturas de prueba y movimientos bancarios para que puedas tocar todos los botones y ver cómo funciona sin miedo.

### B. Revisar las facturas dudosas (`Bandeja de Revisión`)
- Cuando el programa tiene una seguridad alta (por defecto más del **85%**) y comprueba que `Base + IVA + Recargo - IRPF = Total`, da la factura por buena automáticamente.
- Si ve que la suma no cuadra o algún dato falta, verás un aviso rojo al lado de **Bandeja de Revisión** en el menú izquierdo.
- Entra ahí y pulsa **"Corregir / Ver PDF"**: se abrirá una ventana con los datos a la izquierda y el documento PDF original a la derecha. Corrige el número que haga falta y pulsa **"Confirmar y Guardar Factura"**.

### C. Cuadrar las facturas con el banco (`Cuadrar con el Banco`)
1. Entra en la web de tu banco y descarga los movimientos del mes (en formato **Norma 43**, **Excel `.xlsx`**, **CSV** o **XML**).
2. En el programa, entra en **Cuadrar con el Banco**, selecciona ese archivo en el cuadro **1. Subir Movimientos del Banco** y pulsa **"Subir Extracto y Cruzar Pagos"**.
3. El programa hará tres cosas:
   - **Pagos claros:** Si el importe, la fecha de vencimiento y el proveedor coinciden claramente (incluso cuando un solo cargo del banco junta varias facturas y descuenta un abono), los marcará como **Cuadrado con factura** automáticamente.
   - **Coincidencias probables:** Si encuentra una factura que parece corresponder pero tiene alguna duda, te mostrará el botón verde **Confirmar** o **No es esta**.
   - **Unir a mano (remesas especiales, plazos o descuentos):** Si quieres unir tú mismo un cargo del banco con una o varias facturas, pulsa **"Unir a mano"** en la fila del movimiento bancario y marca con un tic las facturas en el cuadro superior derecho. Aunque haya una pequeña diferencia (por ejemplo, por un descuento por pronto pago), el programa te dejará guardarlo anotando la diferencia.

### D. Descargar el Excel para tu gestoría
En cualquier momento, pulsa el botón **"Descargar Excel para Gestoría"** de la barra superior. Se descargará un archivo Excel con 4 pestañas ya preparadas:
1. `Facturas Recibidas` (con todas las columnas contables y totales sumados).
2. `Resumen Mensual y Categorías`.
3. `Por Proveedor`.
4. `Conciliación Bancaria`.

---

## 5. Cómo quedan ordenadas las carpetas para tu gestoría

El programa organiza los archivos PDF siguiendo siempre esta estructura fija de carpetas:

```text
archivo_facturas/
└── 2026/
    └── 11-Noviembre/
        ├── compras_mercancia/
        │   └── Distribuidora_Farmaceutica_Norte_S_Coop/
        │       └── 2026-11-02_Distribuidora_Farmaceutica_Norte_S_Coop_FRA-2026-1001.pdf
        ├── suministros/
        │   └── Comercializadora_Electrica_Peninsular_S_A/
        │       └── 2026-11-10_Comercializadora_Electrica_Peninsular_S_A_FRA-2026-2050.pdf
        └── alquiler/
            └── Arrendamientos_Urbanos_Goya_S_L/
                └── 2026-11-01_Arrendamientos_Urbanos_Goya_S_L_ALQ-2026-011.pdf
```

---

## 6. Seguridad, privacidad y preguntas frecuentes

- **¿Mis contraseñas están seguras?** Sí. Las contraseñas del correo y las claves de la IA nunca se guardan en la base de datos ni en archivos de texto legibles: se guardan en el almacén de seguridad del propio sistema operativo (el *Administrador de credenciales de Windows* o el *Llavero de Mac*). Si ese almacén no está disponible, se guardan en un archivo cifrado dentro de la carpeta de datos del programa, que solo Archivero sabe abrir.
- **¿Se hacen copias de seguridad?** Sí. El programa guarda una copia de la base de datos una vez al día (conserva las últimas 14) y otra antes de cada cambio de versión de la base de datos. También puedes guardar una copia cuando quieras desde Configuración.
- **¿Pueden entrar otros ordenadores de la farmacia a ver mis facturas?** No. El programa escucha únicamente en `127.0.0.1` (`localhost`), por lo que solo se puede abrir desde el mismo ordenador donde está funcionando.
- **¿Qué ocurre si uso la clave gratuita de Google AI Studio?** En el nivel gratuito de Google AI Studio, Google indica en sus condiciones que puede utilizar los datos procesados para mejorar sus modelos. El programa te avisa claramente en la pantalla de configuración y te permite usar una clave de pago confidencial o el modo de lectura local sin Internet.
- **¿Qué pasa si borro o muevo sin querer un PDF de la carpeta?** Nada grave: el panel principal, las estadísticas y el Excel leen siempre de la base de datos interna (`SQLite`), por lo que tus cuentas nunca se pierden aunque alguien mueva los archivos de la carpeta de la gestoría.

---

## 7. Información técnica para instaladores y desarrolladores

### Requisitos para ejecutar desde código fuente
- Python 3.11 o superior.

### Instalación y arranque en consola

```bash
git clone https://github.com/abel-eiras/archivero.git
cd archivero
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main
```

La interfaz quedará disponible en `http://127.0.0.1:8000`.

### Ejecutar los tests automatizados

```bash
.venv/bin/python -m pytest -v
```

### Compilar ejecutables independientes (Windows / macOS)

- **Windows**:
  ```bat
  packaging\build_windows.bat
  ```
  Genera `dist\Archivero.exe`, el actualizador independiente `dist\ArchiveroUpdater.exe` y el archivo de firmas `dist\SHA256SUMS.txt`.
- **macOS / Linux**:
  ```bash
  bash packaging/build_mac.sh
  ```
- **Docker**:
  ```bash
  docker compose up --build -d
  ```

---

## 8. Licencia

Este proyecto se distribuye como software libre bajo la **Licencia MIT**. Consulta el archivo [LICENSE](LICENSE) para ver el texto completo.
