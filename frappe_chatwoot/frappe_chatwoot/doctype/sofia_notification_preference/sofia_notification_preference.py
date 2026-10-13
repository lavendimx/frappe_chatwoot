# Copyright (c) 2026, lavendi.mx
# MIT

import frappe
from frappe.model.document import Document


class SofiaNotificationPreference(Document):
	def validate(self):
		# Una fila por (user, event_type). El autoname "format:{user}-{event_type}"
		# ya lo garantiza en la práctica (una segunda inserción con el mismo par
		# colisiona con DuplicateEntryError antes de llegar aquí), pero se deja
		# esta validación explícita como cinturón de seguridad para cualquier
		# camino que construya el `name` distinto (ej. importación/migración).
		existing = frappe.db.exists(
			"Sofia Notification Preference",
			{"user": self.user, "event_type": self.event_type, "name": ("!=", self.name)},
		)
		if existing:
			frappe.throw(
				f"Ya existe una preferencia de notificación para {self.user} / {self.event_type}."
			)
