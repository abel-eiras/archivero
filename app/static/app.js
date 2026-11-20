/* Lógica de cliente de Archivero (100% local, pensada para uso sencillo en farmacia) */

const state = {
  categorias: [],
  tiposDocumento: {},
  selectedBankTx: null,
  unpaidInvoices: [],
  bankTransactionsById: {},
  currentEditingInvoice: null,
};

const RULE_TYPE_LABELS = {
  remitente_categoria: "Correo del remitente",
  remitente_proveedor: "Correo del remitente",
  nif_categoria: "NIF o nombre de proveedor",
  concepto_proveedor: "Concepto del banco",
};

const MAIL_TYPE_LABELS = {
  imap: "Correo con contraseña",
  oauth_gmail: "Gmail (Google)",
  oauth_outlook: "Outlook / Hotmail",
};

function fmtEur(val) {
  const num = Number(val || 0);
  return num.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";
}

function showToast(msg) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function categoryName(code) {
  const found = state.categorias.find((c) => c.codigo === code);
  return found ? found.nombre : code || "Otros";
}

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = `Error (${resp.status})`;
    try {
      const errJson = await resp.json();
      detail = errJson.detail || JSON.stringify(errJson);
    } catch (_) {}
    throw new Error(detail);
  }
  return resp.json();
}

/* =========================================================
   Navegación por pestañas
   ========================================================= */
function setupTabs() {
  const buttons = document.querySelectorAll(".nav-btn");
  const panels = document.querySelectorAll(".tab-panel");
  const titleMap = {
    "tab-dashboard": "Resumen General",
    "tab-revision": "Bandeja de Revisión",
    "tab-facturas": "Todas las Facturas",
    "tab-conciliacion": "Cuadrar con el Banco",
    "tab-configuracion": "Configuración Fácil",
    "tab-ayuda": "Ayuda Paso a Paso",
  };

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      buttons.forEach((b) => b.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(target).classList.add("active");
      document.getElementById("current-screen-title").textContent = titleMap[target] || "Archivero";

      if (target === "tab-dashboard") loadDashboard();
      if (target === "tab-revision") loadReviewInbox();
      if (target === "tab-facturas") loadInvoicesList();
      if (target === "tab-conciliacion") loadBankReconciliation();
      if (target === "tab-configuracion") loadConfiguration();
    });
  });
}

/* =========================================================
   1. Resumen General (Dashboard)
   ========================================================= */
async function loadDashboard() {
  try {
    const data = await apiFetch("/api/dashboard");
    state.categorias = (data.catalogos && data.catalogos.categorias) || [];
    state.tiposDocumento = (data.catalogos && data.catalogos.tipos_documento) || {};
    populateCategorySelects();

    const r = data.resumen || {};
    document.getElementById("kpi-gasto-total").textContent = fmtEur(r.gasto_total);
    document.getElementById("kpi-base-iva").textContent = `Base sin IVA: ${fmtEur(r.base_total)} · IVA: ${fmtEur(r.iva_total)}`;
    document.getElementById("kpi-total-facturas").textContent = String(r.total_facturas || 0);
    document.getElementById("kpi-pendientes-revision").textContent = String(r.pendientes_revision || 0);
    document.getElementById("kpi-importe-impagado").textContent = fmtEur(r.importe_pendiente_pago);
    document.getElementById("kpi-facturas-impagadas").textContent = `${r.facturas_impagadas || 0} facturas pendientes de pago`;

    const badgeRev = document.getElementById("badge-pendientes-revision");
    if (r.pendientes_revision > 0) {
      badgeRev.style.display = "inline-block";
      badgeRev.textContent = String(r.pendientes_revision);
    } else {
      badgeRev.style.display = "none";
    }

    const propCount = (data.conciliacion && data.conciliacion.propuestos) || 0;
    const badgeBank = document.getElementById("badge-propuestas-banco");
    if (propCount > 0) {
      badgeBank.style.display = "inline-block";
      badgeBank.textContent = String(propCount);
    } else {
      badgeBank.style.display = "none";
    }

    renderBarList(
      "chart-por-mes",
      (data.gasto_por_mes || []).map((x) => ({ label: x.mes, count: x.cantidad, total: x.total }))
    );
    renderBarList(
      "chart-por-categoria",
      (data.gasto_por_categoria || []).map((x) => ({
        label: categoryName(x.categoria),
        count: x.cantidad,
        total: x.total,
      }))
    );
    renderBarList(
      "chart-por-proveedor",
      (data.gasto_por_proveedor || []).map((x) => ({
        label: x.proveedor,
        count: x.cantidad,
        total: x.total,
      }))
    );

    const tbodyVto = document.getElementById("tbody-vencimientos");
    tbodyVto.innerHTML = "";
    (data.vencimientos_proximos || []).forEach((inv) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(inv.fecha_vencimiento || "-")}</strong></td>
        <td>${escapeHtml(inv.proveedor_nombre)}</td>
        <td>${escapeHtml(inv.numero_factura)}</td>
        <td class="num">${fmtEur(inv.total)}</td>
        <td><span class="badge badge-warning">Pendiente</span></td>
      `;
      tbodyVto.appendChild(tr);
    });
    if (!data.vencimientos_proximos || data.vencimientos_proximos.length === 0) {
      tbodyVto.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">No hay facturas próximas a vencer en los próximos 15 días.</td></tr>`;
    }

    const tbodyPend = document.getElementById("tbody-pendientes-pago");
    tbodyPend.innerHTML = "";
    (data.pendientes_pago || []).forEach((inv) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td>${escapeHtml(inv.proveedor_nombre)}</td>
        <td>${escapeHtml(inv.numero_factura)}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.total)}</td>
      `;
      tbodyPend.appendChild(tr);
    });
    if (!data.pendientes_pago || data.pendientes_pago.length === 0) {
      tbodyPend.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">Todas las facturas registradas están ya pagadas.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando el resumen: " + err.message);
  }
}

function renderBarList(containerId, items) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  if (!items || items.length === 0) {
    container.innerHTML = `<div style="color:#64748b;font-size:0.84rem;">Todavía no hay facturas registradas.</div>`;
    return;
  }
  const maxAbs = Math.max(...items.map((i) => Math.abs(Number(i.total || 0))), 1);
  items.forEach((item) => {
    const pct = Math.min(100, Math.round((Math.abs(Number(item.total || 0)) / maxAbs) * 100));
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="bar-item-head">
        <span><strong>${escapeHtml(item.label)}</strong> <small style="color:#64748b;">(${item.count})</small></span>
        <span><strong>${fmtEur(item.total)}</strong></span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;"></div></div>
    `;
    container.appendChild(div);
  });
}

