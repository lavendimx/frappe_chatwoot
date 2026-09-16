# Provisionamiento de un sitio de cliente (Sofía CRM)

Cómo se propaga una mejora de `crm.lavendi.mx` a un sitio nuevo (Six Gardens y los
que vengan). Antes de esto la propagación era a mano, sitio por sitio, y la rama
`lavendi-sofia` de `crm` no estaba en ningún remoto: un rebuild del volumen
`frappe-home` se la llevaba.

## De qué consta un sitio

| Capa | Dónde vive | Cómo viaja |
|---|---|---|
| App `crm` (SPA + backend) | `lavendimx/crm`, rama **`lavendi-sofia`** (privado) | `bench get-app crm <url> --branch lavendi-sofia` |
| App `frappe_chatwoot` (API, utils, agentes) | `lavendimx/frappe_chatwoot`, rama `master` | `bench get-app frappe_chatwoot <url>` |
| 15 doctypes propios | `frappe_chatwoot/frappe_chatwoot/doctype/*` (código) | `bench migrate` los crea |
| Campos, property setters, traducciones, permisos, Web Forms, embudo/orígenes/razones | `frappe_chatwoot/frappe_chatwoot/fixtures/*.json` | `bench migrate` los importa |
| Lo que depende de `erpnext` (`Customer`, `Sales Invoice`, `Payment Entry`) | `frappe_chatwoot/frappe_chatwoot/fixtures/erpnext/*.json` | `after_migrate`, **solo si erpnext está instalado** |
| Ajustes que los fixtures no pueden hacer (borrar etapas nativas) | `frappe_chatwoot/utils/provisionamiento.py` (`after_migrate`) | `bench migrate` |
| Marca de la plataforma (nombre, logo, splash, favicon) | `frappe_chatwoot/public/images/*.png` + `provisionamiento.py` | `bench migrate` |
| Usuario de servicio del agente (`agente-ia@lavendi.mx`) | `provisionamiento.py` (usuario; la API key es manual) | `bench migrate` |
| Agente IA (motor Node) | repo `agente-ia` (rama `master`) | `pm2 restart agente-ia-chatwoot` |
| Datos del cliente (contactos, conversaciones, KB, plantillas, tokens) | DB + Chatwoot | **No viaja** — es por cliente |

**No hay un branch por sitio.** El código es uno solo, compartido por todos los sitios
del bench. Lo que cambia por sitio es (a) qué apps están instaladas y (b) la config de
su DB. Un sitio "básico" (frappe + crm + frappe_chatwoot, sin erpnext) recibe todo
menos lo de la fila de erpnext.

## Alta de un sitio nuevo

```bash
bench new-site <cliente>.lavendi.mx --mariadb-root-password "$MARIADB_ROOT_PASSWORD" --admin-password <pass>
bench --site <cliente>.lavendi.mx install-app crm
bench --site <cliente>.lavendi.mx install-app frappe_chatwoot
# solo si el cliente lleva facturación:
bench --site <cliente>.lavendi.mx install-app erpnext
bench --site <cliente>.lavendi.mx migrate
bench build
```

`frappe_chatwoot` al final: sus fixtures tocan doctypes de `crm`. `erpnext` en cualquier
orden (sus dependencias van en `fixtures/erpnext/`, que se importan solas si está).

El `migrate` deja el sitio listo **salvo lo que es decisión por cliente**: marca propia
(si la quiere), inbox de Chatwoot + número de WhatsApp, `Chatwoot Settings` (tokens de
agenda y onboarding), KB sources, plantillas, y encender el agente.

### DNS, vhost y el header que no se puede olvidar

1. Registro A `<cliente>.lavendi.mx` → `37.27.107.103` (API de Hostinger, aditivo, TTL 300).
2. vhost de nginx + `certbot --nginx`.
3. ⚠ **El vhost DEBE mandar `X-Frappe-Site-Name: <cliente>.lavendi.mx`.** El bench es
   multi-sitio real (se quitó `default_site` el 2026-09-16); sin ese header el sitio da
   **404**. Aplica a todo vhost que proxye a `8094` (`sofiav2`, `agenda`, `sixgardens`).
   El upstream de socket.io vive en `conf.d/00-sofia-socketio-upstream.conf` — incluirlo
   dentro de cada vhost choca con `duplicate upstream`.

### Conectar el agente al sitio (el "switcher")

El agente es **uno solo** y atiende a todos los clientes; decide a qué sitio hablar por
el inbox de la conversación. Si el cliente no tiene agente, este paso se omite.

1. **Usuario de servicio + API key** (en el sitio nuevo):
   ```bash
   bench --site <sitio> execute frappe_chatwoot.utils.provisionamiento.asegurar_usuario_servicio
   ```
   Devuelve `api_key`/`api_secret`. Es idempotente y **no pisa una key existente**.
