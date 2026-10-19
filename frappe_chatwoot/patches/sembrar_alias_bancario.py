"""Siembra `Contact.alias_bancario` en el nuevo Sofía.

Por qué existe
--------------
El skill `pagos-recibidos` resuelve la identidad de una transferencia comparando
el nombre del ordenante bancario contra el del contacto. Cuando no coinciden
(el cliente transfiere desde la razón social de su empresa), hace falta un
alias. En GHL ese campo existía (`RIqtXLPoCHSHxrOCA5HV`) pero estaba **vacío en
los 4,224 contactos**: el conocimiento vivía escrito a mano dentro del texto del
skill, así que solo servía si alguien leía el skill completo.

Al migrar fuera de GHL se aprovecha para volverlo dato de verdad. Este script es
idempotente: se puede correr las veces que haga falta.

    docker exec frappe-<id> bash -lc "cd /home/frappe/frappe-bench && \
      bench --site crm.lavendi.mx execute \
      frappe_chatwoot.patches.sembrar_alias_bancario.run"

o, más simple, copiándolo al contenedor y ejecutándolo con `bench console`.
"""
import unicodedata

import frappe

# Los alias que el skill `pagos-recibidos` documentaba en prosa. Agregar aquí
# los nuevos que vayan apareciendo, o llenarlos desde la ficha del contacto.
ALIAS = {
    "alexis leon": "Aerotec",
    "guillermo nicolas garcia": "Comercializadora Empresa Segura SA",
    "alma cabello": "T Integra Bienes Raices",
}


def _norm(s: str) -> str:
    """Minúsculas sin acentos — los nombres migrados de GHL los traen
    inconsistentes ('Nicolas' vs 'Nicolás' para la misma persona)."""
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def run():
    contactos = frappe.get_all("Contact", pluck="name")
    normalizados = [(c, _norm(c)) for c in contactos]

    for nombre, alias in ALIAS.items():
        partes = nombre.split()
        candidatos = [c for c, n in normalizados if all(p in n for p in partes)]
        if len(candidatos) == 1:
            frappe.db.set_value("Contact", candidatos[0], "alias_bancario", alias,
                                update_modified=False)
            print(f"SEMBRADO: {candidatos[0]} -> {alias}")
        elif not candidatos:
            print(f"NO_ENCONTRADO: {nombre}")
        else:
            # Ante empate no se adivina: colgarle el alias al contacto
            # equivocado haría que un pago se acredite a otra persona.
            print(f"AMBIGUO: {nombre} -> {candidatos[:5]}")

    frappe.db.commit()
    print("CON_ALIAS:", frappe.db.count("Contact", {"alias_bancario": ["!=", ""]}))