function populateCategorySelects() {
  const selects = [
    document.getElementById("filter-categoria"),
    document.getElementById("edit-categoria"),
    document.getElementById("rule-categoria"),
  ];
  selects.forEach((sel, idx) => {
    if (!sel) return;
    const currentVal = sel.value;
    const firstOpt =
      idx === 0
        ? `<option value="">Todas las categorías</option>`
        : idx === 2
        ? `<option value="">(Dejar la que detecte)</option>`
        : "";
    sel.innerHTML =
      firstOpt +
      state.categorias
        .map((c) => `<option value="${escapeHtml(c.codigo)}">${escapeHtml(c.nombre)}</option>`)
        .join("");
    if (currentVal) sel.value = currentVal;
  });

  const docSel = document.getElementById("edit-tipo-documento");
  if (docSel) {
    const cur = docSel.value;
    docSel.innerHTML = Object.entries(state.tiposDocumento)
      .map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(v)}</option>`)
      .join("");
    if (cur) docSel.value = cur;
  }
}

/* =========================================================
   2. Bandeja de Revisión
   ========================================================= */
async function loadReviewInbox() {
  try {
    const data = await apiFetch("/api/invoices?estado_revision=pendiente_revision");
    const tbody = document.getElementById("tbody-revision");
    tbody.innerHTML = "";

    (data.items || []).forEach((inv) => {
      const confPct = Math.round(Number(inv.nivel_confianza || 0) * 100);
      const motivos =
        (inv.errores_validacion || []).join(" · ") ||
        `Lectura con seguridad del ${confPct}% (requiere confirmación)`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td><strong>${escapeHtml(inv.proveedor_nombre || "Sin proveedor")}</strong></td>
        <td>${escapeHtml(inv.nif || "-")}</td>
        <td>${escapeHtml(inv.numero_factura || "-")}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.base_imponible)}</td>
        <td class="num">${fmtEur(inv.iva)}</td>
        <td class="num">${fmtEur(inv.total)}</td>
        <td>
          <span class="badge badge-warning">Revisar</span>
          <div style="font-size:0.76rem;color:#92400e;margin-top:0.2rem;">${escapeHtml(motivos)}</div>
        </td>
        <td style="white-space:nowrap;">
          <button class="btn btn-primary btn-sm" onclick="openInvoiceModal(${inv.id})">Corregir / Ver PDF</button>
          <button class="btn btn-success btn-sm" onclick="quickConfirmInvoice(${inv.id})">Dar por buena</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align:center;color:#64748b;padding:1.5rem;">¡Todo al día! No tienes ninguna factura pendiente de revisar.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando bandeja de revisión: " + err.message);
  }
}

async function quickConfirmInvoice(invoiceId) {
  try {
    await apiFetch(`/api/invoices/${invoiceId}/confirm`, { method: "POST" });
    showToast("Factura confirmada correctamente.");
    loadReviewInbox();
    loadDashboard();
  } catch (err) {
    showToast("Error confirmando factura: " + err.message);
  }
}

/* =========================================================
   3. Todas las Facturas
   ========================================================= */
async function loadInvoicesList() {
  try {
    const params = new URLSearchParams();
    const q = document.getElementById("filter-busqueda").value.trim();
    const mes = document.getElementById("filter-mes").value.trim();
    const cat = document.getElementById("filter-categoria").value;
    const rev = document.getElementById("filter-estado-revision").value;
    const pago = document.getElementById("filter-estado-pago").value;

    if (q) params.set("busqueda", q);
    if (mes) params.set("mes", mes);
    if (cat) params.set("categoria", cat);
    if (rev) params.set("estado_revision", rev);
    if (pago) params.set("estado_pago", pago);
    if (document.getElementById("filter-incluir-sustituidas").checked) params.set("incluir_sustituidas", "true");

    const data = await apiFetch(`/api/invoices?${params.toString()}`);
    const tbody = document.getElementById("tbody-facturas");
    tbody.innerHTML = "";

    (data.items || []).forEach((inv) => {
      let revBadge =
        inv.estado_revision === "confirmada"
          ? `<span class="badge badge-success">Confirmada</span>`
          : `<span class="badge badge-warning">Por revisar</span>`;
      if (inv.sustituida_por) {
        revBadge += ` <span class="badge badge-info">Sustituida por #${escapeHtml(inv.sustituida_por)}</span>`;
      }
      if (inv.sustituye_a) {
        revBadge += ` <span class="badge badge-info">Sustituye a #${escapeHtml(inv.sustituye_a)}</span>`;
      }

      let pagoBadge = `<span class="badge badge-danger">Pendiente</span>`;
      if (inv.estado_pago === "pagada") pagoBadge = `<span class="badge badge-success">Pagada</span>`;
      else if (inv.estado_pago === "parcial") pagoBadge = `<span class="badge badge-info">Pago parcial</span>`;

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(inv.fecha_emision || "-")}</td>
        <td>${escapeHtml(inv.fecha_vencimiento || "-")}</td>
        <td><strong>${escapeHtml(inv.proveedor_nombre)}</strong></td>
        <td>${escapeHtml(inv.nif)}</td>
        <td>${escapeHtml(inv.numero_factura)}</td>
        <td>${escapeHtml(categoryName(inv.categoria))}</td>
        <td class="num">${fmtEur(inv.base_imponible)}</td>
        <td class="num">${fmtEur(inv.iva)}</td>
        <td class="num">${fmtEur(inv.total)}</td>
        <td>${revBadge}</td>
        <td>${pagoBadge}</td>
        <td style="white-space:nowrap;">
          <button class="btn btn-secondary btn-sm" onclick="openPdfFile(${inv.id})">Ver PDF</button>
          <button class="btn btn-secondary btn-sm" onclick="openInvoiceModal(${inv.id})">Editar</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    if (!data.items || data.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:#64748b;padding:1.5rem;">No se encontraron facturas con esos criterios de búsqueda.</td></tr>`;
    }
  } catch (err) {
    showToast("Error listando facturas: " + err.message);
  }
}

