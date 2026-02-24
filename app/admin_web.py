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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#f5f2ea" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="mobile-web-app-capable" content="yes" />
  <title>Taxi Userbot Admin</title>
  <style>
    :root {{
      --bg: #f5f2ea;
      --ink: #1d201f;
      --muted: #6a7368;
      --panel: rgba(255,255,255,0.92);
      --line: #d8decf;
      --line-strong: #bfc9b7;
      --accent: #0f7a6a;
      --accent-2: #d46a2f;
      --danger: #b33636;
      --shadow: 0 10px 28px rgba(28, 38, 24, 0.08);
      --radius: 16px;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
      scroll-behavior: smooth;
    }}
    html.tg-webapp {{
      background: var(--bg);
    }}
    body {{
      margin:0;
      font-family: "Trebuchet MS", "Segoe UI", "Tahoma", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(60rem 40rem at -10% -10%, rgba(15,122,106,0.12), transparent 55%),
        radial-gradient(48rem 30rem at 110% 0%, rgba(212,106,47,0.12), transparent 50%),
        linear-gradient(180deg, #fbfaf6 0%, var(--bg) 100%);
      min-height:100vh;
      min-height:100dvh;
      overscroll-behavior-y: contain;
    }}
    .wrap {{
      max-width: 1360px;
      margin:0 auto;
      padding:
        calc(16px + env(safe-area-inset-top, 0px))
        calc(14px + env(safe-area-inset-right, 0px))
        calc(24px + env(safe-area-inset-bottom, 0px))
        calc(14px + env(safe-area-inset-left, 0px));
    }}
    .hero {{
      display:grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap:12px;
      margin-bottom:12px;
    }}
    .hero-main, .hero-side {{
      background: var(--panel);
      border:1px solid var(--line);
      border-radius: 20px;
      box-shadow: var(--shadow);
    }}
    .hero-main {{
      padding:16px 16px 14px;
      position: relative;
      overflow:hidden;
    }}
    .hero-main::after {{
      content:"";
      position:absolute;
      right:-40px;
      top:-28px;
      width:180px;
      height:180px;
      background: linear-gradient(180deg, rgba(15,122,106,0.14), rgba(212,106,47,0.10));
      border-radius: 28px;
      transform: rotate(14deg);
      border: 1px solid rgba(15,122,106,0.10);
    }}
    .hero h1 {{
      margin:0;
      font-family: Georgia, "Times New Roman", serif;
      font-size:1.7rem;
      letter-spacing: .01em;
      position: relative;
      z-index: 1;
    }}
    .hero p {{
      margin:8px 0 0;
      color: var(--muted);
      line-height:1.4;
      max-width: 56ch;
      position: relative;
      z-index:1;
    }}
    .hero-actions {{ margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; position: relative; z-index:1; }}
    .hero-actions::-webkit-scrollbar,
    .toolbar::-webkit-scrollbar {{
      height: 6px;
    }}
    .hero-actions::-webkit-scrollbar-thumb,
    .toolbar::-webkit-scrollbar-thumb {{
      background: rgba(109, 118, 106, 0.35);
      border-radius: 999px;
    }}
    .hero-side {{ padding:12px; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
    .metric {{
      border:1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(180deg, #fff, #f5f7ef);
      padding:10px;
      min-height:78px;
    }}
    .metric .label {{ font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }}
    .metric .value {{ font-size:1.24rem; font-weight:700; margin-top:4px; }}
    .metric .sub {{ color:var(--muted); font-size:.78rem; margin-top:3px; }}

    .toolbar {{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      align-items:center;
      margin-bottom:12px;
      background: rgba(255,255,255,.72);
      border:1px solid var(--line);
      border-radius: 14px;
      padding:8px;
      backdrop-filter: blur(3px);
      position: sticky;
      top: calc(env(safe-area-inset-top, 0px) + 6px);
      z-index: 4;
    }}
    .toolbar .spacer {{ flex:1 1 auto; }}
    .toolbar button {{ flex: 0 0 auto; }}

    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; align-items:start; }}
    .stack {{ display:grid; gap:12px; align-content:start; }}
    .card {{
      background: var(--panel);
      border:1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow:hidden;
    }}
    .card-head {{
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:8px;
      padding:12px 14px;
      border-bottom:1px solid #e6ebdf;
      background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(245,247,239,0.9));
    }}
    .card-head h3 {{ margin:0; font-size:1rem; }}
    .card-head .meta {{ color:var(--muted); font-size:.78rem; margin-top:2px; }}
    .card-body {{ padding:12px 14px 14px; }}
    .card-actions {{ display:flex; gap:6px; flex-wrap:wrap; }}
    .row {{ display:flex; gap:8px; margin-bottom:8px; align-items:flex-end; }}
    .row-col {{ display:flex; flex-direction:column; gap:8px; margin-bottom:8px; }}
    .field {{ display:grid; gap:4px; flex:1 1 auto; min-width:0; }}
    .field.small {{ flex:0 0 170px; }}
    .field label {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }}
    .grid-2 {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:8px; }}
    .grid-3 {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:8px; }}
    .soft-box {{
      background: rgba(255,255,255,0.55);
      border:1px solid #e4e9de;
      border-radius: 12px;
      padding:10px;
    }}
    .soft-box + .soft-box {{ margin-top:10px; }}
    .section-note {{ margin-top:5px; color:var(--muted); font-size:.79rem; }}

    input, select, textarea, button {{
      padding:9px 10px;
      border-radius:10px;
      border:1px solid var(--line-strong);
      font:inherit;
      background:#fff;
      color:var(--ink);
      min-height: 44px;
      -webkit-appearance: none;
      appearance: none;
    }}
    input, select, textarea {{ font-size: 16px; }}
    input:focus, select:focus, textarea:focus {{
      outline:none;
      border-color: rgba(15,122,106,.55);
      box-shadow: 0 0 0 4px rgba(15,122,106,.12);
    }}
    button {{
      cursor:pointer;
      background: linear-gradient(180deg, #fff, #f4f7ee);
      font-weight:600;
    }}
    button:hover {{ border-color: #98a590; }}
    button:disabled {{ opacity:.55; cursor:wait; }}
    .btn-primary {{
      background: linear-gradient(180deg, rgba(15,122,106,.95), rgba(11,104,90,.95));
      color:#fff;
      border-color: rgba(10,86,74,.95);
    }}
    .btn-danger {{
      color: var(--danger);
      background: rgba(179,54,54,.05);
      border-color: rgba(179,54,54,.25);
    }}
    .btn-mini {{ padding:6px 9px; border-radius:8px; font-size:.78rem; min-height: 34px; }}

    textarea {{ min-height:108px; resize:vertical; }}
    .table-wrap {{
      border:1px solid #e4e9de;
      border-radius:12px;
      overflow:auto;
      background:#fff;
      max-height: 420px;
      -webkit-overflow-scrolling: touch;
      overscroll-behavior: contain;
    }}
    .tbl {{ width:100%; border-collapse:collapse; font-size:0.85rem; min-width:640px; }}
    .tbl th, .tbl td {{ border-bottom:1px solid #edf1e7; padding:8px; text-align:left; vertical-align:top; }}
    .tbl th {{
      position:sticky;
      top:0;
      background:#f2f5ea;
      z-index:1;
      color:#505a4e;
      text-transform:uppercase;
      letter-spacing:.04em;
      font-size:.72rem;
    }}
    .tbl tr:nth-child(even) td {{ background: rgba(247,249,242,.6); }}
    .muted {{ color: var(--muted); font-size:0.82rem; }}
    .badge {{
      display:inline-flex;
      align-items:center;
      padding:2px 8px;
      border-radius:999px;
      border:1px solid var(--line);
      background:#fff;
      font-size:.74rem;
      white-space:nowrap;
    }}
    .badge.ok {{ border-color: rgba(12,107,59,.22); color:#0c6b3b; background: rgba(12,107,59,.07); }}
    .badge.off {{ border-color: rgba(124,49,49,.20); color:#7c3131; background: rgba(124,49,49,.06); }}
    .badge.warn {{ border-color: rgba(153,106,18,.22); color:#8d5e10; background: rgba(153,106,18,.07); }}
    .mono {{ font-family: Consolas, "Courier New", monospace; font-size:.77rem; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:6px; }}
    .chip {{
      display:inline-flex;
      border:1px solid #d6deca;
      background:#fff;
      border-radius:999px;
      padding:4px 8px;
      font-size:.78rem;
      max-width:100%;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }}
    .chip-group {{
      border:1px dashed #d5ddca;
      border-radius:12px;
      background: rgba(255,255,255,.55);
      padding:10px;
    }}
    .chip-group h4 {{ margin:0 0 8px; font-size:.82rem; text-transform:uppercase; letter-spacing:.05em; color:#4f5a4e; }}
    .status-bar {{
      position: sticky;
      bottom: calc(env(safe-area-inset-bottom, 0px) + 8px);
      margin-top:12px;
      display:flex;
      justify-content:space-between;
      gap:10px;
      align-items:flex-start;
      background: rgba(255,255,255,.92);
      border:1px solid var(--line);
      border-radius:14px;
      box-shadow: var(--shadow);
      padding:10px 12px;
      z-index: 5;
    }}
    .status-title {{ font-weight:700; font-size:.86rem; margin-bottom:2px; }}
    .status-text {{ color:var(--muted); font-size:.84rem; word-break:break-word; }}
    .status-ok .status-text {{ color:#0d6d4b; }}
    .status-bad .status-text {{ color:var(--danger); }}
    .status-time {{ color:var(--muted); font-size:.78rem; white-space:nowrap; }}
    .full {{ grid-column: 1 / -1; }}

    @media (max-width: 1120px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .hero-side {{ grid-template-columns: repeat(4, minmax(0,1fr)); }}
      .full {{ grid-column:auto; }}
    }}
    @media (max-width: 760px) {{
      .wrap {{
        padding:
          calc(12px + env(safe-area-inset-top, 0px))
          calc(10px + env(safe-area-inset-right, 0px))
          calc(20px + env(safe-area-inset-bottom, 0px))
          calc(10px + env(safe-area-inset-left, 0px));
      }}
      .hero h1 {{ font-size:1.38rem; }}
      .hero-side {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
      .hero-actions {{
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        -webkit-overflow-scrolling: touch;
      }}
      .hero-actions button {{ flex: 0 0 auto; }}
      .toolbar {{
        flex-wrap: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }}
      .toolbar .spacer {{ display:none; }}
      .toolbar .badge {{ flex: 0 0 auto; }}
      .row {{ flex-wrap:wrap; align-items:stretch; }}
      .field.small {{ flex:1 1 46%; }}
      .grid-2, .grid-3 {{ grid-template-columns:1fr; }}
      .tbl {{ min-width:520px; }}
      .status-bar {{
        flex-direction: column;
        align-items: stretch;
        gap: 6px;
      }}
      .status-time {{ white-space: normal; }}
    }}
    @media (max-width: 420px) {{
      .hero-main, .hero-side, .card {{
        border-radius: 14px;
      }}
      .hero-main {{
        padding: 12px 12px 10px;
      }}
      .card-head {{
        padding: 10px 12px;
      }}
      .card-body {{
        padding: 10px 12px 12px;
      }}
      .metric {{
        min-height: 70px;
        padding: 8px;
      }}
      .metric .value {{
        font-size: 1.08rem;
      }}
      .tbl th, .tbl td {{
        padding: 7px 6px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-main">
        <h1>Taxi Userbot Admin</h1>
        <p>Keywordlar, private/public source guruhlar va runtime config ni bitta sahifadan boshqaring. UI qayta tartiblangan: jadval o'qilishi, filter qidiruvi va status feedback yaxshilandi.</p>
        <div class="hero-actions">
          <button class="btn-primary" onclick="refreshAll()">Refresh All</button>
          <button onclick="scrollToId('runtimeCard')">Runtime Config</button>
          <button onclick="scrollToId('privateCard')">Private Groups</button>
          <button onclick="scrollToId('publicCard')">Public Groups</button>
        </div>
      </div>
      <div class="hero-side">
        <div class="metric">
          <div class="label">Keywords</div>
          <div class="value" id="sumKeywords">-</div>
          <div class="sub" id="sumKeywordKinds">kinds: -</div>
        </div>
        <div class="metric">
          <div class="label">Private Invites</div>
          <div class="value" id="sumPrivate">-</div>
          <div class="sub" id="sumPrivateActive">active: -</div>
        </div>
        <div class="metric">
          <div class="label">Public Groups</div>
          <div class="value" id="sumPublic">-</div>
          <div class="sub" id="sumPublicJoined">joined: -</div>
        </div>
        <div class="metric">
          <div class="label">Runtime Config</div>
          <div class="value" id="sumConfig">-</div>
          <div class="sub" id="sumConfigMeta">loaded: -</div>
        </div>
      </div>
    </section>

    <section class="toolbar">
      <button onclick="refreshAll()">Refresh</button>
      <button onclick="loadKeywords()">Keywords</button>
      <button onclick="loadGroups()">Groups</button>
      <button onclick="loadConfig()">Config</button>
      <span class="spacer"></span>
      <span class="badge" id="lastRefreshBadge">Last refresh: -</span>
    </section>

    <div class="grid">
      <div class="stack">
        <section class="card" id="keywordsCard">
          <div class="card-head">
            <div>
              <h3>Keyword Management</h3>
              <div class="meta">Qo'shish, o'chirish va category bo'yicha ko'rish</div>
            </div>
            <div class="card-actions">
              <button class="btn-mini" onclick="loadKeywords()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <div class="row">
              <div class="field small">
                <label>Kind</label>
                <select id="kwKind">{kind_opts}</select>
              </div>
              <div class="field">
                <label>Yangi keyword</label>
                <input id="kwValue" placeholder="Masalan: kerak / yandex / vakansiya" />
              </div>
              <div class="field small">
                <label>&nbsp;</label>
                <button class="btn-primary" onclick="kwAdd()">Qo'shish</button>
              </div>
            </div>
            <div class="row">
              <div class="field">
                <label>Delete keyword</label>
                <input id="kwDeleteValue" placeholder="O'chiriladigan keyword" />
              </div>
              <div class="field small">
                <label>&nbsp;</label>
                <button class="btn-danger" onclick="kwDelete()">O'chirish</button>
              </div>
            </div>
            <div class="row">
              <div class="field">
                <label>Qidiruv</label>
                <input id="kwSearch" placeholder="Keyword filter..." oninput="renderKeywords()" />
              </div>
            </div>
            <div id="kwList">Yuklanmoqda...</div>
          </div>
        </section>

        <section class="card" id="privateCard">
          <div class="card-head">
            <div>
              <h3>Yopiq Guruh (Invite)</h3>
              <div class="meta">Private invite linklar: enable/disable/delete</div>
            </div>
            <div class="card-actions">
              <button class="btn-mini" onclick="loadGroups()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <div class="row">
              <div class="field">
                <label>Invite link</label>
                <input id="privateLink" placeholder="https://t.me/+xxxx" />
              </div>
              <div class="field small">
                <label>&nbsp;</label>
                <button class="btn-primary" onclick="privateAdd()">Qo'shish</button>
              </div>
            </div>
            <div class="row">
              <div class="field">
                <label>Filter</label>
                <input id="privateSearch" placeholder="invite yoki source_chat_id bo'yicha..." oninput="renderGroups()" />
              </div>
            </div>
            <div class="table-wrap">
              <table class="tbl" id="privateTbl"></table>
            </div>
          </div>
        </section>

        <section class="card" id="publicCard">
          <div class="card-head">
            <div>
              <h3>Ochiq Guruh (Username)</h3>
              <div class="meta">Discovery/public source guruhlar ro'yxati</div>
            </div>
            <div class="card-actions">
              <button class="btn-mini" onclick="loadGroups()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <div class="row">
              <div class="field">
                <label>Username</label>
                <input id="publicUsername" placeholder="@group_username" />
              </div>
              <div class="field small">
                <label>&nbsp;</label>
                <button class="btn-primary" onclick="publicAdd()">Qo'shish</button>
              </div>
            </div>
            <div class="row">
              <div class="field">
                <label>Filter</label>
                <input id="publicSearch" placeholder="@username / title / peer_id..." oninput="renderGroups()" />
              </div>
            </div>
            <div class="table-wrap">
              <table class="tbl" id="publicTbl"></table>
            </div>
          </div>
        </section>
      </div>

      <div class="stack">
        <section class="card" id="runtimeCard">
          <div class="card-head">
            <div>
              <h3>Runtime Config</h3>
              <div class="meta">Bot ishlayotgan paytda parametrlarni yangilash</div>
            </div>
            <div class="card-actions">
              <button class="btn-mini" onclick="loadConfig()">Reload</button>
              <button class="btn-mini btn-primary" onclick="saveConfig()">Save</button>
            </div>
          </div>
          <div class="card-body">
            <div class="soft-box">
              <div class="field">
                <label>Forward Target</label>
                <input id="cfg_forward_target" placeholder="forward_target (me / @channel / -100...)" />
              </div>
              <div class="grid-2" style="margin-top:8px;">
                <div class="field">
                  <label>Min Text Length</label>
                  <input id="cfg_min_text_length" placeholder="min_text_length" />
                </div>
                <div class="field">
                  <label>Global Actions / Minute</label>
                  <input id="cfg_global_actions_minute" placeholder="global actions/min" />
                </div>
              </div>
              <div class="section-note">Forward target va asosiy filter/limit parametrlari.</div>
            </div>

            <div class="soft-box">
              <div class="grid-3">
                <div class="field">
                  <label>Actions / Hour / Group</label>
                  <input id="cfg_per_group_actions_hour" placeholder="actions/hour/group" />
                </div>
                <div class="field">
                  <label>Replies / 10m / Group</label>
                  <input id="cfg_per_group_replies_10m" placeholder="replies/10m/group" />
                </div>
                <div class="field">
                  <label>Join / Day</label>
                  <input id="cfg_join_limit_day" placeholder="join/day" />
                </div>
              </div>
              <div class="grid-2" style="margin-top:8px;">
                <div class="field">
                  <label>Min Human Delay (sec)</label>
                  <input id="cfg_min_human_delay_sec" placeholder="min delay sec" />
                </div>
                <div class="field">
                  <label>Max Human Delay (sec)</label>
                  <input id="cfg_max_human_delay_sec" placeholder="max delay sec" />
                </div>
              </div>
              <div class="section-note">Rate limit va human-like delay bloklari.</div>
            </div>

            <div class="soft-box">
              <div class="row" style="align-items:center;">
                <label class="badge" style="gap:8px; padding:8px 10px; border-radius:10px;">
                  <input type="checkbox" id="cfg_discovery_enabled" />
                  <span>discovery_enabled</span>
                </label>
              </div>
              <div class="grid-2">
                <div class="field">
                  <label>Discovery Query Limit</label>
                  <input id="cfg_discovery_query_limit" placeholder="discovery query limit" />
                </div>
                <div class="field">
                  <label>Discovery Join Batch</label>
                  <input id="cfg_discovery_join_batch" placeholder="discovery join batch" />
                </div>
              </div>
              <div class="field" style="margin-top:8px;">
                <label>Discovery Queries</label>
                <textarea id="cfg_discovery_queries" placeholder="discovery querylar (vergul yoki yangi qator bilan)"></textarea>
              </div>
              <div class="section-note">Querylar vergul yoki yangi qatorda bo'lishi mumkin.</div>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <div>
              <h3>Notes</h3>
              <div class="meta">Tezkor eslatmalar</div>
            </div>
          </div>
          <div class="card-body">
            <div class="soft-box">
              <div class="muted" style="line-height:1.5;">
                <div><strong>Private invite:</strong> `https://t.me/+...` formatidan foydalaning.</div>
                <div><strong>Public group:</strong> `@username` yoki username yozish mumkin.</div>
                <div><strong>Config Save:</strong> runtime config darhol apply bo'ladi.</div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <section class="status-bar" id="statusBar">
      <div class="status-ok" id="statusState">
        <div class="status-title">Status</div>
        <div id="status" class="status-text">Ready</div>
      </div>
      <div class="status-time" id="statusTime">-</div>
    </section>
  </div>

  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script>
    const tokenQs = "{token_qs}";
    const state = {{ keywords: null, groups: null, config: null, configEnabled: false, busy: 0 }};
    let tgWebApp = null;
    let tgEventsBound = false;
    function el(id) {{ return document.getElementById(id); }}
    function esc(v) {{
      return String(v || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}
    function setStatus(msg, isErr=false) {{
      const statusNode = el('status');
      const stateNode = el('statusState');
      statusNode.textContent = String(msg || '');
      stateNode.className = isErr ? 'status-bad' : 'status-ok';
      el('statusTime').textContent = new Date().toLocaleTimeString();
    }}
    function scrollToId(id) {{
      const node = el(id);
      if (node) node.scrollIntoView({{behavior:'smooth', block:'start'}});
    }}
    function setBusy(on) {{
      state.busy += on ? 1 : -1;
      if (state.busy < 0) state.busy = 0;
      for (const btn of document.querySelectorAll('button')) {{
        btn.disabled = state.busy > 0;
      }}
      try {{
        if (tgWebApp?.MainButton) {{
          if (state.busy > 0) tgWebApp.MainButton.showProgress(false);
          else tgWebApp.MainButton.hideProgress();
        }}
      }} catch (_e) {{}}
    }}
    function applyTelegramTheme() {{
      try {{
        const tg = window.Telegram?.WebApp;
        if (!tg) return;
        tgWebApp = tg;
        document.documentElement.classList.add('tg-webapp');
        const tp = tg.themeParams || {{}};
        const root = document.documentElement.style;
        if (tp.bg_color) root.setProperty('--bg', tp.bg_color);
        if (tp.text_color) root.setProperty('--ink', tp.text_color);
        if (tp.hint_color) root.setProperty('--muted', tp.hint_color);
        if (tp.button_color) root.setProperty('--accent', tp.button_color);
        if (tp.secondary_bg_color) root.setProperty('--panel', tp.secondary_bg_color);
        if (tp.section_bg_color) root.setProperty('--panel', tp.section_bg_color);
        if (tp.destructive_text_color) root.setProperty('--danger', tp.destructive_text_color);
        if (tp.header_bg_color) {{
          const metaTheme = document.querySelector('meta[name="theme-color"]');
          if (metaTheme) metaTheme.setAttribute('content', tp.header_bg_color);
        }}
        if (typeof tg.ready === 'function') tg.ready();
        if (typeof tg.expand === 'function') tg.expand();
        if (typeof tg.setHeaderColor === 'function' && tp.header_bg_color) {{
          try {{ tg.setHeaderColor(tp.header_bg_color); }} catch (_e) {{}}
        }}
        if (typeof tg.setBackgroundColor === 'function' && tp.bg_color) {{
          try {{ tg.setBackgroundColor(tp.bg_color); }} catch (_e) {{}}
        }}
        if (!tgEventsBound && typeof tg.onEvent === 'function') {{
          tgEventsBound = true;
          tg.onEvent('themeChanged', () => {{
            applyTelegramTheme();
            updateSummary();
          }});
          tg.onEvent('viewportChanged', () => {{
            // Sticky panels rely on layout metrics; force reflow-safe status timestamp touch.
            el('lastRefreshBadge')?.offsetHeight;
          }});
        }}
      }} catch (_e) {{}}
    }}
    async function req(url, body=null) {{
      const init = body ? {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}} : {{}};
      const res = await fetch(url + tokenQs, init);
      const data = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(data));
      return data;
    }}
    function fmtTime(v) {{
      if (!v) return '-';
      const d = new Date(v);
      return Number.isNaN(d.getTime()) ? String(v) : `${{d.toLocaleDateString()}} ${{d.toLocaleTimeString()}}`;
    }}
    function boolBadge(v, t='active', f='disabled') {{
      return v
        ? `<span class="badge ok">${{esc(t)}}</span>`
        : `<span class="badge off">${{esc(f)}}</span>`;
    }}
    function jsArg(v) {{
      return JSON.stringify(v ?? '');
    }}
    function updateSummary() {{
      const kwKinds = state.keywords?.kinds || null;
      let kwTotal = 0;
      let kwKindCount = 0;
      if (kwKinds) {{
        for (const values of Object.values(kwKinds)) {{
          kwKindCount += 1;
          kwTotal += Array.isArray(values) ? values.length : 0;
        }}
      }}
      const privateRows = Array.isArray(state.groups?.private) ? state.groups.private : [];
      const publicRows = Array.isArray(state.groups?.public) ? state.groups.public : [];
      const privateActive = privateRows.filter(r => !!r.active).length;
      const publicJoined = publicRows.filter(r => !!r.joined).length;
      const cfgKeys = state.config ? Object.keys(state.config).length : 0;
      el('sumKeywords').textContent = kwKinds ? String(kwTotal) : '-';
      el('sumKeywordKinds').textContent = kwKinds ? `kinds: ${{kwKindCount}}` : 'kinds: -';
      el('sumPrivate').textContent = state.groups ? String(privateRows.length) : '-';
      el('sumPrivateActive').textContent = state.groups ? `active: ${{privateActive}}` : 'active: -';
      el('sumPublic').textContent = state.groups ? String(publicRows.length) : '-';
      el('sumPublicJoined').textContent = state.groups ? `joined: ${{publicJoined}}` : 'joined: -';
      el('sumConfig').textContent = state.configEnabled ? String(cfgKeys) : 'off';
      el('sumConfigMeta').textContent = state.configEnabled ? 'runtime keys' : 'disabled';
      const ts = new Date().toLocaleString();
      el('lastRefreshBadge').textContent = `Last refresh: ${{ts}}`;
    }}
    function renderKeywords() {{
      const holder = el('kwList');
      if (!state.keywords?.kinds) {{
        holder.innerHTML = '<div class="muted">Keywordlar hali yuklanmagan.</div>';
        updateSummary();
        return;
      }}
      const q = (el('kwSearch')?.value || '').trim().toLowerCase();
      const groups = [];
      for (const [kind, rawValues] of Object.entries(state.keywords.kinds)) {{
        const values = Array.isArray(rawValues) ? rawValues : [];
        const filtered = q ? values.filter(v => String(v).toLowerCase().includes(q)) : values;
        const chips = filtered.length
          ? `<div class="chips">${{filtered.map(v => `<span class="chip" title="${{esc(v)}}">${{esc(v)}}</span>`).join('')}}</div>`
          : '<div class="muted">Mos keyword topilmadi</div>';
        groups.push(`<div class="chip-group"><h4>${{esc(kind)}} <span class="muted">(${{values.length}})</span></h4>${{chips}}</div>`);
      }}
      holder.innerHTML = groups.join('') || '<div class="muted">Keywordlar yo\\'q.</div>';
      updateSummary();
    }}
    function renderGroups() {{
      const data = state.groups || {{ private: [], public: [] }};
      const privateFilter = (el('privateSearch')?.value || '').trim().toLowerCase();
      const publicFilter = (el('publicSearch')?.value || '').trim().toLowerCase();

      const privateRows = (data.private || []).filter(row => {{
        if (!privateFilter) return true;
        return String(row.invite_link || '').toLowerCase().includes(privateFilter)
          || String(row.source_chat_id || '').toLowerCase().includes(privateFilter);
      }});
      const pHtml = ['<tr><th>Invite</th><th>Source</th><th>Last Seen</th><th>State</th><th>Action</th></tr>'];
      for (const row of privateRows) {{
        const rawLink = String(row.invite_link || '');
        pHtml.push(`<tr>
          <td class="mono" style="word-break:break-all;">${{esc(rawLink)}}</td>
          <td class="mono">${{esc(row.source_chat_id ?? '-')}}</td>
          <td>${{esc(fmtTime(row.last_seen_at))}}</td>
          <td>${{boolBadge(!!row.active)}}</td>
          <td>
            <div class="card-actions">
              <button class="btn-mini" onclick='privateToggle(${{jsArg(rawLink)}}, ${{!row.active}})'>${{row.active ? 'Disable' : 'Enable'}}</button>
              <button class="btn-mini btn-danger" onclick='privateRemove(${{jsArg(rawLink)}})'>Delete</button>
            </div>
          </td>
        </tr>`);
      }}
      if (!privateRows.length) pHtml.push('<tr><td colspan="5" class="muted">Private invite topilmadi.</td></tr>');
      el('privateTbl').innerHTML = pHtml.join('');

      const publicRows = (data.public || []).filter(row => {{
        if (!publicFilter) return true;
        return String(row.username || '').toLowerCase().includes(publicFilter)
          || String(row.title || '').toLowerCase().includes(publicFilter)
          || String(row.peer_id || '').toLowerCase().includes(publicFilter);
      }});
      const gHtml = ['<tr><th>Username</th><th>Title</th><th>Joined</th><th>Active</th><th>Source</th><th>Error</th><th>Action</th></tr>'];
      for (const row of publicRows) {{
        const rawUsername = String(row.username || '');
        gHtml.push(`<tr>
          <td class="mono">@${{esc(rawUsername || '-')}}</td>
          <td>${{esc(row.title || '-')}}</td>
          <td>${{boolBadge(!!row.joined, 'joined', 'pending')}}</td>
          <td>${{boolBadge(!!row.active)}}</td>
          <td><span class="badge">${{esc(row.source_query || '-')}}</span></td>
          <td class="muted" style="max-width:220px;">${{esc(row.last_error || '-')}}</td>
          <td>
            <div class="card-actions">
              <button class="btn-mini" onclick='publicToggle(${{jsArg(rawUsername)}}, ${{!row.active}})'>${{row.active ? 'Disable' : 'Enable'}}</button>
              <button class="btn-mini btn-danger" onclick='publicRemove(${{jsArg(rawUsername)}})'>Delete</button>
            </div>
          </td>
        </tr>`);
      }}
      if (!publicRows.length) gHtml.push('<tr><td colspan="7" class="muted">Public group topilmadi.</td></tr>');
      el('publicTbl').innerHTML = gHtml.join('');
      updateSummary();
    }}
    async function refreshAll() {{
      setBusy(true);
      try {{
        await Promise.all([loadKeywords(false), loadGroups(false), loadConfig(false)]);
        updateSummary();
        setStatus('Barcha bo\\'limlar yangilandi');
      }} catch (e) {{
        setStatus(String(e), true);
      }} finally {{
        setBusy(false);
      }}
    }}
    async function loadKeywords(showStatus=true) {{
      try {{
        state.keywords = await req('/api/keywords');
        renderKeywords();
        if (showStatus) setStatus('Keywordlar yuklandi');
      }} catch (e) {{
        if (showStatus) setStatus(String(e), true);
        throw e;
      }}
    }}
    async function loadGroups(showStatus=true) {{
      try {{
        state.groups = await req('/api/groups');
        renderGroups();
        if (showStatus) setStatus('Guruhlar ro\\'yxati yangilandi');
      }} catch (e) {{
        if (showStatus) setStatus(String(e), true);
        throw e;
      }}
    }}
    async function loadConfig(showStatus=true) {{
      try {{
        const data = await req('/api/config');
        state.configEnabled = !!data.enabled;
        if (!data.enabled) {{
          state.config = {{}};
          updateSummary();
          if (showStatus) setStatus('Runtime config disabled');
          return;
        }}
        const cfg = data.config || {{}};
        state.config = cfg;
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
        updateSummary();
        if (showStatus) setStatus('Runtime config yuklandi');
      }} catch (e) {{
        if (showStatus) setStatus(String(e), true);
        throw e;
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
        const result = await req('/api/config', {{values}});
        state.config = result.config || state.config;
        state.configEnabled = true;
        updateSummary();
        setStatus('Config saqlandi');
      }} catch (e) {{
        setStatus(String(e), true);
      }}
    }}
    async function kwAdd() {{
      setBusy(true);
      try {{
        const value = kwValue.value.trim();
        if (!value) {{
          setStatus('Keyword bo\\'sh bo\\'lmasin', true);
          return;
        }}
        tgWebApp?.HapticFeedback?.impactOccurred?.('light');
        await req('/api/keywords', {{kind: kwKind.value, value}});
        kwValue.value = '';
        await loadKeywords(false);
        updateSummary();
        setStatus('Keyword qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function kwDelete() {{
      setBusy(true);
      try {{
        const value = kwDeleteValue.value.trim();
        if (!value) {{
          setStatus('O\\'chirish uchun keyword kiriting', true);
          return;
        }}
        tgWebApp?.HapticFeedback?.impactOccurred?.('light');
        await req('/api/keywords/delete', {{kind: kwKind.value, value}});
        kwDeleteValue.value = '';
        await loadKeywords(false);
        updateSummary();
        setStatus('Keyword o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function privateAdd() {{
      setBusy(true);
      try {{
        const invite_link = privateLink.value.trim();
        if (!invite_link) {{
          setStatus('Private invite link kiriting', true);
          return;
        }}
        tgWebApp?.HapticFeedback?.impactOccurred?.('light');
        await req('/api/groups/private/add', {{invite_link}});
        privateLink.value = '';
        await loadGroups(false);
        updateSummary();
        setStatus('Private invite qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function privateRemove(link) {{
      setBusy(true);
      try {{
        tgWebApp?.HapticFeedback?.notificationOccurred?.('warning');
        await req('/api/groups/private/remove', {{invite_link: link}});
        await loadGroups(false);
        updateSummary();
        setStatus('Private invite o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function privateToggle(link, active) {{
      setBusy(true);
      try {{
        tgWebApp?.HapticFeedback?.impactOccurred?.('medium');
        await req('/api/groups/private/toggle', {{invite_link: link, active}});
        await loadGroups(false);
        updateSummary();
        setStatus('Private invite holati yangilandi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function publicAdd() {{
      setBusy(true);
      try {{
        const username = publicUsername.value.trim();
        if (!username) {{
          setStatus('Public username kiriting', true);
          return;
        }}
        tgWebApp?.HapticFeedback?.impactOccurred?.('light');
        await req('/api/groups/public/add', {{username}});
        publicUsername.value = '';
        await loadGroups(false);
        updateSummary();
        setStatus('Public group qo\\'shildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function publicRemove(username) {{
      setBusy(true);
      try {{
        tgWebApp?.HapticFeedback?.notificationOccurred?.('warning');
        await req('/api/groups/public/remove', {{username}});
        await loadGroups(false);
        updateSummary();
        setStatus('Public group o\\'chirildi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    async function publicToggle(username, active) {{
      setBusy(true);
      try {{
        tgWebApp?.HapticFeedback?.impactOccurred?.('medium');
        await req('/api/groups/public/toggle', {{username, active}});
        await loadGroups(false);
        updateSummary();
        setStatus('Public group holati yangilandi');
      }} catch (e) {{ setStatus(String(e), true); }}
      finally {{ setBusy(false); }}
    }}
    applyTelegramTheme();
    refreshAll().catch((e) => setStatus(String(e), true));
  </script>
</body>
</html>
        """
