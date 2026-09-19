# Copyright (c) 2026, lavendi.mx
#
# Plantilla reutilizable de "Caso de éxito" (one-page PDF) para SofíA CRM.
# Generaliza el diseño aprobado por Alejandro el 2026-09-18 (hero dorado/negro,
# Instrument Serif + Archivo + JetBrains Mono) en dos formatos observados en
# los primeros 2 casos reales:
#   - "Métricas": reto -> tabla "lo que hicimos" -> tarjetas de cifra (Esplendido.mx)
#   - "Diagnóstico": reto -> tarjetas por cliente -> cita destacada (patrón de ventas)
#
# El PDF NO se genera aquí dentro de Frappe: la página pública /caso-exito
# (www/caso_exito.py) renderiza el HTML, y el mismo motor Playwright que ya
# aprobó Alejandro (/root/scripts/render_cotizacion.py) le toma un PDF desde
# fuera, apuntando a esa URL. Así el diseño no cambia de motor de render.

import frappe
from frappe.model.document import Document

MAX_STATS = 3


class CasoExito(Document):
	def validate(self):
		if len(self.stats or []) > MAX_STATS:
			frappe.throw(f"Máximo {MAX_STATS} tarjetas de cifra (stats) por caso de éxito.")
