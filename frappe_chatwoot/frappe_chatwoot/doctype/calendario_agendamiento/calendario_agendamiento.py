# Copyright (c) 2026, lavendi.mx
# MIT
"""Calendario Agendamiento — fuente de agendamiento NATIVA (sin Google Calendar).

Generaliza el patrón ya probado en Estrublock (citas con `event_id` sintético
`local-<uuid>`, sin pasar por Google) a un doctype propio, reusable por
cualquier sitio: cada registro es un "calendario" con su propia duración,
horario semanal y ventana de reserva. La disponibilidad real (slots) y la
reserva viven en `api/agenda_calendarios.py`, NO aquí — este controller solo
normaliza `slug` y `url_publica`.

Deliberadamente NO toca `api/agenda_publica.py` / `www/agenda/*` (el sistema
de agendamiento público ACTUAL, exclusivo de lavendi.mx vía Google Calendar) —
es un camino aditivo y paralelo.
"""

import re
import unicodedata

import frappe
from frappe.model.document import Document


def slugificar(texto: str) -> str:
	"""Minúsculas, sin acentos, espacios/separadores → guiones.

	Mismo criterio que cualquier slug de URL: solo [a-z0-9-], sin guiones
	repetidos ni en los extremos.
	"""
	texto = (texto or "").strip()
	# Descompone acentos (NFKD) y se queda solo con los caracteres base ASCII.
	texto = unicodedata.normalize("NFKD", texto)
	texto = texto.encode("ascii", "ignore").decode("ascii")
	texto = texto.lower()
	texto = re.sub(r"[^a-z0-9]+", "-", texto)
	texto = re.sub(r"-+", "-", texto).strip("-")
	return texto


class CalendarioAgendamiento(Document):
	# NO se define `autoname()` aquí a propósito: el doctype ya usa
	# `autoname: "hash"` (naming_rule "Random") — definir el método lo
	# sobreescribiría y Frappe dejaría de generar el `name` interno solo.
	# La normalización del slug vive en `validate()`.
	def validate(self):
		if not self.slug:
			self.slug = slugificar(self.titulo)
		else:
			self.slug = slugificar(self.slug)

		if not self.slug:
			frappe.throw(frappe._("No se pudo generar un slug a partir del título — escribe uno manualmente."))

		# Duplicado explícito y claro: el `unique` del campo ya lo impediría a
		# nivel de BD, pero con un IntegrityError críptico en vez de un mensaje
		# útil para quien está dando de alta el calendario.
		existente = frappe.db.get_value(
			"Calendario Agendamiento", {"slug": self.slug, "name": ["!=", self.name or ""]}, "name"
		)
		if existente:
			frappe.throw(frappe._("Ya existe otro calendario con el slug «{0}» ({1}).").format(self.slug, existente))

		if self.duracion_minutos is not None and self.duracion_minutos <= 0:
			frappe.throw(frappe._("La duración debe ser mayor a cero minutos."))

		# `url_publica` es de solo lectura para el usuario (read_only=1 en el
		# doctype) pero se recalcula aquí en cada guardado para que nunca quede
		# desincronizada de `slug` — no es un dato propio, es una proyección.
		self.url_publica = f"/agendar/{self.slug}"
