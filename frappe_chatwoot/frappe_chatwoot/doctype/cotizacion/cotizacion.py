import frappe
from frappe.model.document import Document
from frappe.utils import add_days, flt, getdate

class Cotizacion(Document):
    def validate(self):
        total = desc = 0.0
        for it in self.items:
            importe = flt(it.cantidad) * flt(it.precio_unitario)
            d = importe * flt(it.descuento_pct) / 100.0
            it.importe = importe - d
            total += importe
            desc += d
        self.total = total
        self.descuento_total = desc
        self.neto = total - desc
        self.iva = self.neto * 0.16 if self.aplica_iva else 0
        self.gran_total = self.neto + self.iva
        if self.fecha and self.validez_dias:
            self.vence_el = add_days(getdate(self.fecha), int(self.validez_dias))
