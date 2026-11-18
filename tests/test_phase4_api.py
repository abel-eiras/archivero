"""Tests de Fase 4: API REST FastAPI, Dashboard, Bandeja de Revisión y Muestra de Fase 0."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def test_full_web_api_and_phase0_demo_flow(tmp_path: Path):
    app = create_app(
        db_path=tmp_path / "web_test.sqlite",
        backup_dir=tmp_path / "backups",
        start_background_poller=False,
    )
    app.state.db.set_settings({"carpeta_salida": str(tmp_path / "archivo_facturas")})
    client = TestClient(app)

    # 1. Healthcheck y página principal HTML
    r_health = client.get("/health")
    assert r_health.status_code == 200
    r_index = client.get("/")
    assert r_index.status_code == 200
    assert "Archivero" in r_index.text

    # 2. Cargar datos de muestra de Fase 0 desde el endpoint
    r_demo = client.post("/api/demo/load-sample-data")
    assert r_demo.status_code == 200
    demo_data = r_demo.json()
    assert demo_data["facturas_insertadas"] == 7
    assert demo_data["movimientos_insertados"] == 5

    # 3. Consultar métricas del Dashboard
    r_dash = client.get("/api/dashboard")
    assert r_dash.status_code == 200
    dash = r_dash.json()
    assert dash["resumen"]["total_facturas"] == 7
    # La factura de FarmaPack con descuadre debe estar en la bandeja de revisión
    assert dash["resumen"]["pendientes_revision"] >= 1

    # 4. Bandeja de revisión: corregir el importe de la factura con descuadre y confirmarla
    r_rev = client.get("/api/invoices?estado_revision=pendiente_revision")
    assert r_rev.status_code == 200
    rev_items = r_rev.json()["items"]
    assert len(rev_items) >= 1
    dudosa = rev_items[0]

    r_fix = client.put(
        f"/api/invoices/{dudosa['id']}",
        json={
            "base_imponible": 100.00,
            "iva": 21.00,
            "total": 121.00,
            "estado_revision": "confirmada",
            "aprender_regla_proveedor": True,
        },
    )
    assert r_fix.status_code == 200
    fixed_inv = r_fix.json()
    assert fixed_inv["estado_revision"] == "confirmada"
    assert fixed_inv["total"] == 121.00
    assert fixed_inv["errores_validacion"] == []

    # 5. Abrir PDF archivado
    r_pdf = client.get(f"/api/invoices/{dudosa['id']}/pdf")
    assert r_pdf.status_code == 200
    assert r_pdf.headers["content-type"] == "application/pdf"
    assert r_pdf.content.startswith(b"%PDF")

    # 6. Exportar Excel de contabilidad
    r_xlsx = client.get("/api/export/excel")
    assert r_xlsx.status_code == 200
    assert "spreadsheetml" in r_xlsx.headers["content-type"]
    assert len(r_xlsx.content) > 1000
