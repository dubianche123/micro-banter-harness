"""本机维护面板 —— 只绑定 loopback，单独进程，不碰 QQ 连接和模型。

用途是维护者视角的几个高频动作：
  · 看运行状态、预算、群列表；
  · 直接编辑 knowledge/*.md，保存后 bot 下一轮自动重载；
  · 查看每日记忆 MD（只读，摘要仍归压缩流程）；
  · 手动跑一次 DuckDuckGo，检查搜索是否可用。

它不是远程后台：默认只监听 127.0.0.1，并要求本地 token。知识库保存走
storage.atomic_write_text，文件名校验后再落盘，避免路径穿越。
"""

from __future__ import annotations

import html
import hmac
import json
import os
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

import knowledge as knowledge_mod
import search as search_mod
import storage

TOKEN_FILE = "panel_token"
MAX_EDIT_BYTES = 256 * 1024
MAX_MEMORY_BYTES = 512 * 1024
TOKEN_HEADER = "X-Panel-Token"


def load_or_create_token(cfg):
    path = Path(cfg.DATA_DIR) / TOKEN_FILE
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 24:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    storage.atomic_write_text(str(path), token + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _read_text(path, limit=MAX_MEMORY_BYTES):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def _tail(path, lines=40):
    text = _read_text(path, limit=256 * 1024)
    return "\n".join(text.splitlines()[-lines:])


def _groups(state):
    groups = {}
    for gid, slot in (state.get("groups") or {}).items():
        groups.setdefault(gid, {})["slot"] = slot if isinstance(slot, dict) else {}
    for gid in (state.get("digests") or {}):
        groups.setdefault(gid, {})
    for gid, mode in (state.get("modes") or {}).items():
        groups.setdefault(gid, {})["mode"] = mode
    for key in (state.get("relations") or {}):
        gid = str(key).rsplit("|", 1)[0]
        groups.setdefault(gid, {})["relations"] = groups.get(gid, {}).get("relations", 0) + 1
    for gid in (state.get("promises") or {}):
        groups.setdefault(gid, {})["promises"] = len(state["promises"].get(gid) or [])
    return groups


def _knowledge_files(cfg):
    try:
        names = sorted(os.listdir(cfg.KNOWLEDGE_DIR))
    except OSError:
        return []
    return [name for name in names
            if name.endswith(".md") and name != "README.md" and _safe_name(name)]


def _safe_name(name):
    name = str(name or "").strip()
    if name == "all.md":
        return True
    if not name.endswith(".md"):
        return False
    stem = name[:-3]
    return (4 <= len(stem) <= 64
            and all(ch.isalnum() or ch in "_-" for ch in stem))


def _knowledge_path(cfg, name):
    if not _safe_name(name):
        raise web.HTTPBadRequest(text="非法知识库文件名")
    root = os.path.realpath(cfg.KNOWLEDGE_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root:
        raise web.HTTPBadRequest(text="非法知识库路径")
    return path


def _memory_files(cfg):
    try:
        names = os.listdir(cfg.MEMORY_DIR)
    except OSError:
        return []
    rows = []
    for name in names:
        if not name.endswith(".md"):
            continue
        path = os.path.join(cfg.MEMORY_DIR, name)
        try:
            rows.append((os.path.getmtime(path), name))
        except OSError:
            continue
    return [name for _mtime, name in sorted(rows, reverse=True)]


def _status(cfg):
    snapshot = _read_json(cfg.STATUS_FILE) or {}
    state = _read_json(cfg.STATE_FILE) or {}
    return snapshot, state


def _esc(value):
    return html.escape(str(value if value is not None else ""))


def _page(title, body, active=""):
    nav = [
        ("/", "概览", "home"),
        ("/knowledge", "知识库", "knowledge"),
        ("/memory", "记忆", "memory"),
        ("/search", "搜索", "search"),
    ]
    links = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in nav
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · The Regular</title>
<style>
:root {{ color-scheme: light; --bg:#f4f1ea; --card:#fffdf8; --ink:#25231f;
  --muted:#77716a; --line:#ded7cc; --accent:#9a5b35; --bad:#a33b32; --good:#2f6b4f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
header {{ background:var(--card); border-bottom:1px solid var(--line); }}
.wrap {{ max-width:1040px; margin:0 auto; padding:0 20px; }}
header .wrap {{ display:flex; align-items:center; gap:22px; height:56px; }}
.brand {{ font-weight:700; letter-spacing:.02em; }}
nav {{ display:flex; gap:14px; flex-wrap:wrap; }}
nav a {{ color:var(--muted); text-decoration:none; padding:4px 0; }}
nav a.active {{ color:var(--accent); border-bottom:2px solid var(--accent); }}
main {{ padding:26px 0 60px; }}
h1 {{ font-size:22px; margin:0 0 6px; }}
h2 {{ font-size:16px; margin:24px 0 10px; }}
p {{ margin:6px 0; }}
.muted {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.card .label {{ color:var(--muted); font-size:12px; }}
.card .value {{ font-size:19px; font-weight:650; margin-top:2px; }}
.good {{ color:var(--good); }} .bad {{ color:var(--bad); }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }}
th {{ color:var(--muted); font-size:12px; font-weight:600; }}
tr:last-child td {{ border-bottom:0; }}
code,.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
pre {{ background:#292721; color:#eee8dc; padding:14px; border-radius:10px;
  overflow:auto; white-space:pre-wrap; word-break:break-word; }}
textarea {{ width:100%; min-height:420px; padding:12px; border:1px solid var(--line);
  border-radius:10px; font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--card); color:var(--ink); }}
input[type=text] {{ padding:9px 11px; border:1px solid var(--line); border-radius:8px;
  background:var(--card); color:var(--ink); min-width:260px; }}
button {{ padding:9px 16px; border:0; border-radius:8px; background:var(--accent);
  color:white; font-weight:600; cursor:pointer; }}
.row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
.notice {{ padding:10px 14px; border-radius:9px; border:1px solid var(--line);
  background:var(--card); margin:12px 0; }}
.notice.bad {{ border-color:#d9b3ae; color:var(--bad); }}
.notice.good {{ border-color:#b8d3c5; color:var(--good); }}
.links a {{ color:var(--accent); }}
</style></head><body>
<header><div class="wrap"><div class="brand">The Regular · 维护面板</div>
<nav>{links}</nav></div></header>
<main><div class="wrap">{body}</div></main>
</body></html>"""


def _status_cards(snapshot, state):
    budget = snapshot.get("budget") or {}
    providers = snapshot.get("providers") or {}
    main = (providers.get("providers") or [{}])[0]
    age = time.time() - float(snapshot.get("updated_at") or 0) if snapshot else None
    age_text = "无快照" if age is None else f"{age:.0f}s 前"
    lifecycle = snapshot.get("lifecycle") or "unknown"
    gateway = snapshot.get("gateway_connected")
    budget_text = f"{budget.get('used', '?')} / {budget.get('limit', '?')}"
    model = (main.get("models") or ["?"])[0]
    vision = providers.get("vision") or {}
    groups = len(_groups(state))
    cards = [
        ("运行状态", lifecycle + (" · 网关在线" if gateway else " · 网关离线")),
        ("快照", age_text),
        ("今日预算", budget_text),
        ("主模型", f"{main.get('label') or main.get('name') or '?'} · {model}"),
        ("读图", f"{vision.get('model') or '已关闭'}"),
        ("群数量", str(groups)),
    ]
    return "".join(
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div></div>'
        for label, value in cards)


def _dashboard(cfg):
    snapshot, state = _status(cfg)
    groups = _groups(state)
    rows = []
    for gid in sorted(groups):
        info = groups[gid]
        last = (info.get("slot") or {}).get("last_seen")
        last_text = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "—"
        digest = (state.get("digests") or {}).get(gid) or {}
        updated = (digest.get("summary") or {}).get("updated") if isinstance(digest, dict) else None
        updated_text = time.strftime("%m-%d %H:%M", time.localtime(updated)) if updated else "—"
        kfile = f"{gid}.md"
        klink = (f'<a href="/knowledge?file={_esc(kfile)}">{_esc(kfile)}</a>'
                 if kfile in _knowledge_files(cfg) else '<span class="muted">未创建</span>')
        rows.append(
            f"<tr><td class=mono>{_esc(gid)}</td><td>{_esc(info.get('mode') or '—')}</td>"
            f"<td>{info.get('relations', 0)}</td><td>{info.get('promises', 0)}</td>"
            f"<td>{_esc(last_text)}</td><td>{_esc(updated_text)}</td><td>{klink}</td></tr>")
    if not rows:
        group_table = '<p class="muted">还没有群数据。</p>'
    else:
        group_table = ("<table><thead><tr><th>群号</th><th>模式</th><th>关系档案</th>"
                       "<th>承诺</th><th>最近活动</th><th>摘要更新</th><th>知识库</th></tr></thead>"
                       "<tbody>" + "".join(rows) + "</tbody></table>")
    files = []
    for name in _knowledge_files(cfg):
        path = os.path.join(cfg.KNOWLEDGE_DIR, name)
        try:
            stat = os.stat(path)
            files.append(f"<tr><td><a href=\"/knowledge?file={_esc(name)}\">{_esc(name)}</a></td>"
                         f"<td>{stat.st_size} B</td>"
                         f"<td>{time.strftime('%m-%d %H:%M', time.localtime(stat.st_mtime))}</td></tr>")
        except OSError:
            continue
    knowledge_table = ("<table><thead><tr><th>文件</th><th>大小</th><th>更新</th></tr></thead><tbody>"
                       + "".join(files) + "</tbody></table>") if files else '<p class="muted">知识库还没有文件。</p>'
    log = _tail(cfg.LOG_FILE)
    body = f"""
<h1>概览</h1>
<p class="muted">只读状态来自 status.json；知识库保存后 bot 下一轮自动重载。</p>
<div class="grid">{_status_cards(snapshot, state)}</div>
<h2>群</h2>{group_table}
<h2>知识库</h2>{knowledge_table}
<h2>最近日志</h2><pre>{_esc(log or "暂无日志")}</pre>
"""
    return _page("概览", body, "home")


def _knowledge(cfg, request):
    files = _knowledge_files(cfg)
    name = request.query.get("file") or (files[0] if files else "all.md")
    if not _safe_name(name):
        raise web.HTTPBadRequest(text="非法知识库文件名")
    path = _knowledge_path(cfg, name)
    content = _read_text(path, limit=MAX_EDIT_BYTES)
    parsed = knowledge_mod.KnowledgeBase(cfg.KNOWLEDGE_DIR).parse_file(path)
    entries = "".join(
        f"<li><b>{_esc(entry['title'])}</b> "
        f"<span class=mono>{_esc('、'.join(entry['keywords']))}</span><br>"
        f"{_esc(entry['content'])}</li>" for entry in parsed)
    preview = (f"<ul>{entries}</ul>" if entries
               else '<p class="muted">当前文件没有可解析条目。</p>')
    saved = ('<div class="notice good">已保存；下一条相关消息会使用新内容。</div>'
             if request.query.get("saved") else "")
    options = "".join(
        f'<option value="{_esc(item)}"{" selected" if item == name else ""}>{_esc(item)}</option>'
        for item in files)
    body = f"""
<h1>知识库</h1>
<p class="muted">all.md 全群共享；&lt;群号&gt;.md 只对该群生效。每个 ## 标题一条，
关键词行写成 <span class=mono>关键词：奶茶、请客</span>。保存后下一轮生效，不需要重启。</p>
{saved}
<div class="row" style="margin:14px 0">
<form method="get" action="/knowledge" class="row">
<select name="file">{options}</select>
<button type="submit">打开</button>
</form>
<form method="get" action="/knowledge" class="row">
<input type="text" name="file" placeholder="新建：群号.md">
<button type="submit">新建</button>
</form>
</div>
<form method="post" action="/knowledge/save">
<input type="hidden" name="file" value="{_esc(name)}">
<textarea name="content" spellcheck="false">{_esc(content)}</textarea>
<div class="row" style="margin-top:10px">
<button type="submit">保存 {_esc(name)}</button>
<span class="muted">解析到 {len(parsed)} 条；敏感词条目会被跳过。</span>
</div>
</form>
<h2>解析预览</h2>{preview}
"""
    return _page("知识库", body, "knowledge")


async def _knowledge_save(cfg, request):
    form = await request.post()
    name = str(form.get("file") or "")
    content = str(form.get("content") or "")
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise web.HTTPBadRequest(text="文件过大")
    path = _knowledge_path(cfg, name)
    storage.atomic_write_text(path, content)
    raise web.HTTPFound(f"/knowledge?file={quote(name, safe='')}&saved=1")


def _memory(cfg, request):
    files = _memory_files(cfg)
    name = request.query.get("file") or (files[0] if files else "")
    content = ""
    if name:
        if os.path.basename(name) != name or name not in files:
            raise web.HTTPBadRequest(text="非法记忆文件名")
        content = _read_text(os.path.join(cfg.MEMORY_DIR, name))
    options = "".join(
        f'<option value="{_esc(item)}"{" selected" if item == name else ""}>{_esc(item)}</option>'
        for item in files)
    picker = (f'<form method="get" action="/memory" class="row"><select name="file">'
              f'{options}</select><button type="submit">打开</button></form>') if files else ""
    body = f"""
<h1>记忆</h1>
<p class="muted">这里只读。摘要由每日压缩生成；要纠正具体事实，请改知识库。</p>
{picker}
<pre>{_esc(content or "暂无可显示的每日记忆。")}</pre>
"""
    return _page("记忆", body, "memory")


async def _search(cfg, request):
    query = (request.query.get("q") or "").strip()[:120]
    rows = ""
    notice = ""
    if query:
        if not cfg.SEARCH_ENABLED:
            notice = '<div class="notice bad">搜索已关闭（SEARCH_ENABLED=false）。</div>'
        else:
            results = await search_mod.search_web(query)
            if results is None:
                notice = '<div class="notice bad">搜索失败：网络或 DuckDuckGo 不可用。</div>'
            elif not results:
                notice = '<div class="notice bad">没有找到结果。</div>'
            else:
                rows = "<table><thead><tr><th>结果</th></tr></thead><tbody>" + "".join(
                    f'<tr><td><a href="{_esc(row["url"])}" target="_blank" '
                    f'rel="noreferrer noopener">{_esc(row["title"])}</a><br>'
                    f'<span class="muted">{_esc(row["snippet"])}</span></td></tr>'
                    for row in results) + "</tbody></table>"
    body = f"""
<h1>搜索调试</h1>
<p class="muted">这里只验证 DuckDuckGo 是否可用；群里的正式入口是 <span class=mono>搜索 关键词</span>。</p>
<form method="get" action="/search" class="row">
<input type="text" name="q" value="{_esc(query)}" placeholder="关键词">
<button type="submit">搜索</button>
</form>
{notice}{rows}
"""
    return _page("搜索", body, "search")


def create_app(cfg, *, token):
    @web.middleware
    async def auth(request, handler):
        supplied = (request.query.get("token") or request.headers.get(TOKEN_HEADER)
                    or request.cookies.get("panel_token"))
        if not supplied or not hmac.compare_digest(str(supplied), str(token)):
            raise web.HTTPUnauthorized(text="Panel token required.\n")
        if request.query.get("token"):
            query = {k: v for k, v in request.query.items() if k != "token"}
            target = request.rel_url.with_query(query)
            response = web.HTTPFound(str(target))
            response.set_cookie("panel_token", token, httponly=True,
                                samesite="Strict", path="/")
            raise response
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        return response

    async def home(request):
        return _html(_dashboard(cfg))

    async def knowledge(request):
        return _html(_knowledge(cfg, request))

    async def knowledge_save(request):
        return await _knowledge_save(cfg, request)

    async def memory(request):
        return _html(_memory(cfg, request))

    async def search(request):
        return _html(await _search(cfg, request))

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", home)
    app.router.add_get("/knowledge", knowledge)
    app.router.add_post("/knowledge/save", knowledge_save)
    app.router.add_get("/memory", memory)
    app.router.add_get("/search", search)
    return app


def _html(response):
    return web.Response(text=response, content_type="text/html", charset="utf-8")


def run(cfg, *, host="127.0.0.1", port=8765, open_browser=False):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("panel only binds to loopback addresses")
    token = load_or_create_token(cfg)
    app = create_app(cfg, token=token)
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{int(port)}/?token={token}"
    print(f"Panel: {url}")
    print("Local only. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    web.run_app(app, host=host, port=int(port), print=None)
