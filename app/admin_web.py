from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiohttp import web

from app.config import Settings
from app.keywords import KeywordService
from app.runtime_config import CONFIG_KEYS, RuntimeConfigService
from app.storage.db import ActionRepository, KEYWORD_KINDS

logger = logging.getLogger(__name__)


class AdminWebServer:
    def __init__(
        self,
        settings: Settings,
        keyword_service: KeywordService,
        repository: ActionRepository,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self.settings = settings
        self.keyword_service = keyword_service
        self.repository = repository
        self.runtime_config = runtime_config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth_middleware])
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/healthz", self._healthz),
                web.get("/api/keywords", self._api_list_keywords),
                web.post("/api/keywords", self._api_add_keyword),
                web.post("/api/keywords/delete", self._api_delete_keyword),
                web.get("/api/groups", self._api_list_groups),
                web.post("/api/groups/private/add", self._api_private_add),
                web.post("/api/groups/private/remove", self._api_private_remove),
                web.post("/api/groups/private/toggle", self._api_private_toggle),
                web.post("/api/groups/public/add", self._api_public_add),
                web.post("/api/groups/public/remove", self._api_public_remove),
                web.post("/api/groups/public/toggle", self._api_public_toggle),
                web.get("/api/config", self._api_get_config),
                web.post("/api/config", self._api_set_config),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.settings.admin_web_host, port=self.settings.admin_web_port)
        await self._site.start()
        logger.info(
            "admin_web_started",
            extra={"action": "admin_web", "reason": f"{self.settings.admin_web_host}:{self.settings.admin_web_port}"},
        )

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)

        token = (self.settings.admin_web_token or "").strip()
        if token:
            request_token = request.query.get("token", "") or request.headers.get("X-Admin-Token", "")
            if request_token != token:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _api_list_keywords(self, _: web.Request) -> web.Response:
        data = await self.keyword_service.list_keywords()
        return web.json_response({"kinds": data, "valid_kinds": list(KEYWORD_KINDS)})

    async def _api_add_keyword(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        kind = str(payload.get("kind", "")).strip().lower()
        value = str(payload.get("value", "")).strip()
        if kind not in KEYWORD_KINDS:
            return web.json_response({"error": "invalid_kind"}, status=400)
        if not value:
            return web.json_response({"error": "empty_value"}, status=400)
        tokens = await self.keyword_service.add_keyword(kind, value)
        return web.json_response({"status": "ok", "added": tokens})

    async def _api_delete_keyword(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        kind = str(payload.get("kind", "")).strip().lower()
        value = str(payload.get("value", "")).strip()
        if kind not in KEYWORD_KINDS:
            return web.json_response({"error": "invalid_kind"}, status=400)
        if not value:
            return web.json_response({"error": "empty_value"}, status=400)
        deleted = await self.keyword_service.delete_keyword(kind, value)
        return web.json_response({"status": "ok", "deleted": deleted})

    async def _api_list_groups(self, _: web.Request) -> web.Response:
        private_rows = await self.repository.fetch_private_invite_rows(limit=300)
        public_rows = await self.repository.fetch_public_groups(limit=300)
        return web.json_response(
            {
                "private": [
                    {
                        "invite_link": row.invite_link,
                        "active": row.active,
                        "source_chat_id": row.source_chat_id,
                        "last_seen_at": row.last_seen_at,
                    }
                    for row in private_rows
                ],
                "public": [
                    {
                        "peer_id": row.peer_id,
                        "username": row.username,
                        "title": row.title,
                        "active": row.active,
                        "joined": row.joined,
                        "source_query": row.source_query,
                        "last_error": row.last_error,
                    }
                    for row in public_rows
                ],
            }
        )

    async def _api_private_add(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        invite_link = str(payload.get("invite_link", "")).strip()
        if not invite_link:
            return web.json_response({"error": "empty_invite_link"}, status=400)
        await self.repository.upsert_private_invite_link(
            invite_link=invite_link,
            source_chat_id=None,
            note="admin_manual",
            active=True,
        )
        return web.json_response({"status": "ok", "invite_link": invite_link})

    async def _api_private_remove(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        invite_link = str(payload.get("invite_link", "")).strip()
        if not invite_link:
            return web.json_response({"error": "empty_invite_link"}, status=400)
        removed = await self.repository.delete_private_invite(invite_link)
        return web.json_response({"status": "ok", "removed": removed})

    async def _api_private_toggle(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        invite_link = str(payload.get("invite_link", "")).strip()
        active = self._to_bool(payload.get("active", True))
        if not invite_link:
            return web.json_response({"error": "empty_invite_link"}, status=400)
        updated = await self.repository.set_private_invite_active(invite_link, active)
        return web.json_response({"status": "ok", "updated": updated, "active": active})

    async def _api_public_add(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        username = str(payload.get("username", "")).strip().lstrip("@")
        if not username:
            return web.json_response({"error": "empty_username"}, status=400)
        peer_id = await self.repository.upsert_public_group_username(username=username, title="admin_manual")
        return web.json_response({"status": "ok", "username": username, "peer_id": peer_id})

    async def _api_public_remove(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        username = str(payload.get("username", "")).strip().lstrip("@")
        if not username:
            return web.json_response({"error": "empty_username"}, status=400)
        removed = await self.repository.delete_public_group(username)
        return web.json_response({"status": "ok", "removed": removed})

    async def _api_public_toggle(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        username = str(payload.get("username", "")).strip().lstrip("@")
        active = self._to_bool(payload.get("active", True))
        if not username:
            return web.json_response({"error": "empty_username"}, status=400)
        updated = await self.repository.set_public_group_active(username, active)
        return web.json_response({"status": "ok", "updated": updated, "active": active})

    async def _api_get_config(self, _: web.Request) -> web.Response:
        if not self.runtime_config:
            return web.json_response({"enabled": False, "config": {}, "keys": []})
        config = await self.runtime_config.list_config()
        return web.json_response({"enabled": True, "config": config, "keys": list(CONFIG_KEYS)})

    async def _api_set_config(self, request: web.Request) -> web.Response:
        if not self.runtime_config:
            return web.json_response({"error": "runtime_config_disabled"}, status=503)
        payload = await self._read_payload(request)
        try:
            if "key" in payload:
                key = str(payload.get("key", "")).strip()
                value = payload.get("value")
                snapshot = await self.runtime_config.set_value(key, value)
            else:
                values = payload.get("values", payload)
                if not isinstance(values, dict):
                    return web.json_response({"error": "invalid_values_payload"}, status=400)
                cleaned = {str(k).strip(): v for k, v in values.items() if str(k).strip() in CONFIG_KEYS}
                if not cleaned:
                    return web.json_response({"error": "empty_values"}, status=400)
                snapshot = await self.runtime_config.set_many(cleaned)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"status": "ok", "config": snapshot.as_json()})

    async def _index(self, request: web.Request) -> web.Response:
        token = request.query.get("token", "")
        html = self._render_dashboard_html(token)
        return web.Response(text=html, content_type="text/html")

    @staticmethod
    async def _read_payload(request: web.Request) -> dict[str, Any]:
        content_type = request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return await request.json()
        post = await request.post()
        return {k: v for k, v in post.items()}

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _render_dashboard_html(token: str) -> str:
        token_qs = f"?token={escape(token)}" if token else ""
        kind_opts = "".join(f"<option value='{k}'>{k}</option>" for k in KEYWORD_KINDS)
        return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Taxi Userbot Admin</title>
  <style>
    body {{ margin:0; font-family: -apple-system, Segoe UI, sans-serif; background:#f3f5fb; color:#151820; }}
    .wrap {{ max-width: 1180px; margin:0 auto; padding:14px; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }}
    .card {{ background:#fff; border:1px solid #dbe0ee; border-radius:12px; padding:12px; }}
    .row {{ display:flex; gap:8px; margin-bottom:8px; }}
    .row-col {{ display:flex; flex-direction:column; gap:8px; margin-bottom:8px; }}
    input, select, textarea, button {{ padding:9px; border-radius:9px; border:1px solid #ccd3e4; font:inherit; }}
    button {{ cursor:pointer; }}
    textarea {{ min-height:90px; resize:vertical; }}
    .tbl {{ width:100%; border-collapse:collapse; font-size:0.86rem; }}
    .tbl th, .tbl td {{ border-bottom:1px solid #edf1f8; padding:6px; text-align:left; vertical-align:top; }}
    .muted {{ color:#6a7385; font-size:0.82rem; }}
    .ok {{ color:#0f7f2e; }}
    .bad {{ color:#a72020; }}
    .full {{ grid-column: 1 / -1; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} .full {{ grid-column:auto; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h2>Taxi Userbot Admin</h2>
    <p class="muted">Keywordlar, private/public guruhlar, va runtime limitlarni real-time boshqaring.</p>

    <div class="grid">
      <section class="card">
        <h3>Keywordlar</h3>
        <div class="row">
          <select id="kwKind">{kind_opts}</select>
          <input id="kwValue" placeholder="Kalit so'z" />
          <button onclick="kwAdd()">Qo'shish</button>
        </div>
        <div class="row">
          <input id="kwDeleteValue" placeholder="O'chirish kalit so'z" />
          <button onclick="kwDelete()">O'chirish</button>
        </div>
        <div id="kwList" class="muted">Yuklanmoqda...</div>
      </section>

      <section class="card">
        <h3>Runtime Config</h3>
        <div class="row-col">
          <input id="cfg_forward_target" placeholder="forward_target (me yoki @channel)" />
          <div class="row">
            <input id="cfg_min_text_length" placeholder="min_text_length" />
            <input id="cfg_per_group_actions_hour" placeholder="actions/hour/group" />
            <input id="cfg_per_group_replies_10m" placeholder="replies/10m/group" />
          </div>
          <div class="row">
            <input id="cfg_join_limit_day" placeholder="join/day" />
            <input id="cfg_global_actions_minute" placeholder="global actions/min" />
          </div>
          <div class="row">
            <input id="cfg_min_human_delay_sec" placeholder="min delay sec" />
            <input id="cfg_max_human_delay_sec" placeholder="max delay sec" />
          </div>
          <div class="row">
            <label><input type="checkbox" id="cfg_discovery_enabled" /> discovery_enabled</label>
          </div>
          <div class="row">
            <input id="cfg_discovery_query_limit" placeholder="discovery query limit" />
            <input id="cfg_discovery_join_batch" placeholder="discovery join batch" />
          </div>
          <textarea id="cfg_discovery_queries" placeholder="discovery querylar (vergul yoki yangi qator bilan)"></textarea>
          <button onclick="saveConfig()">Config ni saqlash</button>
        </div>
      </section>

      <section class="card">
        <h3>Yopiq Guruh (Invite)</h3>
        <div class="row">
          <input id="privateLink" style="flex:1" placeholder="https://t.me/+xxxx" />
          <button onclick="privateAdd()">Qo'shish</button>
        </div>
        <table class="tbl" id="privateTbl"></table>
      </section>

      <section class="card">
        <h3>Ochiq Guruh (Username)</h3>
        <div class="row">
          <input id="publicUsername" style="flex:1" placeholder="@group_username" />
          <button onclick="publicAdd()">Qo'shish</button>
        </div>
        <table class="tbl" id="publicTbl"></table>
      </section>

      <section class="card full">
        <h3>Status</h3>
        <div id="status" class="muted">Ready</div>
      </section>
    </div>
  </div>

  <script>
    const tokenQs = "{token_qs}";
    function setStatus(msg, isErr=false) {{
      const node = document.getElementById('status');
      node.className = isErr ? 'bad' : 'ok';
      node.textContent = msg;
    }}
    async function req(url, body=null) {{
      const init = body ? {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}} : {{}};
      const res = await fetch(url + tokenQs, init);
      const data = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(data));
      return data;
    }}
    function esc(v) {{
      return String(v || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}
    async function refreshAll() {{
      await Promise.all([loadKeywords(), loadGroups(), loadConfig()]);
    }}
    async function loadKeywords() {{
      const data = await req('/api/keywords');
      const kinds = data.kinds;
      const lines = Object.keys(kinds).map(k => `<b>${{esc(k)}}</b>: ${{kinds[k].map(esc).join(', ')}}`);
      document.getElementById('kwList').innerHTML = lines.join('<br/>') || 'No keywords';
    }}
    async function loadGroups() {{
      const data = await req('/api/groups');
      const pRows = ['<tr><th>Invite</th><th>Active</th><th>Action</th></tr>'];
      for (const row of data.private) {{
        const link = esc(row.invite_link);
        pRows.push(`<tr>
          <td>${{link}}</td>
          <td>${{row.active}}</td>
          <td>
            <button onclick="privateToggle('${{link}}', ${{!row.active}})">${{row.active ? 'Disable' : 'Enable'}}</button>
            <button onclick="privateRemove('${{link}}')">Delete</button>
          </td>
        </tr>`);
      }}
      document.getElementById('privateTbl').innerHTML = pRows.join('');

      const gRows = ['<tr><th>Username</th><th>Joined</th><th>Active</th><th>Action</th></tr>'];
      for (const row of data.public) {{
        const u = esc(row.username || '');
        gRows.push(`<tr>
          <td>@${{u}}</td>
          <td>${{row.joined}}</td>
          <td>${{row.active}}</td>
          <td>
            <button onclick="publicToggle('${{u}}', ${{!row.active}})">${{row.active ? 'Disable' : 'Enable'}}</button>
            <button onclick="publicRemove('${{u}}')">Delete</button>
          </td>
        </tr>`);
      }}
      document.getElementById('publicTbl').innerHTML = gRows.join('');
    }}
    async function loadConfig() {{
      try {{
        const data = await req('/api/config');
        if (!data.enabled) {{
          return;
        }}
        const cfg = data.config || {{}};
        cfg_forward_target.value = cfg.forward_target || '';
        cfg_min_text_length.value = cfg.min_text_length ?? '';
        cfg_per_group_actions_hour.value = cfg.per_group_actions_hour ?? '';
        cfg_per_group_replies_10m.value = cfg.per_group_replies_10m ?? '';
        cfg_join_limit_day.value = cfg.join_limit_day ?? '';
        cfg_global_actions_minute.value = cfg.global_actions_minute ?? '';
        cfg_min_human_delay_sec.value = cfg.min_human_delay_sec ?? '';
        cfg_max_human_delay_sec.value = cfg.max_human_delay_sec ?? '';
        cfg_discovery_enabled.checked = !!cfg.discovery_enabled;
        cfg_discovery_query_limit.value = cfg.discovery_query_limit ?? '';
        cfg_discovery_join_batch.value = cfg.discovery_join_batch ?? '';
        cfg_discovery_queries.value = (cfg.discovery_queries || []).join('\\n');
      }} catch (e) {{
        setStatus(String(e), true);
      }}
    }}
    async function saveConfig() {{
      try {{
        const values = {{
          forward_target: cfg_forward_target.value,
          min_text_length: cfg_min_text_length.value,
          per_group_actions_hour: cfg_per_group_actions_hour.value,
          per_group_replies_10m: cfg_per_group_replies_10m.value,
          join_limit_day: cfg_join_limit_day.value,
          global_actions_minute: cfg_global_actions_minute.value,
          min_human_delay_sec: cfg_min_human_delay_sec.value,
          max_human_delay_sec: cfg_max_human_delay_sec.value,
          discovery_enabled: cfg_discovery_enabled.checked,
          discovery_query_limit: cfg_discovery_query_limit.value,
          discovery_join_batch: cfg_discovery_join_batch.value,
          discovery_queries: cfg_discovery_queries.value,
        }};
        await req('/api/config', {{values}});
        setStatus('Config saqlandi');
      }} catch (e) {{
        setStatus(String(e), true);
      }}
    }}
    async function kwAdd() {{
      try {{
        await req('/api/keywords', {{kind: kwKind.value, value: kwValue.value}});
        kwValue.value = '';
        await loadKeywords();
        setStatus('Keyword qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function kwDelete() {{
      try {{
        await req('/api/keywords/delete', {{kind: kwKind.value, value: kwDeleteValue.value}});
        kwDeleteValue.value = '';
        await loadKeywords();
        setStatus('Keyword o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function privateAdd() {{
      try {{
        await req('/api/groups/private/add', {{invite_link: privateLink.value}});
        privateLink.value = '';
        await loadGroups();
        setStatus('Private invite qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function privateRemove(link) {{
      try {{
        await req('/api/groups/private/remove', {{invite_link: link}});
        await loadGroups();
        setStatus('Private invite o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function privateToggle(link, active) {{
      try {{
        await req('/api/groups/private/toggle', {{invite_link: link, active}});
        await loadGroups();
        setStatus('Private invite holati yangilandi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function publicAdd() {{
      try {{
        await req('/api/groups/public/add', {{username: publicUsername.value}});
        publicUsername.value = '';
        await loadGroups();
        setStatus('Public group qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function publicRemove(username) {{
      try {{
        await req('/api/groups/public/remove', {{username}});
        await loadGroups();
        setStatus('Public group o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    async function publicToggle(username, active) {{
      try {{
        await req('/api/groups/public/toggle', {{username, active}});
        await loadGroups();
        setStatus('Public group holati yangilandi');
      }} catch (e) {{ setStatus(String(e), true); }}
    }}
    refreshAll().catch((e) => setStatus(String(e), true));
  </script>
</body>
</html>
        """
