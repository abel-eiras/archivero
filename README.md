# FarmaFacturas — Gestor Local de Facturas por Correo y Conciliación Bancaria para Farmacias

Aplicación web local (`localhost`), diseñada para instalarse en el ordenador de cada farmacia, que vigila uno o varios buzones de correo, detecta facturas de proveedores recibidas en PDF, las clasifica y extrae sus datos con IA (con validación aritmética y de fechas), las archiva en una estructura fija de carpetas para la gestoría y permite conciliarlas con los extractos bancarios.

---

## Principios de Arquitectura Implementados

1. **SQLite como única fuente de verdad (`app/db/database.py`)**: Todo el dashboard, listados, conciliación y exportación a Excel leen exclusivamente de SQLite (modo `WAL`, claves foráneas activas y copia de seguridad automática antes de cada migración de esquema).
2. **Copia archivada resiliente para la gestoría (`app/services/storage.py`)**:
   - Estructura fija: `{Año}/{Mes}/{Categoría}/{Proveedor}/{Fecha}_{Proveedor}_{NumeroFactura}.pdf`.
   - Escritura primero en carpeta temporal (`.tmp_staging`) y movimiento atómico con reintentos ante fallos o lentitud de unidades montadas (Google Drive Desktop, OneDrive, Dropbox).
   - **Dos capas bajo la misma interfaz (`StorageBackend`)**:
     - **Capa 1 (`LocalFolderStorage`)**: escritura en carpeta local o unidad sincronizada.
     - **Capa 2 (`GoogleDriveStorage`)**: subida directa mediante Google Drive API v3 con OAuth2.
   - Si alguien mueve o renombra un PDF fuera de la aplicación, el panel sigue funcionando sin fallos y el visor avisa de que el archivo no se encuentra en la ruta registrada.
3. **Procesado con Modelo de Lenguaje + Validación (`app/services/llm_processor.py`)**:
   - Proveedor por defecto: **Google AI Studio (Gemini API)** con soporte multimodal nativo (`application/pdf`) para facturas escaneadas sin capa de texto, configurable también para **OpenAI / Compatible** (OpenAI, Groq, Ollama local) y con **Extractor Heurístico Local** de respaldo (reconoce NIFs, fechas, Base Imponible, IVA, Recargo de Equivalencia e IRPF sin necesidad de red).
   - Validador estricto (`validate_invoice_extraction`): comprueba que `Base Imponible + IVA + Recargo de Equivalencia - IRPF == Total` (±0,05 €), coherencia de fechas (`vencimiento >= emisión`) y campos obligatorios.
   - Si la confianza supera el umbral configurable (por defecto `85%`) y no hay errores de validación, queda `confirmada`; en caso contrario pasa a la **Bandeja de Revisión**.
4. **Módulo de Correo (`app/services/mail_poller.py`)**:
   - Soporta múltiples cuentas por instalación (ej. administración y titular).
   - Conectores **IMAP genérico** y **OAuth2 (XOAUTH2) para Gmail y Outlook**.
   - Deduplicación estricta en SQLite (`mensajes_procesados` por `Message-ID`) y por hash `SHA-256` del binario del PDF.
5. **Conciliación Bancaria (`app/services/bank_parsers.py` y `app/services/reconciliation.py`)**:
   - Importadores para **Norma 43 (AEB)**, **CAMT.053 (ISO 20022 XML)** y **CSV / Excel (`.xlsx`)** con mapeo de columnas guardado por banco.
   - Conciliador automático 1:1 y 1:N (pagos agrupados y abonos que restan).
   - Conciliación manual muchos-a-muchos (1 pago para varias facturas, 1 factura en varios plazos, abonos y registro de diferencias pendientes con aprendizaje automático de reglas `concepto -> proveedor`).
6. **Seguridad y Actualizaciones (`app/services/credentials.py`, `app/services/updater_service.py`, `updater.py`)**:
   - Contraseñas, tokens OAuth y claves de API se guardan en el almacén seguro del sistema (`keyring` / Credential Manager o bóveda cifrada Fernet), nunca en SQLite.
   - Servidor vinculado a `127.0.0.1` (`localhost`).
   - Actualizador independiente (`updater.py`) con verificación obligatoria de hash `SHA-256` antes de reemplazar el ejecutable.

---

## Puesta en Marcha Rápida

### 1. Arrancar la aplicación web local

```bash
.venv/bin/python -m app.main
```

Abre en el navegador: **`http://127.0.0.1:8000`**

> En la barra superior dispones del botón **"Cargar Muestra (Fase 0)"**, que genera e ingiere automáticamente 7 facturas PDF realistas de proveedores de farmacia (Cofares, Bidafarma, Abono Bidafarma, Iberdrola, Alquiler con IRPF, Asesoría y una factura con descuadre aritmético para probar la Bandeja de Revisión) junto con extractos bancarios en **Norma 43**, **CAMT.053 XML** y **CSV**.

### 2. Ejecutar el procesador de Fase 1 por línea de comandos

```bash
.venv/bin/python -m app.services.llm_processor ruta/a/factura.pdf
```

### 3. Ejecutar la suite de tests automatizados

```bash
.venv/bin/python -m pytest -v
```

### 4. Empaquetado como ejecutable (Windows / macOS) o Docker

- **Windows**: `packaging\build_windows.bat` (genera `dist\FarmaFacturas.exe`, `dist\FarmaFacturasUpdater.exe` y `dist\SHA256SUMS.txt`).
- **macOS / Linux**: `bash packaging/build_mac.sh`
- **Docker**: `docker compose up --build -d`