async function openPdfFile(invoiceId) {
  try {
    const resp = await fetch(`/api/invoices/${invoiceId}/pdf`, { method: "HEAD" });
    if (!resp.ok) {
      const getResp = await fetch(`/api/invoices/${invoiceId}/pdf`);
      const errData = await getResp.json();
      alert(errData.detail || "No se encontró el archivo PDF en la carpeta archivada.");
      return;
    }
    window.open(`/api/invoices/${invoiceId}/pdf`, "_blank");
  } catch (err) {
    showToast("No se pudo abrir el PDF: " + err.message);
  }
}

/* =========================================================
   Modal de Revisión y Edición con Comprobación de Suma en Vivo
   ========================================================= */
async function openInvoiceModal(invoiceId) {
  try {
    const inv = await apiFetch(`/api/invoices/${invoiceId}`);
    state.currentEditingInvoice = inv;

    document.getElementById("edit-invoice-id").value = inv.id;
    document.getElementById("edit-proveedor").value = inv.proveedor_nombre || "";
    document.getElementById("edit-nif").value = inv.nif || "";
    document.getElementById("edit-numero").value = inv.numero_factura || "";
    document.getElementById("edit-fecha-emision").value = (inv.fecha_emision || "").slice(0, 10);
    document.getElementById("edit-fecha-vencimiento").value = (inv.fecha_vencimiento || "").slice(0, 10);
    document.getElementById("edit-categoria").value = inv.categoria || "compras_mercancia";
    document.getElementById("edit-tipo-documento").value = inv.tipo_documento || "factura_mercancia";
    document.getElementById("edit-base").value = Number(inv.base_imponible || 0).toFixed(2);
    document.getElementById("edit-iva").value = Number(inv.iva || 0).toFixed(2);

    const extra = inv.datos_extra || {};
    document.getElementById("edit-recargo").value = Number(extra.recargo_equivalencia || 0).toFixed(2);
    document.getElementById("edit-irpf").value = Number(extra.irpf || 0).toFixed(2);
    document.getElementById("edit-total").value = Number(inv.total || 0).toFixed(2);
    document.getElementById("edit-notas").value = inv.notas || "";

    const alertsBox = document.getElementById("modal-validation-alerts");
    const errs = inv.errores_validacion || [];
    let replaceHtml = "";
    if (inv.sustituye_a) {
      replaceHtml += `<div class="alert alert-info">Sustituye a la factura #${escapeHtml(inv.sustituye_a)} (mismo proveedor y número).</div>`;
    }
    if (inv.sustituida_por) {
      replaceHtml += `<div class="alert alert-info">Esta factura ha sido sustituida por la #${escapeHtml(inv.sustituida_por)} y se conserva solo como historial.</div>`;
    }
    if (errs.length > 0) {
      alertsBox.innerHTML = replaceHtml + `<div class="alert alert-warning"><strong>Puntos a revisar en esta factura:</strong><br/>${errs.map(escapeHtml).join("<br/>")}</div>`;
    } else {
      alertsBox.innerHTML = replaceHtml;
    }

    const pdfUrl = `/api/invoices/${inv.id}/pdf`;
    document.getElementById("modal-open-pdf-newtab").href = pdfUrl;
    document.getElementById("modal-pdf-frame").src = inv.archivo_disponible ? pdfUrl : "about:blank";

    updateLiveMathCheck();
    document.getElementById("invoice-modal").classList.add("open");
  } catch (err) {
    showToast("Error cargando factura: " + err.message);
  }
}

function updateLiveMathCheck() {
  const base = parseFloat(document.getElementById("edit-base").value || "0");
  const iva = parseFloat(document.getElementById("edit-iva").value || "0");
  const rec = parseFloat(document.getElementById("edit-recargo").value || "0");
  const irpf = parseFloat(document.getElementById("edit-irpf").value || "0");
  const total = parseFloat(document.getElementById("edit-total").value || "0");

  const expected = Math.round((base + iva + rec - irpf) * 100) / 100;
  const diff = Math.abs(expected - total);
  const box = document.getElementById("edit-math-check");

  if (diff <= 0.05) {
    box.style.color = "var(--success)";
    box.textContent = `✓ Los importes cuadran: Base (${fmtEur(base)}) + IVA (${fmtEur(iva)}) + R.E. (${fmtEur(rec)}) - IRPF (${fmtEur(irpf)}) = ${fmtEur(expected)}`;
  } else {
    box.style.color = "var(--danger)";
    box.textContent = `⚠ Los importes no cuadran por ${fmtEur(diff)}: la suma da ${fmtEur(expected)} pero en Total figura ${fmtEur(total)}`;
  }
}

/* =========================================================
   4. Cuadrar con el Banco (Conciliación Bancaria)
   ========================================================= */