2. **`/etc/hosts` del host**: `127.0.0.1 <cliente>.lavendi.mx` (para que el proceso Node
   resuelva el sitio; el bench escucha en `127.0.0.1:8094`).
3. **`.env` del agente** (`agente-ia/.env`, compartido por todas las pistas — avisar antes):
   ```
   FRAPPE_SITES={"<inbox_id>":{"url":"http://<cliente>.lavendi.mx:8094","key":"...","secret":"..."}}
   ```
4. `pm2 restart agente-ia-chatwoot`. El log debe listar el inbox nuevo:
   `Polling cada 5000ms para inboxes: 1, 3, 4, 5, 7`.
5. Si el inbox tenía un registro `Agente IA` viejo en el sitio compartido (de cuando no
   había switcher), **desactivarlo** (`active=0`) — el del sitio del cliente es el bueno.

**Default-safe**: sin `FRAPPE_SITES`, todo se resuelve al sitio compartido y el agente se
comporta igual que antes. Eso permite desplegar el switcher sin tocar producción.

⚠ **Pendiente conocido del switcher**: `agenda-sync` (barrido Google→CRM) y `agenda-publica`
siguen apuntando al sitio compartido. No molesta mientras un cliente no use la agenda; si
la usa, hay que extenderlos igual que el resto (`siteFor`/`sitesToPoll`).

## Propagar una mejora ya hecha en crm.lavendi.mx

1. **Código** (`crm` o `frappe_chatwoot`): commit + push en la rama correspondiente.
   En cada sitio: `bench --site <sitio> update` (o `git pull` + `bench migrate` + `bench build`).
2. **Doctype nuevo o cambiado**: exportarlo a código
   (`bench --site crm.lavendi.mx execute frappe_chatwoot._exportar_doctypes.ejecutar`)
   y commit.
3. **Campo / property setter / traducción / permiso / Web Form**: agregarlo a la DB de
   crm.lavendi.mx, luego `bench --site crm.lavendi.mx export-fixtures --app frappe_chatwoot`
   y commit. Los filtros viven en `hooks.py` (`fixtures = [...]`).
   Si el doctype destino es de `erpnext` (o de cualquier app opcional), va a
   `fixtures/erpnext/` — no a `fixtures/`.
4. **Borrar algo** (etapa, campo): los fixtures no borran — va en
   `utils/provisionamiento.py` (como `_borrar_etapas_nativas`) o en un patch de la app.

## Trampas aprendidas

- **`bench new-app` en producción es peligroso**: registra la app en `sites/apps.txt`
  al instante; si la carpeta desaparece, bench no carga y el contenedor entra en
  crash-loop. Los doctypes van a una app ya instalada.
- **Los fixtures saltan el ARCHIVO COMPLETO, no el registro**: `frappe/utils/fixtures.py`
  envuelve cada `.json` en un `try/except`; si un solo doctype del archivo no existe,
  no se importa nada de ese archivo. Por eso lo que depende de una app opcional va en
  `fixtures/erpnext/`.
- **Las child tables no se exportan como fixture**: al importarlas Frappe las vuelve a
  insertar y duplica los campos (pasó con `Web Form Field`: 24 → 48). Los campos de un
  Web Form viajan dentro de `web_form.json`.
- **`get_controller`**: la clase del controlador debe llamarse igual que el doctype sin
  espacios ni guiones (`Agente IA` → `AgenteIA`). Si no, `ImportError` →
  `remove_orphan_doctypes` **borra** el doctype en el siguiente migrate.
- **Los doctypes van en la carpeta del módulo**, no en la raíz del paquete:
  `apps/frappe_chatwoot/frappe_chatwoot/frappe_chatwoot/doctype/` (la del `modules.txt`).
- **Cada paso de un `after_migrate` con su propio commit**: un rename que choca revierte
  los borrados de la misma corrida.
- **`generate_keys` regenera el `api_secret` SIEMPRE**, aunque la key ya exista. Correrlo
  en un sitio vivo (o en cada `migrate`) le rompe la credencial al agente que ya la tiene
  en su `.env`. Por eso `after_migrate` solo crea el usuario, y la key se genera a mano
  una vez con `asegurar_usuario_servicio()` (que sí respeta una key existente).
- **`/files/*` se sirve del disco**, no del doctype `File`: basta copiar el PNG a
  `sites/<sitio>/public/files/`. El registro `File` es opcional (verificado 2026-09-16).
- **La marca no se pisa**: `_branding_plataforma()` solo escribe `Website Settings` si el
  `app_name` sigue en `""`/`Frappe`. Un cliente con marca propia la conserva.
- **Rutas en `/etc/hosts` del host**: `crm.lavendi.mx` y cada sitio de cliente apuntan a
  `127.0.0.1` porque el bench solo escucha ahí. Un sitio nuevo sin esa línea da
  *connection refused* desde el proceso Node (no un 404, que sería el otro síntoma).
