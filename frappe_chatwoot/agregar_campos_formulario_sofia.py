"""Campos de configuración del renderer de formularios, sobre el doctype `Web Form`.

Son lo que vuelve módulo a `/f/<ruta>`: sin esto, el diseño de cada formulario
se decidiría en código y cada cliente nuevo exigiría un despliegue.

Se montan como Custom Field sobre el `Web Form` de Frappe —no se toca su JSON—
para que una actualización de Frappe no los borre. Idempotente.

    bench --site crm.lavendi.mx execute frappe_chatwoot.agregar_campos_formulario_sofia.ejecutar
"""

import frappe

DOCTYPE = "Web Form"

CAMPOS = [
    {
        "fieldname": "sofia_seccion",
        "fieldtype": "Section Break",
        "label": "Diseño Sofía",
        "insert_after": "custom_css",
        "collapsible": 1,
        "description": (
            "Aplica a la ruta /f/&lt;ruta&gt;, que sirve este formulario con el "
            "sistema de diseño de Sofía. La ruta original no cambia."
        ),
    },
    {
        "fieldname": "sofia_tema",
        "fieldtype": "Select",
        "label": "Tema",
        "insert_after": "sofia_seccion",
        # El orden importa: el primero es el default de Frappe para un Select
        # sin valor, y `claro` es el único tema que se ve bien sin saber nada
        # de la página donde se va a embeber.
        "options": "claro\noscuro\ncurso\nhome\nproyectos\nia",
        "default": "claro",
        "description": (
            "Default del formulario. El parámetro ?tema= de la URL manda sobre "
            "esto, para poder embeber el mismo formulario en páginas distintas."
        ),
    },
    {
        "fieldname": "sofia_intro",
        "fieldtype": "Small Text",
        "label": "Texto de introducción",
        "insert_after": "sofia_tema",
        "description": "Una línea bajo el título. Si se deja vacío se usa la introducción estándar.",
    },
]


def ejecutar():
    creados, existentes = [], []
    for campo in CAMPOS:
        nombre = f"{DOCTYPE}-{campo['fieldname']}"
        if frappe.db.exists("Custom Field", nombre):
            existentes.append(campo["fieldname"])
            continue
        doc = frappe.get_doc({"doctype": "Custom Field", "dt": DOCTYPE, **campo})
        doc.insert(ignore_permissions=True)
        creados.append(campo["fieldname"])

    frappe.db.commit()
    frappe.clear_cache(doctype=DOCTYPE)
    print(f"creados={creados} ya_existian={existentes}")


if __name__ == "__main__":
    ejecutar()