async function loadBankReconciliation() {
  try {
    const statusFilter = document.getElementById("filter-bank-status").value;
    const params = statusFilter ? `?estado_conciliacion=${encodeURIComponent(statusFilter)}` : "";
    const [txData, invData] = await Promise.all([
      apiFetch(`/api/bank/transactions${params}`),
      apiFetch("/api/invoices"),
    ]);

    state.unpaidInvoices = (invData.items || []).filter((i) => i.estado_pago !== "pagada");
    renderManualCandidateInvoices();

    const tbody = document.getElementById("tbody-bank-transactions");
    tbody.innerHTML = "";
    state.bankTransactionsById = {};

    (txData.items || []).forEach((tx) => {
      state.bankTransactionsById[tx.id] = tx;
      let badge = `<span class="badge badge-danger">Sin factura asignada</span>`;
      if (tx.estado_conciliacion === "conciliado") badge = `<span class="badge badge-success">Cuadrado con factura</span>`;
      else if (tx.estado_conciliacion === "propuesto") badge = `<span class="badge badge-warning">Coincidencia encontrada</span>`;

      const links = tx.vinculos || [];
      const linksHtml =
        links.length > 0
          ? links
              .map(
                (l) =>
                  `<div><strong>${escapeHtml(l.proveedor_nombre)}</strong> · Fra. ${escapeHtml(l.numero_factura)} (${fmtEur(l.importe_asignado)})</div>`
              )
              .join("")
          : `<span style="color:#64748b;">Ninguna factura unida aún</span>`;

      let actionsHtml = "";
      if (tx.estado_conciliacion === "propuesto") {
        actionsHtml = `
          <button class="btn btn-success btn-sm" onclick="confirmBankProposal(${tx.id})">Confirmar</button>
          <button class="btn btn-danger btn-sm" onclick="rejectBankProposal(${tx.id})">No es esta</button>
        `;
      } else if (tx.estado_conciliacion === "conciliado") {
        actionsHtml = `
          <button class="btn btn-secondary btn-sm" onclick="rejectBankProposal(${tx.id})">Deshacer unión</button>
        `;
      } else {
        actionsHtml = `
          <button class="btn btn-primary btn-sm" onclick="selectTxForManualById(${Number(tx.id)})">Unir a mano</button>
        `;
      }

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(tx.fecha)}</td>
        <td><strong>${escapeHtml(tx.banco)}</strong><br/><small style="color:#64748b;">${escapeHtml(tx.cuenta_iban || "")}</small></td>
        <td>${escapeHtml(tx.concepto)}</td>
        <td class="num" style="color:${tx.importe < 0 ? "var(--danger)" : "var(--success)"};">${fmtEur(tx.importe)}</td>
        <td>${badge}</td>
        <td>${linksHtml}</td>
        <td class="num">${tx.diferencia_pendiente ? fmtEur(tx.diferencia_pendiente) : "-"}</td>
        <td style="white-space:nowrap;">${actionsHtml}</td>
      `;
      tbody.appendChild(tr);
    });

    if (!txData.items || txData.items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#64748b;padding:1.5rem;">Todavía no has subido ningún archivo de movimientos del banco.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando movimientos del banco: " + err.message);
  }
}

function selectTxForManualById(id) {
  const tx = state.bankTransactionsById[id];
  if (tx) selectTxForManual(tx);
}

function selectTxForManual(tx) {
  state.selectedBankTx = tx;
  document.getElementById("manual-selected-tx-label").textContent =
    `Movimiento seleccionado (${tx.fecha}) · ${tx.concepto} · Importe: ${fmtEur(tx.importe)}`;
  document.getElementById("btn-save-manual-reconcile").disabled = false;
  renderManualCandidateInvoices();
  recalculateManualSummary();
}

function renderManualCandidateInvoices() {
  const tbody = document.getElementById("tbody-manual-candidate-invoices");
  tbody.innerHTML = "";
  state.unpaidInvoices.forEach((inv) => {
    const pendingVal = Number(inv.importe_pendiente ?? inv.total ?? 0).toFixed(2);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="manual-inv-check" data-id="${inv.id}" onchange="recalculateManualSummary()" /></td>
      <td><strong>${escapeHtml(inv.proveedor_nombre)}</strong> · <small>${escapeHtml(inv.numero_factura)}</small></td>
      <td>${escapeHtml(inv.fecha_vencimiento || inv.fecha_emision || "-")}</td>
      <td class="num">${fmtEur(pendingVal)}</td>
      <td class="num">
        <input type="number" step="0.01" class="manual-inv-amount" data-id="${inv.id}" value="${pendingVal}" style="width:95px;text-align:right;" oninput="recalculateManualSummary()" />
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function recalculateManualSummary() {
  const tx = state.selectedBankTx;
  const txAmt = tx ? Number(tx.importe || 0) : 0;
  const expectedInvoicesNet = -txAmt;

  let sumAssigned = 0;
  document.querySelectorAll(".manual-inv-check:checked").forEach((chk) => {
    const id = chk.getAttribute("data-id");
    const amtInput = document.querySelector(`.manual-inv-amount[data-id="${id}"]`);
    sumAssigned += parseFloat(amtInput ? amtInput.value : "0") || 0;
  });

  const diff = Math.round((expectedInvoicesNet - sumAssigned) * 100) / 100;
  document.getElementById("manual-sum-summary").innerHTML =
    `Cargo en banco: <strong>${fmtEur(txAmt)}</strong> · Suma de facturas marcadas: <strong>${fmtEur(sumAssigned)}</strong> · Diferencia: <strong style="color:${Math.abs(diff) <= 0.05 ? "var(--success)" : "var(--warning)"};">${fmtEur(diff)}</strong>`;
}

async function confirmBankProposal(movId) {
  try {
    await apiFetch(`/api/bank/transactions/${movId}/confirm-proposal`, { method: "POST" });
    showToast("Pago confirmado y factura marcada como pagada.");
    loadBankReconciliation();
    loadDashboard();
  } catch (err) {
    showToast("Error: " + err.message);
  }
}

async function rejectBankProposal(movId) {
  try {
    await apiFetch(`/api/bank/transactions/${movId}/reject-proposal`, { method: "POST" });
    showToast("Unión deshecha; el movimiento vuelve a quedar pendiente.");
    loadBankReconciliation();
    loadDashboard();
  } catch (err) {
    showToast("Error: " + err.message);
  }
}

/* =========================================================
   5. Configuración Fácil
   ========================================================= */
const CUSTOM_MODEL_VALUE = "__otro__";

const PROVIDER_GUIDES = {
  google_ai_studio: `
    <strong>Cómo configurar Google Gemini (gratis):</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Abre <strong>aistudio.google.com/apikey</strong> e inicia sesión con una cuenta de Google.</li>
      <li>Pulsa <strong>"Create API key"</strong> y copia la clave (empieza por <code>AIza...</code>).</li>
      <li>Pégala en <em>Clave de conexión</em>, deja el modelo <strong>Gemini Flash-Lite (recomendado)</strong> y pulsa <strong>Probar Clave</strong>.</li>
      <li>Si sale bien, pulsa <strong>Guardar Opciones</strong>.</li>
    </ol>
    <span>El modelo recomendado usa el alias <code>-latest</code>: Google lo actualiza solo a su versión más reciente, así el programa no deja de funcionar cuando retiran un modelo antiguo. Flash-Lite es el que más lecturas gratuitas diarias permite; si alguna vez ves "límite de usos superado", espera unas horas o activa el plan de pago.</span>`,
  openai_compatible: `
    <strong>Cómo configurar OpenAI:</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Entra en <strong>platform.openai.com/api-keys</strong>, carga saldo en <em>Settings → Billing</em> (5 € suelen durar mucho).</li>
      <li>Pulsa <strong>"Create new secret key"</strong>, copia la clave (<code>sk-...</code>) y pégala arriba.</li>
      <li>Deja vacía la <em>Dirección del servicio</em>, elige el modelo y pulsa <strong>Probar Clave</strong>.</li>
    </ol>
    <span><strong>IA local (Ollama):</strong> no envía las facturas fuera del ordenador. Escribe la dirección <code>http://localhost:11434/v1</code>, elige "Otro modelo" y pon el nombre del modelo instalado. No necesita clave. Las IA locales no leen PDFs escaneados como imagen.</span>`,
  anthropic: `
    <strong>Cómo configurar Anthropic Claude:</strong>
    <ol style="padding-left:1.2rem; margin:0.3rem 0;">
      <li>Entra en <strong>console.anthropic.com/settings/keys</strong> y añade saldo en <em>Billing</em>.</li>
      <li>Pulsa <strong>"Create Key"</strong>, copia la clave (<code>sk-ant-...</code>) y pégala arriba.</li>
      <li>Elige el modelo (Haiku es el más económico) y pulsa <strong>Probar Clave</strong>.</li>
    </ol>`,
  heuristico_local: `
    <strong>Modo básico sin Internet:</strong> no necesita clave ni envía nada fuera del ordenador.
    Lee bien las facturas con texto digital y formato habitual, pero no las escaneadas como foto; esas quedarán en la <em>Bandeja de Revisión</em>.`,
};

function renderModelOptions(provider, currentModel) {
  const info = (state.llmCatalog || {})[provider] || { modelos: [], modelo_defecto: "" };
  const select = document.getElementById("cfg-modelo-select");
  const custom = document.getElementById("cfg-modelo-custom");
  const group = document.getElementById("cfg-modelo-group");
  const endpointGroup = document.getElementById("cfg-endpoint-group");

  group.style.display = provider === "heuristico_local" ? "none" : "";
  endpointGroup.style.display = provider === "openai_compatible" ? "" : "none";
  document.getElementById("cfg-provider-guide").innerHTML = PROVIDER_GUIDES[provider] || "";

  select.innerHTML = "";
  (info.modelos || []).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.nombre;
    select.appendChild(opt);
  });
  const otherOpt = document.createElement("option");
  otherOpt.value = CUSTOM_MODEL_VALUE;
  otherOpt.textContent = "Otro modelo (escribir el nombre a mano)";
  select.appendChild(otherOpt);

  const wanted = currentModel || info.modelo_defecto;
  const known = (info.modelos || []).some((m) => m.id === wanted);
  if (known) {
    select.value = wanted;
    custom.value = "";
  } else if (wanted) {
    select.value = CUSTOM_MODEL_VALUE;
    custom.value = wanted;
  } else {
    select.value = CUSTOM_MODEL_VALUE;
    custom.value = "";
  }
  updateModelHint();
}

function updateModelHint() {
  const provider = document.getElementById("cfg-proveedor-modelo").value;
  const info = (state.llmCatalog || {})[provider] || { modelos: [] };
  const selected = document.getElementById("cfg-modelo-select").value;
  const custom = document.getElementById("cfg-modelo-custom");
  const hint = document.getElementById("cfg-modelo-hint");
  if (selected === CUSTOM_MODEL_VALUE) {
    custom.style.display = "";
    hint.textContent =
      "Escribe el identificador exacto que indica el proveedor. Si lo dejas vacío se usará el recomendado.";
  } else {
    custom.style.display = "none";
    const m = (info.modelos || []).find((x) => x.id === selected);
    hint.textContent = m ? `${m.descripcion} (identificador: ${m.id})` : "";
  }
}

function getSelectedModel() {
  const selected = document.getElementById("cfg-modelo-select").value;
  if (selected === CUSTOM_MODEL_VALUE) {
    return document.getElementById("cfg-modelo-custom").value.trim();
  }
  return selected || "";
}

async function loadConfiguration() {
  try {
    const [settings, mailData, rulesData] = await Promise.all([
      apiFetch("/api/settings"),
      apiFetch("/api/mail/accounts"),
      apiFetch("/api/rules"),
    ]);

    state.llmCatalog = settings.catalogo_proveedores || {};
    document.getElementById("cfg-proveedor-modelo").value = settings.proveedor_modelo || "google_ai_studio";
    document.getElementById("cfg-llm-endpoint").value = settings.llm_endpoint_url || "";
    renderModelOptions(settings.proveedor_modelo || "google_ai_studio", settings.modelo_llm || "");
    document.getElementById("cfg-estrategia-ocr").value = settings.estrategia_ocr || "vision_llm";
    document.getElementById("cfg-umbral-confianza").value = settings.umbral_confianza ?? 0.85;
    document.getElementById("cfg-umbral-val").textContent = Math.round((settings.umbral_confianza ?? 0.85) * 100) + "%";
    document.getElementById("cfg-capa-almacenamiento").value = settings.capa_almacenamiento || "local";
    document.getElementById("cfg-carpeta-salida").value = settings.carpeta_salida || "";
    document.getElementById("cfg-carpeta-datos-actual").textContent = settings.carpeta_datos || "-";

    const keyConfigured = settings.estado_secretos && settings.estado_secretos.llm_api_key_configurada;
    document.getElementById("cfg-key-status").textContent = keyConfigured
      ? "✓ Tu clave ya está guardada de forma segura en este ordenador."
      : "Aún no has puesto ninguna clave externa (el programa funcionará usando el lector básico del ordenador).";

    const tbodyMail = document.getElementById("tbody-mail-accounts");
    tbodyMail.innerHTML = "";
    (mailData.items || []).forEach((acc) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${escapeHtml(acc.nombre)}</strong><br/><small style="color:#64748b;">${escapeHtml(MAIL_TYPE_LABELS[acc.tipo_conexion] || acc.tipo_conexion)}</small></td>
        <td>${escapeHtml(acc.email)}</td>
        <td>${acc.credencial_configurada ? '<span class="badge badge-success">Guardada</span>' : '<span class="badge badge-warning">Falta ponerla</span>'}</td>
        <td><small>${escapeHtml(acc.ultima_sincronizacion || "Aún no revisado")}</small></td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteMailAccount(${acc.id})">Quitar</button></td>
      `;
      tbodyMail.appendChild(tr);
    });
    if (!mailData.items || mailData.items.length === 0) {
      tbodyMail.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#64748b;">No hay ningún buzón de correo añadido todavía.</td></tr>`;
    }

    const tbodyRules = document.getElementById("tbody-rules");
    tbodyRules.innerHTML = "";
    (rulesData.items || []).forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(RULE_TYPE_LABELS[r.tipo_regla] || r.tipo_regla)}</td>
        <td><strong>${escapeHtml(r.patron)}</strong></td>
        <td>${escapeHtml(r.proveedor_nombre || "-")}</td>
        <td>${escapeHtml(categoryName(r.categoria))}</td>
        <td>${r.aprendida ? '<span class="badge badge-info">Automática</span>' : '<span class="badge badge-success">Puesta por ti</span>'}</td>
        <td><button class="btn btn-danger btn-sm" onclick="deleteRule(${r.id})">Quitar</button></td>
      `;
      tbodyRules.appendChild(tr);
    });
    if (!rulesData.items || rulesData.items.length === 0) {
      tbodyRules.innerHTML = `<tr><td colspan="6" style="text-align:center;color:#64748b;">Todavía no hay reglas creadas.</td></tr>`;
    }
  } catch (err) {
    showToast("Error cargando opciones: " + err.message);
  }
}

async function deleteMailAccount(id) {
  await apiFetch(`/api/mail/accounts/${id}`, { method: "DELETE" });
  showToast("Cuenta de correo eliminada.");
  loadConfiguration();
}

async function deleteRule(id) {
  await apiFetch(`/api/rules/${id}`, { method: "DELETE" });
  showToast("Regla eliminada.");
  loadConfiguration();
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* =========================================================
   Inicialización de eventos
   ========================================================= */
document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  loadDashboard();

  // Subida rápida de PDFs
  document.getElementById("quick-upload-input").addEventListener("change", async (e) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    try {
      showToast(`Leyendo y archivando ${files.length} factura(s) PDF...`);
      const res = await apiFetch("/api/invoices/upload", { method: "POST", body: fd });
      showToast(`Listo: ${res.insertadas} factura(s) nueva(s), ${res.duplicadas} ya estaban guardadas.`);
      loadDashboard();
      loadInvoicesList();
      loadReviewInbox();
    } catch (err) {
      showToast("Error al añadir PDF: " + err.message);
    } finally {
      e.target.value = "";
    }
  });

  // Botón cargar datos de ejemplo
  document.getElementById("btn-load-demo").addEventListener("click", async () => {
    try {
      showToast("Cargando facturas y movimientos bancarios de ejemplo...");
      const res = await apiFetch("/api/demo/load-sample-data", { method: "POST" });
      showToast(
        `Datos de ejemplo listos: ${res.facturas_insertadas} facturas y ${res.movimientos_insertados} movimientos del banco.`
      );
      loadDashboard();
      loadReviewInbox();
      loadInvoicesList();
      loadBankReconciliation();
    } catch (err) {
      showToast("Error cargando datos de ejemplo: " + err.message);
    }
  });

  // Botón revisar correo ahora
  document.getElementById("btn-sync-mail").addEventListener("click", async () => {
    try {
      showToast("Buscando facturas nuevas en tu correo...");
      const res = await apiFetch("/api/mail/sync", { method: "POST" });
      const total = (res.sincronizaciones || []).reduce((acc, s) => acc + (s.facturas_insertadas || 0), 0);
      showToast(`Revisión terminada: ${total} factura(s) nueva(s) encontrada(s).`);
      loadDashboard();
    } catch (err) {
      showToast("Error revisando el correo: " + err.message);
    }
  });

  // Filtros de facturas
  document.getElementById("btn-apply-filters").addEventListener("click", loadInvoicesList);
  document.getElementById("btn-clear-filters").addEventListener("click", () => {
    document.getElementById("filter-busqueda").value = "";
    document.getElementById("filter-mes").value = "";
    document.getElementById("filter-categoria").value = "";
    document.getElementById("filter-estado-revision").value = "";
    document.getElementById("filter-estado-pago").value = "";
    document.getElementById("filter-incluir-sustituidas").checked = false;
    loadInvoicesList();
  });
  document.getElementById("btn-refresh-revision").addEventListener("click", loadReviewInbox);

  // Modal de revisión/edición
  document.getElementById("btn-close-modal").addEventListener("click", () => {
    document.getElementById("invoice-modal").classList.remove("open");
  });
  ["edit-base", "edit-iva", "edit-recargo", "edit-irpf", "edit-total"].forEach((id) => {
    document.getElementById(id).addEventListener("input", updateLiveMathCheck);
  });

  document.getElementById("form-edit-invoice").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = document.getElementById("edit-invoice-id").value;
    const currentExtra = (state.currentEditingInvoice && state.currentEditingInvoice.datos_extra) || {};
    const payload = {
      proveedor_nombre: document.getElementById("edit-proveedor").value.trim(),
      nif: document.getElementById("edit-nif").value.trim(),
      numero_factura: document.getElementById("edit-numero").value.trim(),
      fecha_emision: document.getElementById("edit-fecha-emision").value || null,
      fecha_vencimiento: document.getElementById("edit-fecha-vencimiento").value || null,
      categoria: document.getElementById("edit-categoria").value,
      tipo_documento: document.getElementById("edit-tipo-documento").value,
      base_imponible: parseFloat(document.getElementById("edit-base").value || "0"),
      iva: parseFloat(document.getElementById("edit-iva").value || "0"),
      total: parseFloat(document.getElementById("edit-total").value || "0"),
      notas: document.getElementById("edit-notas").value.trim(),
      estado_revision: "confirmada",
      aprender_regla_proveedor: document.getElementById("edit-learn-rule").checked,
      datos_extra: {
        ...currentExtra,
        recargo_equivalencia: parseFloat(document.getElementById("edit-recargo").value || "0"),
        irpf: parseFloat(document.getElementById("edit-irpf").value || "0"),
      },
    };
    try {
      await apiFetch(`/api/invoices/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      showToast("Factura confirmada y guardada.");
      document.getElementById("invoice-modal").classList.remove("open");
      loadReviewInbox();
      loadInvoicesList();
      loadDashboard();
    } catch (err) {
      showToast("Error guardando factura: " + err.message);
    }
  });

  document.getElementById("btn-delete-invoice").addEventListener("click", async () => {
    const id = document.getElementById("edit-invoice-id").value;
    if (!id || !confirm("¿Seguro que quieres borrar esta factura del programa?")) return;
    await apiFetch(`/api/invoices/${id}`, { method: "DELETE" });
    document.getElementById("invoice-modal").classList.remove("open");
    showToast("Factura borrada.");
    loadReviewInbox();
    loadInvoicesList();
    loadDashboard();
  });

  // Conciliación bancaria
  document.getElementById("filter-bank-status").addEventListener("change", loadBankReconciliation);
  document.getElementById("form-bank-import").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById("bank-import-file");
    if (!fileInput.files || fileInput.files.length === 0) return;

    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("banco", document.getElementById("bank-import-name").value.trim());
    fd.append("formato", document.getElementById("bank-import-format").value);

    const colFecha = document.getElementById("csv-col-fecha").value.trim();
    const colConcepto = document.getElementById("csv-col-concepto").value.trim();
    const colImporte = document.getElementById("csv-col-importe").value.trim();
    if (colFecha) fd.append("columna_fecha", colFecha);
    if (colConcepto) fd.append("columna_concepto", colConcepto);
    if (colImporte) fd.append("columna_importe", colImporte);
    fd.append("separador", document.getElementById("csv-sep").value || ";");

    try {
      const res = await apiFetch("/api/bank/import", { method: "POST", body: fd });
      showToast(
        `Extracto leído: ${res.nuevos_insertados} movimientos nuevos añadidos (${res.duplicados_omitidos} repetidos ignorados).`
      );
      fileInput.value = "";
      loadBankReconciliation();
      loadDashboard();
    } catch (err) {
      showToast("Error subiendo el archivo del banco: " + err.message);
    }
  });

  document.getElementById("btn-run-auto-reconcile").addEventListener("click", async () => {
    const res = await apiFetch("/api/bank/auto-reconcile", { method: "POST" });
    showToast(
      `Búsqueda terminada: ${res.conciliados_automaticos || 0} pagos cuadrados solos y ${res.propuestas_revision || 0} propuestas para revisar.`
    );
    loadBankReconciliation();
    loadDashboard();
  });

  document.getElementById("btn-save-manual-reconcile").addEventListener("click", async () => {
    if (!state.selectedBankTx) return;
    const asignaciones = [];
    document.querySelectorAll(".manual-inv-check:checked").forEach((chk) => {
      const id = parseInt(chk.getAttribute("data-id"), 10);
      const amtInput = document.querySelector(`.manual-inv-amount[data-id="${id}"]`);
      const val = parseFloat(amtInput ? amtInput.value : "0") || 0;
      asignaciones.push({ factura_id: id, importe_asignado: val });
    });
    if (asignaciones.length === 0) {
      showToast("Marca con un tic al menos una factura de la lista.");
      return;
    }
    try {
      const res = await apiFetch("/api/bank/reconcile-manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          movimiento_id: state.selectedBankTx.id,
          asignaciones,
          aprender_regla: document.getElementById("manual-learn-rule").checked,
        }),
      });
      showToast(
        res.cuadra_exacto
          ? "Pago unido con sus facturas correctamente (suma exacta)."
          : `Pago guardado dejando una diferencia pendiente de ${fmtEur(res.diferencia_pendiente)}.`
      );
      state.selectedBankTx = null;
      document.getElementById("btn-save-manual-reconcile").disabled = true;
      document.getElementById("manual-selected-tx-label").textContent =
        "Ningún movimiento del banco seleccionado todavía";
      loadBankReconciliation();
      loadDashboard();
    } catch (err) {
      showToast("Error guardando la unión: " + err.message);
    }
  });

  // Configuración
  document.getElementById("cfg-umbral-confianza").addEventListener("input", (e) => {
    document.getElementById("cfg-umbral-val").textContent = Math.round(parseFloat(e.target.value) * 100) + "%";
  });

  document.getElementById("cfg-proveedor-modelo").addEventListener("change", (e) => {
    // Al cambiar de proveedor se propone su modelo recomendado y su dirección oficial.
    document.getElementById("cfg-llm-endpoint").value = "";
    renderModelOptions(e.target.value, "");
    document.getElementById("cfg-test-result").textContent = "";
  });

  document.getElementById("cfg-modelo-select").addEventListener("change", () => {
    updateModelHint();
  });

  document.getElementById("btn-test-llm-key").addEventListener("click", async () => {
    const resBox = document.getElementById("cfg-test-result");
    resBox.style.color = "var(--text-muted)";
    resBox.textContent = "Probando conexión con el lector de facturas...";
    try {
      const res = await apiFetch("/api/settings/test-llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          proveedor_modelo: document.getElementById("cfg-proveedor-modelo").value,
          modelo_llm: getSelectedModel(),
          llm_endpoint_url: document.getElementById("cfg-llm-endpoint").value.trim(),
          llm_api_key: document.getElementById("cfg-llm-api-key").value.trim(),
        }),
      });
      resBox.style.color = res.ok ? "var(--success)" : "var(--danger)";
      resBox.textContent = res.mensaje;
      if (res.ok) loadConfiguration();
    } catch (err) {
      resBox.style.color = "var(--danger)";
      resBox.textContent = "Error al probar la clave: " + err.message;
    }
  });

  document.getElementById("form-general-settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      proveedor_modelo: document.getElementById("cfg-proveedor-modelo").value,
      modelo_llm: getSelectedModel(),
      llm_endpoint_url: document.getElementById("cfg-llm-endpoint").value.trim(),
      estrategia_ocr: document.getElementById("cfg-estrategia-ocr").value,
      umbral_confianza: parseFloat(document.getElementById("cfg-umbral-confianza").value),
      capa_almacenamiento: document.getElementById("cfg-capa-almacenamiento").value,
      carpeta_salida: document.getElementById("cfg-carpeta-salida").value.trim(),
      llm_api_key: document.getElementById("cfg-llm-api-key").value.trim(),
    };
    try {
      await apiFetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      document.getElementById("cfg-llm-api-key").value = "";
      showToast("Opciones guardadas correctamente.");
      loadConfiguration();
    } catch (err) {
      showToast("Error guardando opciones: " + err.message);
    }
  });

  document.getElementById("form-mail-account").addEventListener("submit", async (e) => {
    e.preventDefault();
    const remitentes = document
      .getElementById("mail-remitentes")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      nombre: document.getElementById("mail-nombre").value.trim(),
      email: document.getElementById("mail-email").value.trim(),
      tipo_conexion: document.getElementById("mail-tipo").value,
      servidor_imap: document.getElementById("mail-host").value.trim(),
      puerto_imap: parseInt(document.getElementById("mail-port").value || "993", 10),
      password_o_token: document.getElementById("mail-secret").value,
      remitentes_permitidos: remitentes,
    };
    try {
      await apiFetch("/api/mail/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      document.getElementById("mail-secret").value = "";
      showToast("Cuenta de correo guardada de forma segura.");
      loadConfiguration();
    } catch (err) {
      showToast("Error guardando cuenta de correo: " + err.message);
    }
  });

  document.getElementById("form-rule").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      tipo_regla: document.getElementById("rule-tipo").value,
      patron: document.getElementById("rule-patron").value.trim(),
      proveedor_nombre: document.getElementById("rule-proveedor").value.trim(),
      categoria: document.getElementById("rule-categoria").value,
    };
    await apiFetch("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("rule-patron").value = "";
    showToast("Regla añadida.");
    loadConfiguration();
  });

  document.getElementById("btn-manual-backup").addEventListener("click", async () => {
    const res = await apiFetch("/api/database/backup", { method: "POST" });
    showToast(res.created ? `Copia de seguridad guardada en: ${res.backup_path}` : "Todavía no hay datos que copiar.");
  });

  document.getElementById("btn-change-data-dir").addEventListener("click", async () => {
    const box = document.getElementById("data-dir-status-box");
    const ruta = document.getElementById("cfg-carpeta-datos-nueva").value.trim();
    if (!ruta) {
      box.textContent = "Escribe primero la carpeta nueva.";
      return;
    }
    box.textContent = "Preparando la nueva carpeta...";
    try {
      const res = await apiFetch("/api/settings/data-dir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ruta }),
      });
      box.textContent = res.mensaje;
    } catch (err) {
      box.textContent = "No se pudo cambiar la carpeta: " + err.message;
    }
  });

  document.getElementById("btn-check-updates").addEventListener("click", async () => {
    const box = document.getElementById("updater-status-box");
    box.textContent = "Comprobando si hay una versión nueva...";
    const res = await apiFetch("/api/updater/check");
    if (res.error) {
      box.textContent = `No se pudo comprobar ahora mismo: ${res.error}`;
    } else if (res.hay_actualizacion) {
      box.innerHTML = `<strong style="color:var(--success);">¡Hay una versión nueva disponible (${escapeHtml(res.version_remota)})!</strong> <button type="button" class="btn btn-primary btn-sm" id="btn-install-update">Instalar ahora</button>`;
      document.getElementById("btn-install-update").addEventListener("click", async (ev) => {
        ev.target.disabled = true;
        box.textContent = "Descargando y comprobando la actualización...";
        try {
          const r = await apiFetch("/api/updater/install", { method: "POST" });
          box.textContent = r.mensaje || "Actualización en curso.";
        } catch (err) {
          box.textContent = "No se pudo instalar la actualización: " + err.message;
        }
      });
    } else {
      box.textContent = `Ya tienes instalada la última versión disponible (${res.version_actual}).`;
    }
  });
});
