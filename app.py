import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, copy_current_request_context, jsonify, redirect, request, session

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "public"),
    static_url_path="",
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # nenhum envio do site passa de 16 KB
# Sessão do login (cookie assinado). A chave sai do token do bot, então nunca fica em lugar nenhum além da Vercel;
# se o token for trocado, todo mundo só precisa entrar de novo.
_tk = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
app.secret_key = hashlib.sha256(("kyzensa-sessao:" + _tk).encode()).digest() if _tk else secrets.token_bytes(32)
app.config.update(SESSION_COOKIE_NAME="kz", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")), PERMANENT_SESSION_LIFETIME=timedelta(days=7))


# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------
def debug_ok():
    """Diagnóstico só com ?debug=<DEBUG_KEY>. Sem a variável DEBUG_KEY, fica desligado."""
    chave = os.environ.get("DEBUG_KEY", "")
    return len(chave) >= 16 and hmac.compare_digest(request.args.get("debug", ""), chave)


_falhas = {}


def pode_tentar(nome, espera=30):
    """Depois de uma falha do Discord, espera alguns segundos antes de tentar de novo."""
    return time.time() - _falhas.get(nome, 0) > espera


def falhou(nome):
    _falhas[nome] = time.time()


def url_segura(u):
    return u if isinstance(u, str) and re.match(r"^https?://[^\s\"'<>]+$", u) else ""


def csp_da_pagina(html):
    """CSP com o hash exato do script da página: só esse script pode rodar."""
    hashes = []
    for s in re.findall(r"<script>(.*?)</script>", html, re.S):
        h = base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode()
        hashes.append(f"'sha256-{h}'")
    return ("default-src 'self'; "
            f"script-src 'self' {' '.join(hashes)}; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "media-src https://cdn.discordapp.com https://media.discordapp.net; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'; "
            "upgrade-insecure-requests")


@app.after_request
def cabecalhos(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return resp

# ---------------------------------------------------------------------------
# Configuração (variáveis de ambiente na Vercel)
#   DISCORD_BOT_TOKEN  -> token do bot
#   ANNOUNCE_CHANNELS  -> "ID_CANAL:TAG,ID_CANAL:TAG"  (a TAG é opcional)
#                         ex: 1111111111:ANÚNCIO,2222222222:EVENTO
#   DISCORD_GUILD_ID   -> ID do servidor (para o link da mensagem)
# ---------------------------------------------------------------------------
API = "https://discord.com/api/v10"
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_cache = {"t": 0, "data": []}
CACHE_SECONDS = 60


def pagina():
    for pasta in ("templates", "teamplates"):
        arq = BASE_DIR / pasta / "index.html"
        if arq.exists():
            return arq.read_text(encoding="utf-8")
    return "index.html não encontrado", 500


def discord_get(path, token):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bot {token}", "User-Agent": "KyzenSA-Site (1.0)"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def discord_post(path, token, dados):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(dados).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bot {token}", "User-Agent": "KyzenSA-Site (1.0)",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def limpar(txt):
    txt = txt or ""
    txt = re.sub(r"(?m)^\s*(?:#{1,3}|-#)\s+", "", txt)  # títulos e subtexto do Discord
    txt = re.sub(r"<a?:\w+:\d+>", "", txt)              # emojis custom
    txt = re.sub(r"<@[!&]?\d+>|<#\d+>", "", txt)         # menções
    txt = re.sub(r"@(everyone|here)", "", txt)
    txt = re.sub(r"https?://\S+", "", txt)               # links soltos
    txt = re.sub(r"[*_~`>|]+", "", txt)                  # markdown
    return re.sub(r"\s+", " ", txt).strip()


def cortar(txt, n):
    return txt if len(txt) <= n else txt[: n - 1].rstrip() + "…"


def imagem_da(msg):
    """Miniatura do vídeo/embed ou primeira imagem anexada."""
    for emb in msg.get("embeds") or []:
        for k in ("image", "thumbnail"):
            u = (emb.get(k) or {}).get("url")
            if u:
                return u
    for a in msg.get("attachments") or []:
        if (a.get("content_type") or "").startswith("image/") and a.get("url"):
            return a["url"]
    return ""


def primeiro_link(msg):
    """Primeiro link externo da mensagem (texto ou embed)."""
    textos = [msg.get("content") or ""]
    for e in msg.get("embeds") or []:
        textos += [e.get("url") or "", e.get("description") or ""]
    for t in textos:
        for u in re.findall(r"https?://[^\s<>()\]]+", t):
            if "discord.com/channels" not in u:
                return u
    return ""


def montar(msg, tag, guild_id, canal_id):
    emb = (msg.get("embeds") or [{}])[0]
    conteudo = limpar(msg.get("content"))
    corpo = limpar(emb.get("description")) or conteudo
    titulo = limpar(emb.get("title"))
    if not titulo:
        primeira = next((l for l in (msg.get("content") or "").splitlines() if limpar(l)), "")
        titulo = limpar(primeira)
    if not titulo:
        titulo = "Novo vídeo" if "VÍDEO" in tag.upper() or "VIDEO" in tag.upper() else "Novo anúncio"
    resto = corpo[len(titulo):].strip(" -–—:") if corpo.startswith(titulo) else corpo
    eh_video = bool(emb.get("video")) or emb.get("type") == "video"
    if eh_video:
        # em vídeos, mostra o nome do canal (autor) em vez da descrição enorme
        resto = limpar((emb.get("author") or {}).get("name")) or limpar((emb.get("provider") or {}).get("name"))
    d = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
    data = f"{d.day} {MESES[d.month - 1]} {d.year}"
    if resto:
        data += " · " + cortar(resto, 110)
    link = f"https://discord.com/channels/{guild_id}/{canal_id}/{msg['id']}"
    if eh_video and emb.get("url"):
        link = emb["url"]                      # abre o vídeo direto
    return {
        "titulo": cortar(titulo, 90),
        "data": data,
        "tag": tag,
        "link": url_segura(link),
        "img": url_segura(imagem_da(msg)),
        "ext": url_segura(primeiro_link(msg)),
        "_ts": msg["timestamp"],
    }


FEEDS_AUTO = [("SUB-ANÚNCIO", "sub-anuncios"), ("EVENTO", "eventos"), ("JORNAL", "jornal"),
              ("CREW", "crew-post"), ("ANÚNCIO DA CREW", "anuncios-crew")]
CREW_TAGS = {"CREW", "ANÚNCIO DA CREW"}  # vêm da categoria Crew (privada): só aparecem para quem tem acesso


def buscar_anuncios(diag=None):
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    canais = [c.strip() for c in os.environ.get("ANNOUNCE_CHANNELS", "").split(",") if c.strip()]
    if diag is not None:
        diag["token_configurado"] = bool(token)
        diag["canais_configurados"] = len(canais)
    if not token or not canais:
        return None
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    # canais que entram sozinhos (pelo nome), se não estiverem no ANNOUNCE_CHANNELS
    tags = {c.partition(":")[2].strip().upper() for c in canais}
    for tag, nome in FEEDS_AUTO:
        if tag not in tags and guild_id:
            cid = achar_canal(token, guild_id, nome)
            if cid:
                canais.append(f"{cid}:{tag}")
    itens = []
    for c in canais:
        canal_id, _, tag = c.partition(":")
        canal_id, tag = canal_id.strip(), (tag.strip() or "ANÚNCIO")
        info = {"canal": canal_id[:4] + "…" + canal_id[-4:], "tag": tag}
        try:
            if not guild_id:
                guild_id = str(discord_get(f"/channels/{canal_id}", token).get("guild_id", "@me"))
            msgs = discord_get(f"/channels/{canal_id}/messages?limit=10", token)
            info["mensagens_lidas"] = len(msgs)
            info["com_texto"] = sum(1 for m in msgs if m.get("content") or m.get("embeds"))
            if msgs and not info["com_texto"]:
                info["aviso"] = "Mensagens vazias: ligue o Message Content Intent no Developer Portal (Bot)."
        except urllib.error.HTTPError as e:
            info["erro_http"] = e.code
            info["significa"] = {
                401: "Token do bot inválido (gere um novo em Bot > Redefinir token).",
                403: "Bot sem permissão no canal (Ver canal + Ler histórico de mensagens) ou não está no servidor.",
                404: "ID do canal errado (use o ID do CANAL, não do servidor).",
            }.get(e.code, "Erro do Discord.")
            msgs = []
        except (urllib.error.URLError, ValueError, KeyError) as e:
            info["erro"] = str(e)[:120]
            msgs = []
        if diag is not None:
            diag.setdefault("canais", []).append(info)
        do_canal = [montar(m, tag, guild_id, canal_id) for m in msgs if m.get("content") or m.get("embeds")]
        itens.extend(do_canal[:6])
    itens.sort(key=lambda i: i["_ts"], reverse=True)
    for i in itens:
        i.pop("_ts")
    return itens


@app.route("/api/novidades")
def novidades():
    if debug_ok():
        diag = {}
        dados = buscar_anuncios(diag)
        resp = jsonify({"configured": dados is not None, "total": len(dados or []), "diagnostico": diag})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    agora = time.time()
    if agora - _cache["t"] > CACHE_SECONDS:
        dados = buscar_anuncios()
        if dados is None:
            resp = jsonify({"news": [], "configured": False})
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if dados or not _cache["data"]:
            _cache.update(t=agora, data=dados)
        if dados:
            try:
                avisar_anuncios(dados)
            except Exception as e:  # noqa: BLE001  (notificação nunca pode derrubar a página)
                app.logger.error("push anuncios: %s", str(e)[:120])
    publicas = [n for n in _cache["data"] or [] if n.get("tag") not in CREW_TAGS]
    resp = jsonify({"news": publicas, "configured": True})
    resp.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=300"
    return resp


# ---------------------------------------------------------------------------
# Canais do Discord -> páginas do site, com visual do Discord (/api/canal/<slug>)
#   REQUISITOS_CHANNEL -> ID do canal de requisitos (# req-cc)
#   NIVEIS_CHANNEL     -> ID do canal de níveis (# leveis-embed); opcional, o bot acha pelo nome
# ---------------------------------------------------------------------------
_req_cache = {}
_TOKEN_RE = re.compile(
    r"<(a?):(\w+):(\d+)>"                    # 1-3 emoji custom
    r"|<@&(\d+)>"                            # 4 cargo
    r"|<@!?(\d+)>"                           # 5 usuário
    r"|<#(\d+)>"                             # 6 canal
    r"|<t:(-?\d+)(?::[tTdDfFR])?>"           # 7 timestamp
    r"|\[([^\]\n]+)\]\(<?(https?://[^\s)>]+)>?\)"  # 8-9 link com texto
    r"|<?(https?://[^\s<>]+[^\s<>.,;:!?)\]'\"])>?"  # 10 link solto
    r"|`([^`\n]+)`"                          # 11 código
)


EMOJIS = dict(zip(
    "sparkles star star2 dizzy fire boom zap tada confetti_ball trophy medal first_place second_place third_place "
    "crown gem moneybag money_with_wings coin rocket crossed_swords dagger shield bow_and_arrow anchor ship "
    "sailboat ocean pirate_flag skull skull_and_crossbones crossbones warning no_entry x white_check_mark "
    "heavy_check_mark ballot_box_with_check check exclamation question bangbang red_circle orange_circle "
    "yellow_circle green_circle blue_circle purple_circle black_circle white_circle heart orange_heart "
    "yellow_heart green_heart blue_heart purple_heart black_heart white_heart sparkling_heart pushpin "
    "round_pushpin paperclip link memo pencil pencil2 clipboard scroll page_facing_up bookmark books book "
    "bell loudspeaker mega speech_balloon calendar date alarm_clock hourglass stopwatch gift balloon "
    "video_camera movie_camera camera tv film_frames clapper microphone headphones musical_note notes "
    "video_game joystick game_die dart chart_with_upwards_trend bar_chart arrow_right arrow_left arrow_up "
    "arrow_down point_right point_left point_up point_down wave clap thumbsup thumbsdown pray muscle eyes "
    "100 new up cool free ok sos id lock unlock key gear tools hammer wrench mag rotating_light "
    "busts_in_silhouette bust_in_silhouette sunglasses smile smiley grin joy wink heart_eyes".split(),
    "✨ ⭐ 🌟 💫 🔥 💥 ⚡ 🎉 🎊 🏆 🏅 🥇 🥈 🥉 👑 💎 💰 💸 🪙 🚀 ⚔️ 🗡️ 🛡️ 🏹 ⚓ 🚢 ⛵ 🌊 🏴‍☠️ 💀 ☠️ ☠️ ⚠️ ⛔ ❌ ✅ "
    "✔️ ☑️ ✔️ ❗ ❓ ‼️ 🔴 🟠 🟡 🟢 🔵 🟣 ⚫ ⚪ ❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💖 📌 📍 📎 🔗 📝 ✏️ ✏️ 📋 📜 📄 🔖 📚 📖 "
    "🔔 📢 📣 💬 📅 📆 ⏰ ⏳ ⏱️ 🎁 🎈 📹 🎥 📷 📺 🎞️ 🎬 🎤 🎧 🎵 🎶 🎮 🕹️ 🎲 🎯 📈 📊 ➡️ ⬅️ ⬆️ ⬇️ 👉 👈 ☝️ 👇 "
    "👋 👏 👍 👎 🙏 💪 👀 💯 🆕 🆙 🆒 🆓 🆗 🆘 🆔 🔒 🔓 🔑 ⚙️ 🛠️ 🔨 🔧 🔍 🚨 👥 👤 😎 😄 😃 😁 😂 😉 😍".split(),
))


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def cor_hex(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    return f"#{n:06x}" if 0 < n <= 0xFFFFFF else ""


def inline_md(txt, ctx):
    """Markdown do Discord (uma linha) -> HTML seguro."""
    parts = []

    def guardar(html):
        parts.append(html)
        return f"\x00{len(parts) - 1}\x00"

    def troca(m):
        if m.group(3):
            ext = "gif" if m.group(1) else "webp"
            return guardar(f'<img class="em" src="https://cdn.discordapp.com/emojis/{m.group(3)}.{ext}?size=48" '
                           f'alt=":{esc(m.group(2))}:" title=":{esc(m.group(2))}:">')
        if m.group(4):
            nome, cor = ctx["roles"].get(m.group(4), ("cargo", ""))
            st = f' style="--rc:{cor}"' if cor else ""
            return guardar(f'<span class="mn role"{st}>@{esc(nome)}</span>')
        if m.group(5):
            return guardar(f'<span class="mn">@{esc(ctx["users"].get(m.group(5), "usuário"))}</span>')
        if m.group(6):
            return guardar(f'<span class="mn">#{esc(ctx["channels"].get(m.group(6), "canal"))}</span>')
        if m.group(7):
            try:
                d = datetime.fromtimestamp(int(m.group(7)), timezone(timedelta(hours=-3)))
                return guardar(f'<span class="ts">{d.day} {MESES[d.month - 1]} {d.year}</span>')
            except (ValueError, OverflowError, OSError):
                return ""
        if m.group(9):
            txt = m.group(8)  # texto que já é um link não vira outro link dentro do link
            dentro = esc(txt) if re.match(r"\s*<?https?://", txt) else inline_md(txt, ctx)
            return guardar(f'<a href="{esc(m.group(9))}" target="_blank" rel="noopener nofollow">{dentro}</a>')
        if m.group(10):
            u = m.group(10)
            return guardar(f'<a href="{esc(u)}" target="_blank" rel="noopener nofollow">{esc(u)}</a>')
        if m.group(11):
            return guardar(f"<code>{esc(m.group(11))}</code>")
        return m.group(0)

    s = esc(_TOKEN_RE.sub(troca, txt))
    s = re.sub(r":([a-z0-9_+-]+):", lambda m: EMOJIS.get(m.group(1), m.group(0)), s)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r'<span class="sp">\1</span>', s)
    for _ in range(3):  # placeholders podem conter outros placeholders
        s = re.sub("\x00(\\d+)\x00", lambda m: parts[int(m.group(1))], s)
    return s


def discord_md(txt, ctx):
    """Markdown do Discord (texto completo) -> HTML seguro."""
    out, lista, quote_all = [], None, False
    linhas = (txt or "").replace("\r\n", "\n").split("\n")
    i = 0

    def fecha_lista():
        nonlocal lista
        if lista:
            out.append(f"</{lista}>")
            lista = None

    while i < len(linhas):
        ln = linhas[i]
        if ln.strip().startswith("```"):
            fecha_lista()
            bloco, i = [], i + 1
            while i < len(linhas) and not linhas[i].strip().startswith("```"):
                bloco.append(linhas[i])
                i += 1
            out.append(f"<pre><code>{esc(chr(10).join(bloco))}</code></pre>")
            i += 1
            continue
        q = quote_all
        if ln.startswith(">>> "):
            quote_all = q = True
            ln = ln[4:]
        elif ln.startswith("> ") or ln == ">":
            q, ln = True, ln[2:]
        m_h = re.match(r"^(#{1,3}) (.+)", ln)
        m_ul = re.match(r"^(\s*)[-*•] (.+)", ln)
        m_ol = re.match(r"^(\s*)(\d+)\. (.+)", ln)
        if m_ul or m_ol:
            tipo = "ul" if m_ul else "ol"
            if lista != tipo:
                fecha_lista()
                out.append(f"<{tipo}>")
                lista = tipo
            m = m_ul or m_ol
            nivel = " class=\"sub\"" if len(m.group(1)) >= 2 else ""
            out.append(f"<li{nivel}>{inline_md(m.group(m.lastindex), ctx)}</li>")
        else:
            fecha_lista()
            if m_h:
                n = len(m_h.group(1))
                html = f'<div class="h{n}">{inline_md(m_h.group(2), ctx)}</div>'
            elif ln.startswith("-# "):
                html = f'<div class="small">{inline_md(ln[3:], ctx)}</div>'
            elif ln.strip():
                html = f"<div>{inline_md(ln, ctx)}</div>"
            else:
                html = '<div class="gap"></div>'
            out.append(f"<blockquote>{html}</blockquote>" if q else html)
        i += 1
    fecha_lista()
    return "".join(out)


def url_ok(u):
    return isinstance(u, str) and u.startswith("https://")


def render_msg(msg, ctx):
    html = discord_md(msg.get("content"), ctx) if msg.get("content") else ""
    for emb in msg.get("embeds") or []:
        if emb.get("type") not in (None, "rich", "image", "article", "link", "video", "gifv"):
            continue
        cor = cor_hex(emb.get("color")) or "#1e1f22"
        e = []
        aut = (emb.get("author") or {}).get("name")
        if aut:
            e.append(f'<div class="ea">{esc(aut)}</div>')
        if emb.get("title"):
            t = inline_md(emb["title"], ctx)
            if url_ok(emb.get("url")):
                t = f'<a href="{esc(emb["url"])}" target="_blank" rel="noopener nofollow">{t}</a>'
            e.append(f'<div class="et">{t}</div>')
        if emb.get("description"):
            e.append(f'<div class="ed">{discord_md(emb["description"], ctx)}</div>')
        campos = emb.get("fields") or []
        if campos:
            fs = "".join(
                f'<div class="ef{" il" if f.get("inline") else ""}"><div class="en">{inline_md(f.get("name"), ctx)}</div>'
                f'<div>{discord_md(f.get("value"), ctx)}</div></div>'
                for f in campos
            )
            e.append(f'<div class="efs">{fs}</div>')
        img = (emb.get("image") or {}).get("url") or ""
        thumb = (emb.get("thumbnail") or {}).get("url") or ""
        if emb.get("type") in ("image", "gifv") and not img:
            img, thumb = thumb, ""
        if url_ok(img):
            e.append(f'<img class="ei" src="{esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">')
        rod = (emb.get("footer") or {}).get("text")
        if rod:
            e.append(f'<div class="eft">{esc(rod)}</div>')
        tb = (f'<img class="eth" src="{esc(thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
              if url_ok(thumb) else "")
        if e or tb:
            html += f'<div class="emb" style="--ec:{cor}"><div class="eb">{"".join(e)}</div>{tb}</div>'
    for a in msg.get("attachments") or []:
        if (a.get("content_type") or "").startswith("image/") and url_ok(a.get("url")):
            html += f'<img class="att" src="{esc(a["url"])}" alt="" loading="lazy" referrerpolicy="no-referrer">'
    return html


def contexto(token, guild_id, msgs):
    """Nomes de cargos, canais e usuários para as menções."""
    ctx = {"roles": {}, "channels": {}, "users": {}}
    if guild_id:
        try:
            for r in discord_get(f"/guilds/{guild_id}/roles", token):
                cor = cor_hex((r.get("colors") or {}).get("primary_color")) or cor_hex(r.get("color"))
                ctx["roles"][r["id"]] = (r.get("name", "cargo"), cor)
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            pass
        try:
            for c in discord_get(f"/guilds/{guild_id}/channels", token):
                ctx["channels"][c["id"]] = c.get("name", "canal")
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            pass
    for m in msgs:
        for u in m.get("mentions") or []:
            ctx["users"][u["id"]] = u.get("global_name") or u.get("username") or "usuário"
    return ctx


# Páginas do site que espelham um canal do Discord.
#   slug -> (variável de ambiente com o ID do canal, nome do canal para achar sozinho)
PAGINAS = {
    "requisitos": ("REQUISITOS_CHANNEL", "req-cc"),
    "niveis": ("NIVEIS_CHANNEL", "leveis-embed"),
    "jornal": ("JORNAL_CHANNEL", "jornal"),
    "regras": ("REGRAS_CHANNEL", "regras"),
    "aliados": ("ALIADOS_CHANNEL", "aliados"),
    "inimigos": ("INIMIGOS_CHANNEL", "inimigos"),
    "titulos": ("TITULOS_CHANNEL", "titulos"),
}


_canais_cache = {"t": 0, "data": None}


def achar_canal(token, guild_id, nome):
    """Procura o ID do canal pelo nome, quando a variável não foi criada."""
    def norm(s):
        s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]", "", s.lower())

    try:
        if time.time() - _canais_cache["t"] > 300 or not _canais_cache["data"]:
            _canais_cache.update(t=time.time(), data=discord_get(f"/guilds/{guild_id}/channels", token))
        for c in _canais_cache["data"]:
            if c.get("type") in (0, 5) and norm(c.get("name")).endswith(norm(nome)):
                return c["id"]
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        pass
    return ""


def buscar_canal(slug, diag=None):
    var, nome = PAGINAS[slug]
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    canal = os.environ.get(var, "").strip()
    if not canal and token and guild_id:
        canal = achar_canal(token, guild_id, nome)
        if diag is not None:
            diag["canal_achado_pelo_nome"] = bool(canal)
    if diag is not None:
        diag["token_configurado"] = bool(token)
        diag["canal_configurado"] = bool(canal)
    if not token or not canal:
        return None
    try:
        if not guild_id:
            guild_id = str(discord_get(f"/channels/{canal}", token).get("guild_id", ""))
        msgs = discord_get(f"/channels/{canal}/messages?limit=20", token)
    except urllib.error.HTTPError as e:
        if diag is not None:
            diag["erro_http"] = e.code
            diag["significa"] = {
                401: "Token do bot inválido.",
                403: f"Bot sem permissão no canal #{nome} (Ver canal + Ler histórico de mensagens).",
                404: f"ID do canal errado na variável {var}.",
            }.get(e.code, "Erro do Discord.")
        return {"html": "", "link": ""}
    except (urllib.error.URLError, ValueError, KeyError) as e:
        if diag is not None:
            diag["erro"] = str(e)[:120]
        return {"html": "", "link": ""}
    msgs = [m for m in msgs if m.get("content") or m.get("embeds") or m.get("attachments")]
    msgs.reverse()  # ordem do Discord: mais antiga em cima
    if diag is not None:
        diag["mensagens"] = len(msgs)
        diag["com_texto"] = sum(1 for m in msgs if m.get("content") or m.get("embeds"))
    ctx = contexto(token, guild_id, msgs)
    html = "".join(f'<div class="dm">{render_msg(m, ctx)}</div>' for m in msgs)
    link = f"https://discord.com/channels/{guild_id}/{canal}" if guild_id else ""
    return {"html": html, "link": link}


@app.route("/api/canal/<slug>")
def canal_api(slug):
    if slug not in PAGINAS:
        return jsonify({"configured": False, "html": "", "link": ""}), 404
    if slug in PAGINAS_CREW:
        bloqueio = sem_acesso_crew()
        if bloqueio:
            return bloqueio
    if debug_ok():
        diag = {}
        dados = buscar_canal(slug, diag)
        resp = jsonify({"configured": dados is not None, "tamanho_html": len((dados or {}).get("html", "")),
                        "diagnostico": diag})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    cache = _req_cache.setdefault(slug, {"t": 0, "data": None})
    agora = time.time()
    if agora - cache["t"] > CACHE_SECONDS or cache["data"] is None:
        dados = buscar_canal(slug)
        if dados is None:
            resp = jsonify({"configured": False, "html": "", "link": ""})
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if dados["html"] or not cache["data"]:
            cache.update(t=agora, data=dados)
    resp = jsonify({"configured": True, **cache["data"]})
    if slug in PAGINAS_CREW:
        return privado(resp)
    resp.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=300"
    return resp


PAGINAS_CREW = {"aliados", "inimigos", "titulos"}  # canais da categoria Crew


_srv_cache = {"t": 0, "data": None}
TIPOS = {0: "texto", 2: "voz", 4: "categoria", 5: "anuncios", 13: "palco", 15: "forum", 16: "midia"}


def buscar_servidor():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    g = discord_get(f"/guilds/{guild_id}?with_counts=true", token)
    canais = discord_get(f"/guilds/{guild_id}/channels", token)
    cats = {c["id"]: (c.get("position", 0), c.get("name", "")) for c in canais if c.get("type") == 4}
    por_id = {c["id"]: c for c in canais}

    def oculto(c):
        """True se @everyone não pode ver (canal privado / da staff)."""
        for alvo in (c, por_id.get(c.get("parent_id")) or {}):
            for ow in alvo.get("permission_overwrites") or []:
                if ow.get("id") == guild_id:
                    if int(ow.get("deny", 0)) & (1 << 10):
                        return True
                    if int(ow.get("allow", 0)) & (1 << 10):
                        return False
        return False

    lista = []
    for c in canais:
        if c.get("type") == 4 or oculto(c):
            continue
        pos_cat, nome_cat = cats.get(c.get("parent_id"), (-1, ""))
        lista.append({
            "id": c["id"], "nome": c.get("name", ""), "tipo": TIPOS.get(c.get("type"), str(c.get("type"))),
            "categoria": nome_cat, "_o": (pos_cat, c.get("type") in (2, 13), c.get("position", 0)),
        })
    lista.sort(key=lambda c: c.pop("_o"))
    return {
        "nome": g.get("name", ""),
        "membros": g.get("approximate_member_count", 0),
        "online": g.get("approximate_presence_count", 0),
        "boosts": g.get("premium_subscription_count", 0),
        "guild_id": guild_id,
        "canais": lista,
    }


@app.route("/api/servidor")
def servidor():
    """Contagem de membros e lista de canais (nomes e IDs, para os links do site)."""
    agora = time.time()
    if (agora - _srv_cache["t"] > 300 or _srv_cache["data"] is None) and pode_tentar("servidor"):
        try:
            dados = buscar_servidor()
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            dados = None
            app.logger.error("servidor: %s", str(e)[:120])
        if dados:
            _srv_cache.update(t=agora, data=dados)
        else:
            falhou("servidor")
    if not _srv_cache["data"]:
        return jsonify({"configured": False}), 503
    resp = jsonify({"configured": True, **_srv_cache["data"],
                    "roblox": os.environ.get("ROBLOX_URL", "").strip()})
    resp.headers["Cache-Control"] = "public, s-maxage=300, stale-while-revalidate=600"
    return resp


# ---------------------------------------------------------------------------
# Vox Seas ao vivo: APIs públicas do Roblox (sem token)
# ---------------------------------------------------------------------------
_jogo_cache = {"t": 0, "data": None, "universo": None}


def roblox_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "KyzenSA-Site (1.0)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def place_id():
    m = re.search(r"/games/(\d+)", os.environ.get("ROBLOX_URL", ""))
    return m.group(1) if m else "104067066727140"


def buscar_jogo():
    uni = _jogo_cache["universo"] or os.environ.get("ROBLOX_UNIVERSE_ID", "").strip()
    if not uni:
        uni = str(int(roblox_get(f"https://apis.roblox.com/universes/v1/places/{place_id()}/universe")["universeId"]))
        _jogo_cache["universo"] = uni
    g = roblox_get(f"https://games.roblox.com/v1/games?universeIds={uni}")["data"][0]
    dados = {"nome": str(g.get("name", ""))[:60], "jogando": int(g.get("playing") or 0),
             "visitas": int(g.get("visits") or 0), "favoritos": int(g.get("favoritedCount") or 0),
             "atualizado": str(g.get("updated", ""))[:30], "aprovacao": None, "icone": ""}
    try:
        v = roblox_get(f"https://games.roblox.com/v1/games/votes?universeIds={uni}")["data"][0]
        up, down = int(v.get("upVotes") or 0), int(v.get("downVotes") or 0)
        if up + down:
            dados["aprovacao"] = round(100 * up / (up + down))
    except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError):
        pass
    try:
        ic = roblox_get(f"https://thumbnails.roblox.com/v1/games/icons?universeIds={uni}&size=150x150&format=Png")
        u = url_segura(ic["data"][0].get("imageUrl"))
        dados["icone"] = u if u.startswith("https://") and u.split("/")[2].endswith(".rbxcdn.com") else ""
    except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError):
        pass
    return dados


@app.route("/api/jogo")
def jogo():
    """Jogadores online, visitas, favoritos e aprovação de Vox Seas (atualiza a cada 60 s)."""
    agora = time.time()
    if (agora - _jogo_cache["t"] > 60 or _jogo_cache["data"] is None) and pode_tentar("jogo"):
        try:
            _jogo_cache.update(t=agora, data=buscar_jogo())
        except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as e:
            falhou("jogo")
            app.logger.error("jogo: %s", str(e)[:120])
    if not _jogo_cache["data"]:
        return jsonify({"ok": False}), 503
    resp = jsonify({"ok": True, **_jogo_cache["data"]})
    resp.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=120"
    return resp


def canais_publicos():
    """Canais que @everyone vê (mesma lista do /api/servidor), por ID."""
    if not _srv_cache["data"] and pode_tentar("servidor"):
        try:
            dados = buscar_servidor()
            if dados:
                _srv_cache.update(t=time.time(), data=dados)
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            falhou("servidor")
    return {c["id"]: c for c in (_srv_cache["data"] or {}).get("canais", [])}


# ---------------------------------------------------------------------------
# Eventos agendados do Discord (aba "Eventos" do servidor)
# ---------------------------------------------------------------------------
_eventos_cache = {"t": 0, "data": None}


def buscar_eventos():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    evs = discord_get(f"/guilds/{guild_id}/scheduled-events?with_user_count=true", token)
    pub = canais_publicos()
    lista = []
    for e in evs:
        if e.get("status") not in (1, 2):  # 1 = agendado, 2 = acontecendo
            continue
        canal = pub.get(str(e.get("channel_id") or ""))
        if e.get("channel_id") and not canal:
            continue  # evento num canal privado (ex.: da staff): não aparece no site
        local = canal["nome"] if canal else cortar(limpar((e.get("entity_metadata") or {}).get("location") or ""), 60)
        img = f"https://cdn.discordapp.com/guild-events/{e['id']}/{e['image']}.png?size=600" if re.fullmatch(r"[a-f0-9_]{1,64}", str(e.get("image") or "")) else ""
        lista.append({
            "id": str(e["id"]), "nome": cortar(limpar(e.get("name", "")), 90),
            "desc": cortar(limpar(e.get("description") or ""), 220),
            "inicio": e.get("scheduled_start_time"), "fim": e.get("scheduled_end_time"),
            "ao_vivo": e.get("status") == 2, "local": local,
            "tipo": {1: "palco", 2: "voz", 3: "externo"}.get(e.get("entity_type"), ""),
            "interessados": int(e.get("user_count") or 0), "img": img,
            "link": f"https://discord.com/events/{guild_id}/{e['id']}",
        })
    lista.sort(key=lambda x: (not x["ao_vivo"], x["inicio"] or ""))
    return lista[:6]


@app.route("/api/eventos")
def eventos():
    agora = time.time()
    if (agora - _eventos_cache["t"] > 120 or _eventos_cache["data"] is None) and pode_tentar("eventos"):
        try:
            dados = buscar_eventos()
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            dados = None
            app.logger.error("eventos: %s", str(e)[:120])
        if dados is not None:
            _eventos_cache.update(t=agora, data=dados)
            try:
                avisar_eventos(dados)
            except Exception as e:  # noqa: BLE001
                app.logger.error("push eventos: %s", str(e)[:120])
        else:
            falhou("eventos")
    resp = jsonify({"ok": _eventos_cache["data"] is not None, "eventos": _eventos_cache["data"] or []})
    resp.headers["Cache-Control"] = "public, s-maxage=120, stale-while-revalidate=300"
    return resp


# ---------------------------------------------------------------------------
# Calls ao vivo: quantas pessoas em cada canal de voz público (só números, sem nomes).
# O Discord só entrega isso pelo gateway, então o site conecta por ~1 s, lê e desconecta.
# O Discord limita as conexões por dia (normalmente 1000); se sobrarem poucas, o site pula.
# ---------------------------------------------------------------------------
_voz_cache = {"t": 0, "data": None}
VOZ_SEGUNDOS = 180
VOZ_RESERVA = 400  # nunca gasta as últimas conexões do dia


def buscar_voz():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    limite = discord_get("/gateway/bot", token).get("session_start_limit") or {}
    if int(limite.get("remaining", 0)) < VOZ_RESERVA:
        app.logger.warning("voz: poucas conexões restantes hoje (%s), pulando", limite.get("remaining"))
        return None
    import websocket  # websocket-client
    ws = websocket.create_connection("wss://gateway.discord.gg/?v=10&encoding=json", timeout=6)
    estados = None
    try:
        fim = time.time() + 7
        json.loads(ws.recv())  # HELLO
        ws.send(json.dumps({"op": 2, "d": {
            "token": token, "intents": (1 << 0) | (1 << 7),  # GUILDS + GUILD_VOICE_STATES
            "properties": {"os": "linux", "browser": "kyzensa-site", "device": "kyzensa-site"},
            "presence": {"status": "invisible", "since": None, "activities": [], "afk": False},
            "large_threshold": 50}}))
        while time.time() < fim:
            ev = json.loads(ws.recv())
            if ev.get("op") == 9:  # sessão inválida
                break
            if ev.get("t") == "GUILD_CREATE" and str((ev.get("d") or {}).get("id")) == guild_id:
                estados = ev["d"].get("voice_states") or []
                break
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    if estados is None:
        return None
    pub = canais_publicos()
    contagem = {}
    for s in estados:
        cid = str(s.get("channel_id") or "")
        if cid in pub:
            contagem[cid] = contagem.get(cid, 0) + 1
    calls = [{"id": cid, "nome": pub[cid]["nome"], "pessoas": n} for cid, n in contagem.items()]
    calls.sort(key=lambda c: -c["pessoas"])
    return {"total": sum(contagem.values()), "calls": calls, "em": int(time.time())}


@app.route("/api/voz")
def voz():
    agora = time.time()
    if (agora - _voz_cache["t"] > VOZ_SEGUNDOS or _voz_cache["data"] is None) and pode_tentar("voz", 120):
        _voz_cache["t"] = agora  # marca antes, para duas visitas juntas não conectarem duas vezes
        try:
            dados = buscar_voz()
        except Exception as e:  # noqa: BLE001  (gateway pode falhar de vários jeitos)
            dados = None
            app.logger.error("voz: %s", str(e)[:120])
        if dados is not None:
            _voz_cache["data"] = dados
        else:
            falhou("voz")
    if not _voz_cache["data"]:
        return jsonify({"ok": False}), 503
    resp = jsonify({"ok": True, **_voz_cache["data"]})
    resp.headers["Cache-Control"] = f"public, s-maxage={VOZ_SEGUNDOS}, stale-while-revalidate=300"
    return resp


# ---------------------------------------------------------------------------
# Placar das wars (# wars-results). Cada linha no formato:
#   CrewA 3 x 1 CrewB      (também aceita "3-1", "3×1")
#   CrewA venceu CrewB     (conta 1 x 0)
# ---------------------------------------------------------------------------
_wars_cache = {"t": 0, "data": None}
_PLACAR = re.compile(r"^\s*(.+?)\s+(\d{1,3})\s*[xX×-]\s*(\d{1,3})\s+(.+?)\s*$")
_VENCEU = re.compile(r"^\s*(.+?)\s+(?:venceu|ganhou de|derrotou)\s+(?:a\s+|o\s+)?(.+?)\s*$", re.I)


def buscar_wars():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    canal = os.environ.get("WARS_CHANNEL", "").strip() or achar_canal(token, guild_id, "wars-results")
    if not canal:
        return None
    msgs = discord_get(f"/channels/{canal}/messages?limit=100", token)
    tabela, partidas, ultima = {}, [], None

    def crew(n):
        return tabela.setdefault(n.lower(), {"crew": n, "v": 0, "d": 0, "e": 0, "pts": 0, "h": [], "jogos": []})

    for m in reversed(msgs):
        textos = [m.get("content") or ""] + [(e.get("description") or "") for e in m.get("embeds") or []]
        for linha in "\n".join(textos).splitlines():
            linha = limpar(linha)
            a = b = None
            p = _PLACAR.match(linha)
            if p:
                a, ga, gb, b = p.group(1), int(p.group(2)), int(p.group(3)), p.group(4)
            else:
                v = _VENCEU.match(linha)
                if v:
                    a, ga, gb, b = v.group(1), 1, 0, v.group(2)
            if not a:
                continue
            a, b = limpar(a).strip(" -–—:|"), limpar(b).strip(" -–—:|")
            if not a or not b or len(a) > 40 or len(b) > 40:
                continue
            ca, cb = crew(a), crew(b)
            if ga > gb:
                ca["v"] += 1; cb["d"] += 1; ca["pts"] += 3
            elif gb > ga:
                cb["v"] += 1; ca["d"] += 1; cb["pts"] += 3
            else:
                ca["e"] += 1; cb["e"] += 1; ca["pts"] += 1; cb["pts"] += 1
            ca["h"].append("V" if ga > gb else "D" if gb > ga else "E")
            cb["h"].append("V" if gb > ga else "D" if ga > gb else "E")
            d = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            ultima = d
            data_txt = f"{d.day} {MESES[d.month - 1]} {d.year}"
            link_m = f"https://discord.com/channels/{guild_id}/{canal}/{m['id']}"
            ca["jogos"].append({"adv": cb["crew"], "ga": ga, "gb": gb, "data": data_txt, "link": link_m})
            cb["jogos"].append({"adv": ca["crew"], "ga": gb, "gb": ga, "data": data_txt, "link": link_m})
            partidas.append({"a": ca["crew"], "b": cb["crew"], "ga": ga, "gb": gb, "ts": int(d.timestamp()),
                             "data": f"{d.day} {MESES[d.month - 1]} {d.year}",
                             "link": f"https://discord.com/channels/{guild_id}/{canal}/{m['id']}"})
    for c in tabela.values():
        c["h"] = c["h"][-5:][::-1]  # últimas 5, da mais recente para a mais antiga
        c["jogos"] = c["jogos"][-40:][::-1]  # histórico para a página da crew
    ranking = sorted(tabela.values(), key=lambda c: (-c["pts"], -c["v"], c["d"], c["crew"].lower()))
    return {"ranking": ranking[:20], "partidas": partidas[::-1][:10], "todas": partidas[::-1],
            "ultima": int(ultima.timestamp()) if ultima else None}


@app.route("/api/wars")
def wars():
    """Placar das wars. O #wars-results é da categoria Crew (privada): só para quem tem acesso."""
    bloqueio = sem_acesso_crew()
    if bloqueio:
        return bloqueio
    agora = time.time()
    if (agora - _wars_cache["t"] > 15 or _wars_cache["data"] is None) and pode_tentar("wars"):
        try:
            dados = buscar_wars()
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            dados = None
        if dados is not None:
            _wars_cache.update(t=agora, data=dados)
        else:
            falhou("wars")
    resp = jsonify({"configured": _wars_cache["data"] is not None,
                    **(_wars_cache["data"] or {"ranking": [], "partidas": [], "ultima": None})})
    return privado(resp)


# ---------------------------------------------------------------------------
# Staff: membros com os cargos da cadeia de comando
# ---------------------------------------------------------------------------
_staff_cache = {"t": 0, "data": None}
HIERARQUIA = ["Owner", "Co-Owner", "Dev", "Head Admin", "Adm", "Mod", "Senior Staff", "Staff",
              "Junior Staff", "Helper"]


def _n(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def buscar_staff():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not guild_id:
        return None
    roles = discord_get(f"/guilds/{guild_id}/roles", token)
    alvos = [_n(h) for h in HIERARQUIA]
    cargo_de = {}  # role_id -> índice na hierarquia
    for i, alvo in enumerate(alvos):
        exatos = [r for r in roles if _n(r.get("name")) == alvo]
        if not exatos:  # ex.: "👑 Owner" ou "Owner 👑"; não confunde "Senior Staff" com "Staff"
            exatos = [r for r in roles if alvo in _n(r.get("name")) and _n(r.get("name")) not in alvos
                      and not any(o != alvo and o in _n(r.get("name")) and len(o) > len(alvo) for o in alvos)]
        for r in exatos:
            cargo_de.setdefault(r["id"], i)
    info_cargo = {}
    for r in roles:
        if r["id"] in cargo_de:
            i = cargo_de[r["id"]]
            if i not in info_cargo:
                cor = cor_hex((r.get("colors") or {}).get("primary_color")) or cor_hex(r.get("color"))
                info_cargo[i] = {"cargo": limpar(r.get("name")) or HIERARQUIA[i], "cor": cor, "membros": []}
    depois = "0"
    for _ in range(5):  # até 5.000 membros
        lote = discord_get(f"/guilds/{guild_id}/members?limit=1000&after={depois}", token)
        for mb in lote:
            u = mb.get("user") or {}
            if u.get("bot"):
                continue
            niveis = [cargo_de[r] for r in mb.get("roles", []) if r in cargo_de]
            if not niveis:
                continue
            i = min(niveis)
            if mb.get("avatar"):
                av = f"https://cdn.discordapp.com/guilds/{guild_id}/users/{u['id']}/avatars/{mb['avatar']}.webp?size=96"
            elif u.get("avatar"):
                av = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.webp?size=96"
            else:
                av = f"https://cdn.discordapp.com/embed/avatars/{(int(u.get('id', 0)) >> 22) % 6}.png"
            info_cargo[i]["membros"].append({"nome": mb.get("nick") or u.get("global_name") or u.get("username", ""),
                                             "avatar": av})
        if len(lote) < 1000:
            break
        depois = lote[-1]["user"]["id"]
    grupos = [info_cargo[i] for i in sorted(info_cargo) if info_cargo[i]["membros"]]
    for g in grupos:
        g["membros"].sort(key=lambda m: m["nome"].lower())
    return grupos


@app.route("/api/staff")
def staff():
    agora = time.time()
    if (agora - _staff_cache["t"] > 600 or _staff_cache["data"] is None) and pode_tentar("staff", 60):
        try:
            dados = buscar_staff()
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            dados = None
        if dados is not None:
            _staff_cache.update(t=agora, data=dados)
        else:
            falhou("staff")
    resp = jsonify({"configured": _staff_cache["data"] is not None, "grupos": _staff_cache["data"] or []})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1200"
    return resp


# ---------------------------------------------------------------------------
# Avaliação do site -> o bot manda no privado (DM) do dono
#   OWNER_DISCORD_ID -> ID da conta do Discord que recebe as avaliações
# ---------------------------------------------------------------------------
_envios = {}           # ip -> [horários]  (por instância do servidor)
_envios_global = []    # horários de todos os envios (por instância)


def _ip():
    # na Vercel, x-real-ip / x-vercel-forwarded-for são preenchidos pela própria Vercel (não pelo visitante)
    return (request.headers.get("X-Real-Ip", "").strip()
            or request.headers.get("X-Vercel-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def sem_markdown(s):
    """Texto do visitante vai como texto puro: sem links disfarçados, negrito, menções etc."""
    s = re.sub(r"([\\*_~`|>\[\]()#-])", r"\\\1", s)
    s = re.sub(r"@(everyone|here)", "@\u200b\\1", s)
    s = re.sub(r"<(@[!&]?|#|a?:)", "<\u200b\\1", s)
    return re.sub(r"(https?)://", "\\1\u200b://", s)  # link não fica clicável no Discord


@app.route("/api/avaliacao", methods=["POST"])
def avaliacao():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    dono = os.environ.get("OWNER_DISCORD_ID", "").strip()
    if not token or not dono.isdigit():
        return jsonify({"ok": False, "erro": "As avaliações ainda não estão ativas."}), 503
    origem = request.headers.get("Origin", "")
    if origem and origem not in (f"https://{request.host}", f"http://{request.host}"):
        return jsonify({"ok": False, "erro": "Origem não permitida."}), 403  # envio vindo de outro site
    d = request.get_json(silent=True) or {}
    if not isinstance(d, dict):
        d = {}
    if d.get("site"):  # campo invisível: só robô preenche
        return jsonify({"ok": True})
    try:
        nota = int(d.get("nota", 0))
        tempo = float(d.get("t", 0))
    except (TypeError, ValueError):
        nota, tempo = 0, 0
    texto = str(d.get("texto") or "").strip()[:1000]
    nome = re.sub(r"\s+", " ", str(d.get("nome") or "")).strip()[:60]
    if not 1 <= nota <= 5:
        return jsonify({"ok": False, "erro": "Escolha de 1 a 5 estrelas."}), 400
    if tempo < 3:
        return jsonify({"ok": False, "erro": "Calma! Tente de novo em alguns segundos."}), 429
    agora = time.time()
    ip = _ip()
    for k in [k for k, v in _envios.items() if not v or agora - v[-1] > 600]:
        del _envios[k]  # não deixa a lista de IPs crescer sem fim
    meus = [t for t in _envios.get(ip, []) if agora - t < 600]
    _envios_global[:] = [t for t in _envios_global if agora - t < 3600]
    if len(meus) >= 3 or len(_envios_global) >= 40:
        return jsonify({"ok": False, "erro": "Você já enviou avaliações recentemente. Tente mais tarde."}), 429

    estrelas = "★" * nota + "☆" * (5 - nota)
    embed = {
        "title": "⭐ Nova avaliação do site",
        "color": [0xE0574B, 0xE67E22, 0xF1C40F, 0x9ACD32, 0x3BA55D][nota - 1],
        "fields": [
            {"name": "Nota", "value": f"{estrelas}  ({nota}/5)", "inline": True},
            {"name": "Quem enviou", "value": sem_markdown(nome)[:200] or "Anônimo", "inline": True},
            {"name": "Sugestão", "value": sem_markdown(texto)[:1024] or "—"},
        ],
        "footer": {"text": "kyzensaofc.vercel.app"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        dm = discord_post("/users/@me/channels", token, {"recipient_id": dono})
        discord_post(f"/channels/{dm['id']}/messages", token,
                     {"embeds": [embed], "allowed_mentions": {"parse": []}})
    except urllib.error.HTTPError as e:
        app.logger.error("avaliacao: Discord respondeu %s", e.code)
        return jsonify({"ok": False, "erro": "Não foi possível enviar agora. Tente mais tarde."}), 502
    except (urllib.error.URLError, KeyError, ValueError):
        return jsonify({"ok": False, "erro": "Não foi possível enviar agora. Tente mais tarde."}), 502
    _envios[ip] = meus + [agora]
    _envios_global.append(agora)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Divulgação automática do site no #chat-geral (cron da Vercel às 00h, 12h e 19h
# de Brasília). Para não ficar chato: nunca em cima de outra divulgação (precisa
# ter conversa no meio), sem pings e com textos que se revezam.
# ---------------------------------------------------------------------------
CANAL_GERAL = "1383947381032095846"
SITE = "https://kyzensaofc.vercel.app"
DIVULGAR_INTERVALO_H = 4       # tempo mínimo entre duas divulgações (00h, 12h e 19h cabem)
DIVULGAR_MIN_MSGS = 5          # mensagens de membros desde a última divulgação
DIVULGAR_JANELA_H = 12         # ...e pelo menos uma nas últimas horas
DIVULGAR_MIN_RECENTES = 1
HORARIOS_UTC = (3, 15, 22)     # 00h, 12h e 19h de Brasília
DIVULGACOES = [
    {"titulo": "🗺️ O Trello do Vox Seas, do jeito fácil",
     "texto": "Cansou de abrir card por card? No site da Kyzen o **Trello oficial do jogo** fica organizado em abas e "
              "**com busca**: frutas, espadas, ilhas, chefes e drops num lugar só.\n\n"
              "📖 Quer ir direto ao ponto? O **Guia** já mastiga tudo pra você: **onde dropa** cada item e **tier list** "
              "de frutas e espadas.\n"
              "🌊 E na **Comunidade** tem o resumo do jogo com os **números ao vivo** do Roblox e o **mapa das ilhas**.",
     "ancora": "#trello"},
    {"titulo": "📖 Guias do Vox Seas feitos pra te poupar tempo",
     "texto": "No **Guia** do site da Kyzen você encontra:\n"
              "🎯 **Onde dropa?** Busque o item e veja qual **chefe** solta, em qual **ilha** e a **chance de drop**\n"
              "🏆 **Tier list** de frutas e espadas, com **comparador** lado a lado\n"
              "👹 O que **cada chefe** solta, pra farmar sem perder tempo\n\n"
              "Tudo puxado do **Trello oficial** e atualizado sozinho.\n"
              "-# Aqui no Discord também dá: use **/drop** e **/fruta** no #comandos.",
     "ancora": "#guia"},
    {"titulo": "⚓ Não sabe pra onde ir agora?",
     "texto": "Na aba **Comunidade** do site tem o **mapa das ilhas** do Vox Seas em ordem de nível: digite o seu nível "
              "e ele mostra **para onde navegar** e o que tem em cada ilha.\n\n"
              "Lá também ficam o **resumo do jogo** com jogadores **ao vivo**, os nossos **canais e calls** e a "
              "**galeria** com os melhores prints do #mídias.\n"
              "📖 Depois é só abrir o **Guia** pra ver os drops da ilha, ou o **Trello** completo.",
     "ancora": "#comunidade"},
    {"titulo": "🎯 Qual fruta vale a pena? Qual espada pegar?",
     "texto": "Antes de gastar tempo farmando, dá uma olhada no **Guia** do site da Kyzen: **tier list** de frutas e "
              "espadas, **comparador** e o **\"Onde dropa?\"** com chefe, ilha e chance de cada item.\n\n"
              "🗺️ Quer o detalhe completo? O **Trello oficial** está lá dentro, com busca.\n"
              "🌊 E a aba **Comunidade** junta o mapa das ilhas, a galeria e tudo do nosso servidor.",
     "ancora": "#guia"},
    {"titulo": "🌊 Tudo do Vox Seas e da Kyzen num site só",
     "texto": "📖 **Guia**: onde dropa cada item, o que cada chefe solta e tier list de frutas e espadas\n"
              "🗺️ **Trello oficial** do jogo, organizado e com busca\n"
              "🌊 **Comunidade**: mapa das ilhas por nível, jogadores ao vivo, canais, calls e galeria\n"
              "🏆 **Hall da Fama**, eventos do servidor e uma 🗝️ **caça ao tesouro** escondida\n\n"
              "Dá até pra instalar como app no celular. Passa lá! 👇",
     "ancora": "#inicio"},
]
BOTOES = [("📖 Guia", "#guia"), ("🌊 Comunidade", "#comunidade"), ("🗺️ Trello", "#trello")]


def cron_ok():
    """Só a Vercel (Authorization: Bearer CRON_SECRET) ou a staff com a DEBUG_KEY."""
    chave = os.environ.get("CRON_SECRET", "")
    vercel = len(chave) >= 16 and hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {chave}")
    return vercel or debug_ok()


def snowflake_ts(sid):
    return ((int(sid) >> 22) + 1420070400000) / 1000


BLOX = "https://theblox.com.br/?pid=kyzen"
DIVULGAR_SO_BLOX = True
BLOX_LOGO = "https://theblox.com.br/cdn/s/gYzcMXAdJnXPDR89wDWlyo/attachments/17423474473673678imagem.png"
BLOX_BANNER = "https://theblox.com.br/cdn/s/tK_mrGrcvDuVQ2W4ROcra3/attachments/17946020054027417imagem.webp"
CANAIS_DIVULGACAO = ["1383947381032095846", "1504915310892024018"]  # #chat-geral e #chat-crew
BLOX_LEMBRETES = [
    {"titulo": "🤫 A galera que tá subindo rápido no Vox Seas tem um segredo…",
     "texto": "Enquanto muita gente paga caro em Robux, quem é da Kyzen entra na **The Blox**, usa o cupom `KYZEN` "
              "e leva **fruta, espada e gamepass gastando bem menos**.\n\n"
              "Pronto. Agora você também sabe. 😏"},
    {"titulo": "👀 Você ainda tá pagando preço cheio no Roblox?",
     "texto": "Sério mesmo? 😅\n\nQuem é da Kyzen tem **desconto exclusivo** na **The Blox**, a nossa patrocinadora. "
              "**Mesma fruta, mesma espada, preço menor.**\n\nNo pagamento é só usar o cupom `KYZEN`."},
    {"titulo": "🍎 E se a fruta dos seus sonhos estivesse a um cupom de distância?",
     "texto": "Sabe aquela fruta que você vive olhando e pensando *\"um dia eu pego\"*?\n\n"
              "Com o cupom `KYZEN` na **The Blox**, esse dia pode ser **hoje**. "
              "O que parecia caro demais fica ao seu alcance. 🔥"},
    {"titulo": "⚔️ Na próxima war, quem vai estar mais forte: você ou o inimigo?",
     "texto": "Os inimigos **não têm** o cupom da Kyzen. **Você tem.** 😈\n\n"
              "Equipamento melhor, gastando menos, na **The Blox**. "
              "Chega na próxima war preparado e deixa o resto da tripulação orgulhosa. 🏴‍☠️"},
    {"titulo": "💸 Robux tá caro? A Kyzen deu um jeito nisso.",
     "texto": "A **The Blox**, nossa patrocinadora, vende itens do Roblox por **menos do que o próprio Roblox**.\n"
              "E com o cupom `KYZEN` fica **ainda melhor**.\n\n"
              "Frutas permanentes, espadas, gamepasses… tudo num lugar só. 🛒"},
    {"titulo": "🏴‍☠️ Só quem é da tripulação sabe disso…",
     "texto": "Existe um desconto que você **não vai achar em lugar nenhum**: o dono da **The Blox** joga com a gente, "
              "é da nossa crew e liberou o cupom `KYZEN` **só para a Kyzen**.\n\n"
              "Vai deixar passar? 👀"},
]
ANUNCIO_THEBLOX = """@everyone

# 🤝 A KYZEN AGORA É PATROCINADA PELA THE BLOX!

Tripulação, chegou a parceria que faltava! 🏴‍☠️

A **The Blox** é uma das **maiores lojas de itens de Roblox do Brasil**, e agora é a **patrocinadora oficial da Kyzen**. E não é uma loja qualquer: o dono **joga Vox Seas com a gente e faz parte da nossa crew**. Ou seja, é gente de dentro, que conhece o jogo e quer ver a tripulação forte. ⚓

🎟️ **Cupom exclusivo da Kyzen:** `KYZEN`

💸 Preços **bem melhores do que comprando direto no Roblox**
🔒 Loja **confiável**, com milhares de clientes
🍎 Frutas permanentes, espadas, gamepasses e muito mais
🎁 Um desconto que **só quem é da Kyzen tem**

Sabe aquela fruta ou espada que você queria, mas achava cara demais? **Essa é a hora.** Com o cupom da Kyzen, o que parecia longe fica ao alcance. É a chance de evoluir no Vox Seas gastando menos. 🚀

🛒 **Compre pelo link da Kyzen:**
https://theblox.com.br/?pid=kyzen

📝 **Como usar:**
**1.** Entre pelo link acima
**2.** Escolha o que você quer
**3.** No pagamento, use o cupom `KYZEN`

Comprando pelo nosso link, você ainda ajuda a Kyzen a crescer. 💙

Bora ficar mais forte e dominar os mares juntos! 🌊⚓"""


DIVULGAR_PARCEIRA = "novabux"  # quem aparece nos 3 horários: "novabux" ou "theblox"
NOVABUX = "https://discord.gg/vZu9VZMzhz"  # só o servidor deles é divulgado (o site não)
NOVABUX_ICONE = SITE + "/img/novabux-icone.png"
NOVABUX_BANNER = SITE + "/img/novabux-banner3.png"
NOVABUX_RODAPE = "A NovaBux é uma loja parceira e independente da Kyzen. Dúvidas e compras são tratadas direto com eles."
NOVABUX_LEMBRETES = [
    {"titulo": "🤯 Ser da Kyzen nunca valeu tanto a pena",
     "texto": "A **NovaBux** liberou uma promoção **INSANA só para quem é da Kyzen**: **até 44% de desconto** em "
              "gamepasses, espadas, cristais e muito mais do **Vox Seas**.\n\n"
              "Quem não é da Kyzen paga mais caro. **Você não.** 😏 É só dizer que veio da **Kyzen** no ticket."},
    {"titulo": "👀 Você é da Kyzen e ainda paga preço cheio?",
     "texto": "Então você está **perdendo dinheiro**. 😅\n\n"
              "Por ser da Kyzen, você tem acesso a um desconto **absurdo** na **NovaBux**: **até 44% OFF**. "
              "Entra no servidor deles e fala que veio da **Kyzen**."},
    {"titulo": "⚔️ Os inimigos pagam caro. Quem é da Kyzen, não.",
     "texto": "Essa é a vantagem de estar na tripulação certa. 😈\n\n"
              "Na **NovaBux**, quem é da Kyzen leva gamepass, Double XP e espadas com **desconto insano de até 44%**. "
              "Chega na próxima war mais forte gastando quase a metade."},
    {"titulo": "🏝️ Quase metade do preço? Só sendo da Kyzen.",
     "texto": "Private Server, 2x Mastery, Fruit Notifier, barcos… com **até 44% de desconto** na **NovaBux**.\n\n"
              "💜 Essa promoção é **exclusiva da Kyzen**. Em nenhum outro lugar você acha esse preço. "
              "Vai deixar passar?"},
    {"titulo": "💎 Cristais e money com preço de membro da Kyzen",
     "texto": "Quer subir rápido no Vox Seas? Por ser da Kyzen, você paga **muito menos** em **cristais, money e "
              "Double XP** na **NovaBux**.\n\nUma promoção **insana**, só nossa. Diz que veio da **Kyzen** e garante. 🚀"},
    {"titulo": "🤫 O privilégio de quem é da Kyzen…",
     "texto": "Existe um desconto que **só a Kyzen tem**: **até 44% OFF** em itens do Vox Seas na **NovaBux**. 😏\n\n"
              "Entra lá, abre seu ticket e fala que veio da **Kyzen**. O resto é economia."},
]
AVISO_NOVABUX = f"""<@&1446300736454529114>

# 🤝 NOVA PARCEIRA: NOVABUX

A Kyzen fechou parceria com a **NovaBux**, uma loja de itens do **Vox Seas**!

🔥 **Promoções fantásticas** rolando agora: gamepasses, itens e muito mais por preços que você não vai acreditar.

💜 **Descontos de até 44% são exclusivos para quem apoia a Kyzen!** Para garantir o seu, é só dizer que veio da **Kyzen** ao abrir seu ticket lá.

Passa no servidor deles e confere com seus próprios olhos 👀

🔗 {NOVABUX}

-# {NOVABUX_RODAPE}"""


def divulgacao_novabux(agora, turno=0):
    d = NOVABUX_LEMBRETES[(int(agora // 86400) * len(HORARIOS_UTC) + turno) % len(NOVABUX_LEMBRETES)]
    return {"embeds": [{"author": {"name": "NovaBux · parceira da Kyzen", "icon_url": NOVABUX_ICONE, "url": NOVABUX},
                        "title": d["titulo"], "url": NOVABUX, "color": 0xE24FD3, "description": d["texto"],
                        "thumbnail": {"url": NOVABUX_ICONE}, "image": {"url": NOVABUX_BANNER},
                        "footer": {"text": NOVABUX_RODAPE}}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": "💜 Entrar no servidor da NovaBux", "url": NOVABUX}]}],
            "allowed_mentions": {"parse": []}}


def avisar_novabux():
    for m in discord_req("GET", "/channels/1383990514701041836/messages?limit=30"):
        if "nT2EZbUZD" in (m.get("content") or "") or "vZu9VZMzhz" in (m.get("content") or ""):
            imagem = ((m.get("embeds") or [{}])[0].get("image") or {}).get("url") or ""
            if "nT2EZbUZD" in m["content"] or imagem != NOVABUX_BANNER:  # convite ou banner desatualizado
                discord_req("PATCH", f"/channels/1383990514701041836/messages/{m['id']}",
                            {"content": AVISO_NOVABUX, "allowed_mentions": {"parse": []},
                             "embeds": [{"color": 0xE24FD3, "image": {"url": NOVABUX_BANNER}}]})
                return {"enviou": False, "editou": True, "id": m.get("id")}
            return {"enviou": False, "motivo": "já foi avisado", "id": m.get("id")}
    r = discord_req("POST", "/channels/1383990514701041836/messages", {
        "content": AVISO_NOVABUX, "allowed_mentions": {"roles": ["1446300736454529114"]},
        "embeds": [{"color": 0xE24FD3, "image": {"url": NOVABUX_BANNER}}]})
    return {"enviou": True, "id": r.get("id")}


def divulgacao_blox(agora, turno=0):
    d = BLOX_LEMBRETES[(int(agora // 86400) * len(HORARIOS_UTC) + turno) % len(BLOX_LEMBRETES)]
    return {"embeds": [{"author": {"name": "The Blox · parceira oficial da Kyzen", "icon_url": BLOX_LOGO, "url": BLOX},
                        "title": d["titulo"], "url": BLOX, "color": 0x1E90FF, "description": d["texto"],
                        "thumbnail": {"url": BLOX_LOGO}, "image": {"url": BLOX_BANNER},
                        "footer": {"text": "Kyzen SA • patrocinada pela The Blox • comprando pelo link você ajuda a crew"}}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": "🛒 Comprar com o cupom KYZEN", "url": BLOX}]}],
            "allowed_mentions": {"parse": []}}


def anunciar_theblox(token):
    """Anúncio único do patrocínio no #anúncios (não repete se já foi postado)."""
    for m in discord_get("/channels/1383990514701041836/messages?limit=50", token):
        if "theblox.com.br/?pid=kyzen" in (m.get("content") or ""):
            return {"ok": True, "enviou": False, "motivo": "já foi anunciado", "id": m.get("id")}
    r = discord_post("/channels/1383990514701041836/messages", token,
                     {"content": ANUNCIO_THEBLOX, "allowed_mentions": {"parse": ["everyone"]}})
    return {"ok": True, "enviou": True, "id": r.get("id"), "everyone": r.get("mention_everyone")}


def decidir_divulgacao(msgs, bot_id, agora):
    """Decide se é hora de divulgar, olhando só as últimas mensagens do canal (sem banco)."""
    humanas, recentes = 0, 0
    for m in msgs:  # da mais nova para a mais antiga
        autor = m.get("author") or {}
        if str(autor.get("id")) == bot_id and m.get("embeds") and \
                any(SITE in (e.get("url") or "") or "theblox.com.br" in (e.get("url") or "")
                    or "discord.gg/" in (e.get("url") or "") for e in m["embeds"]):
            if agora - snowflake_ts(m["id"]) < DIVULGAR_INTERVALO_H * 3600:
                return False, "divulgou há pouco tempo"
            break  # a contagem de mensagens vale só desde a última divulgação
        if autor.get("bot"):
            continue
        humanas += 1
        if agora - snowflake_ts(m["id"]) < DIVULGAR_JANELA_H * 3600:
            recentes += 1
    if humanas < DIVULGAR_MIN_MSGS:
        return False, f"pouca conversa desde a última divulgação ({humanas})"
    if recentes < DIVULGAR_MIN_RECENTES:
        return False, f"chat parado agora ({recentes} nas últimas {DIVULGAR_JANELA_H}h)"
    return True, "ok"


def montar_divulgacao(agora):
    hora = int(agora // 3600) % 24
    turno = min(range(len(HORARIOS_UTC)), key=lambda i: abs(HORARIOS_UTC[i] - hora))
    if DIVULGAR_PARCEIRA == "novabux":  # os 3 horários são da parceira (os textos do site ficam guardados)
        return divulgacao_novabux(agora, turno)
    if DIVULGAR_SO_BLOX:
        return divulgacao_blox(agora, turno)
    if HORARIOS_UTC[turno] == 15:
        return divulgacao_blox(agora, turno)
    d = DIVULGACOES[(int(agora // 86400) * len(HORARIOS_UTC) + turno) % len(DIVULGACOES)]
    ancora = d["ancora"]
    botoes = sorted(BOTOES, key=lambda x: x[1] != ancora)  # o tema do texto vem primeiro
    if ancora == "#inicio":
        botoes = [("⚓ Abrir o site", "#inicio")] + BOTOES
    return {
        "embeds": [{
            "title": d["titulo"], "url": SITE + "/" + ancora, "color": 0xC9A227,
            "description": d["texto"],
            "thumbnail": {"url": SITE + "/img/icon-192.png"},
            "footer": {"text": "Kyzen SA • kyzensaofc.vercel.app"},
        }],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": r, "url": SITE + "/" + a} for r, a in botoes]}],
        "allowed_mentions": {"parse": []},
    }


@app.route("/api/divulgar")
def divulgar():
    if not cron_ok():
        return jsonify({"ok": False}), 404
    if request.args.get("previa") != "1":
        try:
            app.logger.info("agenda: %s", processar_agenda())
        except Exception as e:  # noqa: BLE001  (o sorteio não pode travar a divulgação)
            app.logger.error("sorteios: %s", str(e)[:160])
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if request.args.get("anuncio") == "theblox" and debug_ok():  # anúncio único do patrocínio
        try:
            return jsonify(anunciar_theblox(token))
        except urllib.error.URLError as e:
            return jsonify({"ok": False, "motivo": str(e)[:120]}), 502
    agora = time.time()
    try:
        bot_id = str(discord_get("/users/@me", token)["id"])
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return jsonify({"ok": False, "motivo": "discord indisponível"}), 502
    forcar = request.args.get("forcar") == "1" and debug_ok()  # teste manual da staff
    previa = request.args.get("previa") == "1"
    resultados = {}
    so = request.args.get("so", "")
    for canal in [c for c in CANAIS_DIVULGACAO if not (forcar and so) or c == so]:
        try:
            msgs = discord_get(f"/channels/{canal}/messages?limit=100", token)
            vai, motivo = (True, "forçado pela staff") if forcar else decidir_divulgacao(msgs, bot_id, agora)
            if previa or not vai:
                resultados[canal] = {"enviou": False, "enviaria": vai, "motivo": motivo,
                                     "mensagem": montar_divulgacao(agora)["embeds"][0]["title"]}
                continue
            r = discord_post(f"/channels/{canal}/messages", token, montar_divulgacao(agora))
            resultados[canal] = {"enviou": True, "id": r.get("id")}
        except urllib.error.HTTPError as e:
            resultados[canal] = {"enviou": False, "motivo": f"discord recusou ({e.code})"}
        except (urllib.error.URLError, ValueError):
            resultados[canal] = {"enviou": False, "motivo": "discord indisponível"}
        time.sleep(0.5)
    return jsonify({"ok": True, "canais": resultados})


@app.route("/api/requisitos")
def requisitos():  # endereço antigo, mantido por compatibilidade
    return canal_api("requisitos")


# ---------------------------------------------------------------------------
# Trello oficial do Vox Seas (quadro público, sem chave)
# ---------------------------------------------------------------------------
TRELLO_BOARD = "qGXeMuSt"
TRELLO_URL = "https://trello.com/b/qGXeMuSt/vox-seas-official-trello"
_trello_cache = {"t": 0, "data": None, "cards": {}}
_CORES_TRELLO = {"green": "#4bce97", "yellow": "#e2b203", "orange": "#faa53d", "red": "#f87462", "purple": "#9f8fef",
                 "blue": "#579dff", "sky": "#6cc3e0", "lime": "#94c748", "pink": "#e774bb", "black": "#8590a2"}


def _capa(card, maximo=600):
    """URL da prévia da capa (a menor com pelo menos 300 px, até `maximo`)."""
    prev = [p for p in (card.get("cover") or {}).get("scaled") or [] if p.get("url")]
    if not prev:
        return ""
    ok = sorted((p for p in prev if 300 <= int(p.get("width") or 0) <= maximo), key=lambda p: p["width"])
    p = ok[0] if ok else max(prev, key=lambda p: int(p.get("width") or 0))
    u = url_segura(p["url"])
    return u if u.startswith("https://trello.com/") else ""


def buscar_trello():
    base = f"https://api.trello.com/1/boards/{TRELLO_BOARD}"
    listas = roblox_get(f"{base}/lists?filter=open&fields=name,pos")
    cards = roblox_get(f"{base}/cards/visible?fields=name,desc,idList,labels,shortUrl,cover,pos"
                       "&attachments=cover&attachment_fields=previews")  # sem isso a capa vem sem as URLs
    por_lista = {}
    detalhes = {}
    for c in sorted(cards, key=lambda c: c.get("pos") or 0):
        cid = str(c.get("id", ""))
        if not re.fullmatch(r"[a-f0-9]{24}", cid):
            continue
        desc = c.get("desc") or ""
        item = {"id": cid, "nome": cortar(str(c.get("name", "")).strip(), 120), "img": _capa(c),
                "desc": cortar(limpar(re.sub(r"\[([^\]]*)\]\([^)]*\)", lambda m: "" if m.group(1).startswith("http") else m.group(1),
                                             re.sub(r"!\[[^\]]*\]\([^)]*\)|^\s*#{1,6}\s*", "", desc, flags=re.M))), 140),
                "tem": bool(desc.strip()),
                "tags": [{"n": cortar(str(l.get("name") or ""), 30), "c": _CORES_TRELLO.get(str(l.get("color") or "").split("_")[0], "")}
                         for l in c.get("labels") or [] if l.get("name")]}
        por_lista.setdefault(c.get("idList"), []).append(item)
        detalhes[cid] = {"desc": desc[:20000], "url": url_segura(c.get("shortUrl")), "img": _capa(c, 1600)}
    saida = []
    for l in sorted(listas, key=lambda l: l.get("pos") or 0):
        itens = por_lista.get(l.get("id"), [])
        if itens:
            saida.append({"nome": cortar(str(l.get("name", "")).strip(), 60), "cards": itens})
    return saida, detalhes


def trello_md(txt):
    """Markdown do Trello -> HTML seguro (reaproveita o conversor do Discord)."""
    imgs = []

    def img(m):
        u = url_segura(m.group(2))
        if not u.startswith("https://"):
            return ""
        imgs.append(u)
        return f"\x01{len(imgs) - 1}\x01"

    txt = re.sub(r"!\[([^\]]*)\]\(\s*([^\s)]+)[^)]*\)", img, txt or "")
    txt = re.sub(r"\]\((https?://[^\s)]+)\s+\"[^\"]*\"\)", r"](\1)", txt)  # tira o "smartCard-block"
    txt = re.sub(r"(?m)^#{4,6} ", "### ", txt)
    txt = re.sub(r"(?m)^\s*(?:---+|\*\*\*+)\s*$", "", txt)
    txt = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", txt)
    html = discord_md(txt, {"roles": {}, "users": {}, "channels": {}})
    return re.sub("\x01(\\d+)\x01", lambda m: f'<img class="att" src="{esc(imgs[int(m.group(1))])}" alt="" '
                                              f'loading="lazy" referrerpolicy="no-referrer">', html)


def trello_cache():
    agora = time.time()
    if (agora - _trello_cache["t"] > 600 or _trello_cache["data"] is None) and pode_tentar("trello", 60):
        try:
            dados, det = buscar_trello()
            _trello_cache.update(t=agora, data=dados, cards=det)
        except Exception as e:  # noqa: BLE001  (conteúdo de terceiros: nada aqui pode derrubar o site)
            falhou("trello")
            app.logger.error("trello: %s", str(e)[:120])
    return _trello_cache["data"]


@app.route("/api/trello")
def trello():
    dados = trello_cache()
    if dados is None:
        return jsonify({"ok": False, "url": TRELLO_URL}), 503
    resp = jsonify({"ok": True, "url": TRELLO_URL, "listas": dados})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1800"
    return resp


@app.route("/api/trello/<cid>")
def trello_card(cid):
    if not re.fullmatch(r"[a-f0-9]{24}", cid):
        return jsonify({"ok": False}), 404
    trello_cache()
    c = _trello_cache["cards"].get(cid)
    if not c:
        return jsonify({"ok": False}), 404
    resp = jsonify({"ok": True, "html": trello_md(c["desc"]), "url": c["url"], "img": c["img"]})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1800"
    return resp


def lista_trello(nome):
    alvo = slug_py(nome)
    for l in trello_cache() or []:
        if slug_py(l["nome"]).startswith(alvo):
            return l["cards"]
    return []


_ILHA = re.compile(r"^\s*(.+?)\s*\|\s*Lvl\s*(\d+)\s*-\s*(\d+)\s*$", re.I)


@app.route("/api/mapa")
def mapa():
    """Ilhas do Vox Seas (lista Islands do Trello), em ordem de nível, para o mapa interativo."""
    cards = lista_trello("Islands")
    if not cards:
        return jsonify({"ok": False}), 503
    ilhas, extras, mundo, porto = [], [], "", None
    for c in cards:
        m = _ILHA.match(c["nome"])
        if m:
            ilhas.append({"id": c["id"], "nome": m.group(1), "min": int(m.group(2)), "max": int(m.group(3)), "img": c["img"]})
        elif c["nome"].lower().startswith("world map"):
            det = _trello_cache["cards"].get(c["id"]) or {}
            achou = re.search(r"!\[[^\]]*\]\((https://trello\.com/[^\s)]+)", det.get("desc", ""))
            mundo = det.get("img") or (url_segura(achou.group(1)) if achou else "")
        elif "|" in c["nome"] and "main island" in c["nome"].lower():
            porto = {"id": c["id"], "nome": c["nome"].split("|")[0].strip(), "img": c["img"]}
        elif c["nome"].lower() not in ("main islands", "miscellaneous"):
            extras.append({"id": c["id"], "nome": c["nome"].split("|")[0].strip(), "img": c["img"]})
    ilhas.sort(key=lambda i: (i["min"], i["max"]))
    resp = jsonify({"ok": True, "porto": porto, "ilhas": ilhas, "extras": extras, "mundo": mundo,
                    "chefes": len(lista_trello("Bosses")), "url": TRELLO_URL})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1800"
    return resp


# ---------------------------------------------------------------------------
# Guia do jogo: "Onde dropa?" (chefes e drops) e tier list (frutas e espadas), tudo do Trello
# ---------------------------------------------------------------------------
_guia_cache = {"t": 0, "data": None}
CATS = {"materials": "Materiais", "resources": "Materiais", "keys": "Chaves", "equipment": "Equipamentos",
        "weapons": "Equipamentos", "accessor": "Equipamentos", "fruit": "Frutas", "sword": "Equipamentos"}


def item_limpo(s):
    s = re.sub(r"\*\*|__|`", "", s).strip(" -•*‌​")
    s = re.sub(r"\s*\((?:direct drop|recipe|drop)[^)]*\)\s*$", "", s, flags=re.I)
    return cortar(re.sub(r"\s+", " ", s), 70)


def drops_do_chefe(desc):
    """Lê as seções de drops (Materials and Resources / Keys / Equipment...) da descrição do chefe."""
    cats, atual = {}, None
    for ln in (desc or "").splitlines():
        t = ln.strip()
        if not t or t.startswith("[") or "smartCard" in t:
            continue
        if re.match(r"^#{1,6}\s", t) and not re.search(r"drops", t, re.I):
            atual = None  # "Boss Set", "Location"... acabaram os drops
            continue
        m_item = re.match(r"^[-*•]\s+(.+)$", t)
        if m_item and atual:
            nome = item_limpo(m_item.group(1))
            if nome and "trello.com" not in nome:
                cats.setdefault(atual, []).append(nome)
            continue
        titulo = item_limpo(re.sub(r"^#{1,6}\s*", "", t)).lower()
        achou = next((v for k, v in CATS.items() if k in titulo), None)
        if achou:
            atual = achou
    return {k: list(dict.fromkeys(v)) for k, v in cats.items()}


def buscar_guia():
    trello_cache()
    det = _trello_cache["cards"]
    curto = {}  # shortLink -> nome da ilha
    for c in lista_trello("Islands"):
        u = (det.get(c["id"]) or {}).get("url", "")
        if "/c/" in u:
            curto[u.rsplit("/", 1)[-1]] = (_ILHA.match(c["nome"]).group(1) if _ILHA.match(c["nome"]) else c["nome"].split("|")[0].strip())
    chefes, itens = [], {}
    for lista, raid in (("Bosses", False), ("Raid Bosses", True)):
        for c in lista_trello(lista):
            desc = (det.get(c["id"]) or {}).get("desc", "")
            drops = drops_do_chefe(desc)
            ilha = ""
            loc = re.search(r"location[\s\S]{0,40}?trello\.com/c/([A-Za-z0-9]{6,12})", desc, re.I)
            if loc:
                ilha = curto.get(loc.group(1), "")
            if not ilha and len(desc) < 150:  # descrição curta do tipo "Chefe X (Prison)"
                par = re.search(r"\(([^)]{3,30})\)\s*$", desc.strip())
                ilha = par.group(1) if par else ""
            if not drops and not ilha:
                continue
            chefe = {"id": c["id"], "nome": c["nome"], "img": c["img"], "ilha": ilha, "raid": raid, "drops": drops}
            chefes.append(chefe)
            for cat, lst in drops.items():
                for it in lst:
                    itens.setdefault(it, {"cat": cat, "chefes": []})["chefes"].append(len(chefes) - 1)
    tiers = []
    for lista in ("Vox Fruits", "Swords"):
        grupos, atual = [], None
        for c in lista_trello(lista):
            if not c.get("tem") and not c.get("img") or (not c.get("tem") and len(c["nome"]) < 16 and c["nome"].istitle()):
                atual = {"tier": c["nome"], "itens": []}
                grupos.append(atual)
                continue
            if atual is None:
                atual = {"tier": "Outros", "itens": []}
                grupos.append(atual)
            atual["itens"].append({"id": c["id"], "nome": c["nome"], "img": c["img"], "tags": c["tags"], "desc": c["desc"]})
        tiers.append({"lista": "Frutas" if lista == "Vox Fruits" else "Espadas", "grupos": [g for g in grupos if g["itens"]]})
    itens_lista = sorted(({"nome": k, **v} for k, v in itens.items()), key=lambda i: i["nome"].lower())
    return {"chefes": chefes, "itens": itens_lista, "tiers": tiers}


def guia_cache():
    if time.time() - _guia_cache["t"] > 600 or _guia_cache["data"] is None:
        try:
            _guia_cache.update(t=time.time(), data=buscar_guia())
        except Exception as e:  # noqa: BLE001  (conteúdo de terceiros)
            app.logger.error("guia: %s", str(e)[:120])
    return _guia_cache["data"]


# ---------------------------------------------------------------------------
# Galeria do # mídias (só imagens e vídeos; sem nome de quem postou; pula spoilers)
# ---------------------------------------------------------------------------
_galeria_cache = {"t": 0, "data": None}
CDN_OK = ("https://cdn.discordapp.com/", "https://media.discordapp.net/")


def buscar_galeria():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    canal = os.environ.get("MIDIAS_CHANNEL", "").strip() or achar_canal(token, gid, "midias")
    if not canal or canal not in canais_publicos():  # só canal que todo mundo do servidor vê
        return []
    msgs = discord_get(f"/channels/{canal}/messages?limit=100", token)
    itens = []
    for m in msgs:
        for a in m.get("attachments") or []:
            tipo = (a.get("content_type") or "").split("/")[0]
            u = str(a.get("url") or "")
            if tipo not in ("image", "video") or not u.startswith(CDN_OK) or a.get("filename", "").startswith("SPOILER_") \
                    or (int(a.get("flags") or 0) & 8):
                continue
            itens.append({"tipo": tipo, "url": u, "w": a.get("width") or 0, "h": a.get("height") or 0,
                          "prev": str(a.get("proxy_url") or u), "link": f"https://discord.com/channels/{gid}/{canal}/{m['id']}"})
    return itens[:60]


@app.route("/api/galeria")
def galeria():
    agora = time.time()
    if (agora - _galeria_cache["t"] > 600 or _galeria_cache["data"] is None) and pode_tentar("galeria", 60):
        try:
            _galeria_cache.update(t=agora, data=buscar_galeria())
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            falhou("galeria")
            app.logger.error("galeria: %s", str(e)[:120])
    resp = jsonify({"ok": _galeria_cache["data"] is not None, "itens": _galeria_cache["data"] or []})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1200"
    return resp


@app.route("/api/guia")
def guia():
    g = guia_cache()
    if not g:
        return jsonify({"ok": False}), 503
    resp = jsonify({"ok": True, **g, "url": TRELLO_URL})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1800"
    return resp


# ---------------------------------------------------------------------------
# Login com Discord + Área da Crew
#   O site só pede o escopo "identify" (nome e avatar). Quem pode ver o quê é calculado com as
#   permissões reais do servidor: quem vê os canais da categoria Crew no Discord, vê no site.
#   DISCORD_CLIENT_SECRET -> segredo do app (Developer Portal > OAuth2). Sem ele o login fica desligado.
#   Redirect cadastrado no Developer Portal: https://kyzensaofc.vercel.app/auth/callback
# ---------------------------------------------------------------------------
VER_CANAL, ADMIN = 1 << 10, 1 << 3
_guild_cache = {"t": 0, "roles": None, "canais": None, "dono": ""}
_acesso_cache = {}
_msgs_cache = {}
_app_id = {"id": ""}


def guild_info():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if time.time() - _guild_cache["t"] > 300 or _guild_cache["roles"] is None:
        roles = discord_get(f"/guilds/{gid}/roles", token)
        canais = discord_get(f"/guilds/{gid}/channels", token)
        dono = str(discord_get(f"/guilds/{gid}", token).get("owner_id") or "")
        _guild_cache.update(t=time.time(), roles=roles, canais=canais, dono=dono)
    return _guild_cache


def canais_crew(info):
    cats = {c["id"] for c in info["canais"] if c.get("type") == 4 and "crew" in slug_py(c.get("name", ""))}
    return sorted((c for c in info["canais"] if c.get("parent_id") in cats and c.get("type") in (0, 5)),
                  key=lambda c: c.get("position", 0))


def pode_ver(info, gid, uid, cargos, canal):
    """Mesma conta que o Discord faz para decidir se alguém vê um canal."""
    if uid == info["dono"]:
        return True
    perm = {str(r["id"]): int(r.get("permissions") or 0) for r in info["roles"]}
    base = perm.get(gid, 0)
    for r in cargos:
        base |= perm.get(r, 0)
    if base & ADMIN:
        return True
    ows = {str(o["id"]): o for o in canal.get("permission_overwrites") or []}
    p = base
    o = ows.get(gid)
    if o:
        p = (p & ~int(o.get("deny") or 0)) | int(o.get("allow") or 0)
    a = d = 0
    for r in cargos:
        o = ows.get(r)
        if o:
            a |= int(o.get("allow") or 0)
            d |= int(o.get("deny") or 0)
    p = (p & ~d) | a
    o = ows.get(uid)
    if o:
        p = (p & ~int(o.get("deny") or 0)) | int(o.get("allow") or 0)
    return bool(p & VER_CANAL)


def acesso(uid):
    """Canais da categoria Crew que este usuário vê no Discord (lista; None se o Discord falhou)."""
    agora = time.time()
    c = _acesso_cache.get(uid)
    if c and agora - c[0] < 60:
        return c[1]
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    try:
        info = guild_info()
        try:
            membro = discord_get(f"/guilds/{gid}/members/{uid}", token)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            membro = None  # não está no servidor
        cargos = [str(r) for r in (membro or {}).get("roles") or []]
        ver = [{"id": str(ch["id"]), "nome": ch.get("name", "")} for ch in canais_crew(info)
               if membro and pode_ver(info, gid, uid, cargos, ch)]
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
        app.logger.error("acesso: %s", str(e)[:120])
        return c[1] if c else None
    if len(_acesso_cache) > 500:
        for k in sorted(_acesso_cache, key=lambda k: _acesso_cache[k][0])[:250]:
            _acesso_cache.pop(k, None)
    _acesso_cache[uid] = (agora, ver, membro is not None, cargos)
    return ver


def e_membro(uid):
    """True se a pessoa está no servidor (qualquer cargo)."""
    acesso(uid)
    c = _acesso_cache.get(uid)
    return bool(c and len(c) > 2 and c[2])


def usuario():
    uid = str(session.get("uid") or "")
    return uid if re.fullmatch(r"\d{5,25}", uid) else None


def privado(resp, status=None):
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Vary"] = "Cookie"
    return (resp, status) if status else resp


def sem_acesso_crew():
    """None se quem pede tem acesso à área da crew; senão a resposta de bloqueio."""
    uid = usuario()
    if not uid:
        return privado(jsonify({"ok": False, "motivo": "login"}), 401)
    ver = acesso(uid)
    if ver is None:
        return privado(jsonify({"ok": False, "motivo": "erro"}), 503)
    if not ver:
        return privado(jsonify({"ok": False, "motivo": "cargo"}), 403)
    return None


def oauth_cfg():
    segredo = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
    if not segredo:
        return None
    cid = os.environ.get("DISCORD_CLIENT_ID", "").strip() or _app_id["id"]
    if not cid:
        try:
            cid = str(discord_get("/oauth2/applications/@me", os.environ.get("DISCORD_BOT_TOKEN", "").strip())["id"])
            _app_id["id"] = cid
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            return None
    local = request.host.split(":")[0] in ("127.0.0.1", "localhost")
    return {"id": cid, "segredo": segredo,
            "redirect": f"{'http' if local else 'https'}://{request.host}/auth/callback"}


@app.route("/auth/entrar")
def auth_entrar():
    cfg = oauth_cfg()
    volta = request.args.get("volta", "")
    session["volta"] = volta if re.fullmatch(r"[a-z0-9-]{1,40}", volta) else "inicio"
    if not cfg:
        return redirect("/#inicio")
    session["st"] = secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({"client_id": cfg["id"], "redirect_uri": cfg["redirect"], "response_type": "code",
                                "scope": "identify", "state": session["st"], "prompt": "none"})
    return redirect(f"https://discord.com/oauth2/authorize?{q}")


@app.route("/auth/callback")
def auth_callback():
    cfg = oauth_cfg()
    st = session.pop("st", "")
    volta = session.pop("volta", "inicio")
    if not cfg or not st or not hmac.compare_digest(request.args.get("state", ""), st) or not request.args.get("code"):
        return redirect("/#inicio")
    try:
        req = urllib.request.Request("https://discord.com/api/v10/oauth2/token", method="POST",
                                     data=urllib.parse.urlencode({
                                         "client_id": cfg["id"], "client_secret": cfg["segredo"],
                                         "grant_type": "authorization_code", "code": request.args["code"][:200],
                                         "redirect_uri": cfg["redirect"]}).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "KyzenSA-Site (1.0)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            tok = json.loads(r.read().decode("utf-8"))["access_token"]
        req = urllib.request.Request(API + "/users/@me", headers={"Authorization": f"Bearer {tok}",
                                                                   "User-Agent": "KyzenSA-Site (1.0)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            u = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
        app.logger.error("login: %s", str(e)[:120])
        return redirect("/#inicio")
    session.clear()  # o token do Discord não é guardado: só o ID, o nome e o avatar
    session.permanent = True
    av = str(u.get("avatar") or "")
    session.update(uid=str(u["id"]), nome=cortar(str(u.get("global_name") or u.get("username") or ""), 40),
                   av=av if re.fullmatch(r"(a_)?[a-f0-9]{32}", av) else "")
    _acesso_cache.pop(str(u["id"]), None)
    return redirect(f"/#{volta}")


@app.route("/auth/sair", methods=["POST"])
def auth_sair():
    origem = request.headers.get("Origin", "")
    if origem not in (f"https://{request.host}", f"http://{request.host}"):
        return jsonify({"ok": False}), 403
    session.clear()
    return privado(jsonify({"ok": True}))


def avatar_url(uid, av):
    if av:
        return f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=64"
    try:
        return f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png"
    except ValueError:
        return "https://cdn.discordapp.com/embed/avatars/0.png"


@app.route("/api/eu")
def eu():
    configurado = bool(os.environ.get("DISCORD_CLIENT_SECRET", "").strip())
    uid = usuario()
    if not uid:
        return privado(jsonify({"logado": False, "configurado": configurado}))
    ver = acesso(uid)
    return privado(jsonify({"logado": True, "configurado": configurado, "nome": session.get("nome", ""),
                            "avatar": avatar_url(uid, session.get("av", "")), "crew": bool(ver),
                            "erro": ver is None, "canais": ver or [],
                            "guild": os.environ.get("DISCORD_GUILD_ID", "").strip(),
                            "staff": nivel_uid(uid)}))


def ctx_rapido(info, msgs):
    ctx = {"roles": {}, "channels": {}, "users": {}}
    for r in info["roles"]:
        cor = cor_hex((r.get("colors") or {}).get("primary_color")) or cor_hex(r.get("color"))
        ctx["roles"][str(r["id"])] = (r.get("name", "cargo"), cor)
    for c in info["canais"]:
        ctx["channels"][str(c["id"])] = c.get("name", "canal")
    for m in msgs:
        for u in m.get("mentions") or []:
            ctx["users"][u["id"]] = u.get("global_name") or u.get("username") or "usuário"
    return ctx


@app.route("/api/crew/canal/<cid>")
def crew_canal(cid):
    bloqueio = sem_acesso_crew()
    if bloqueio:
        return bloqueio
    if not any(c["id"] == cid for c in acesso(usuario())):
        return privado(jsonify({"ok": False, "motivo": "cargo"}), 403)
    agora = time.time()
    c = _msgs_cache.get(cid)
    if not c or agora - c[0] > 15:
        try:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            msgs = discord_get(f"/channels/{cid}/messages?limit=50", token)
            ctx = ctx_rapido(guild_info(), msgs)
            lista = []
            for m in reversed(msgs):
                html = render_msg(m, ctx)
                if not html:
                    continue
                a = m.get("author") or {}
                av = str(a.get("avatar") or "")
                lista.append({"id": m["id"], "autor": cortar(str(a.get("global_name") or a.get("username") or "usuário"), 40),
                              "av": avatar_url(str(a.get("id", "0")), av if re.fullmatch(r"(a_)?[a-f0-9]{32}", av) else ""),
                              "bot": bool(a.get("bot")), "ts": m.get("timestamp"), "html": html})
            c = (agora, lista)
            _msgs_cache[cid] = c
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            app.logger.error("crew canal: %s", str(e)[:120])
            if not c:
                return privado(jsonify({"ok": False, "motivo": "erro"}), 503)
    return privado(jsonify({"ok": True, "mensagens": c[1]}))


@app.route("/api/crew/novidades")
def crew_novidades():
    bloqueio = sem_acesso_crew()
    if bloqueio:
        return bloqueio
    if time.time() - _cache["t"] > CACHE_SECONDS:
        novidades()  # atualiza o cache compartilhado
    return privado(jsonify({"ok": True, "news": [n for n in _cache["data"] or [] if n.get("tag") in CREW_TAGS]}))


# ---------------------------------------------------------------------------
# Hall da Fama (últimos 7 dias)
#   Piratas em ascensão: quem mais subiu de nível no # level-up  -> para membros do servidor logados
#   Crew da semana e maior vitória: do # wars-results            -> só para quem é da crew
# ---------------------------------------------------------------------------
_hall_cache = {"t": 0, "data": None}
_NIVEL = re.compile(r"(?:n[íi]vel|level|lvl|lv|nv)\.?[\s:#*'\"`]*(\d{1,4})(?!\d)", re.I)  # Loritta: nível **'3'**


def textos_msg(m):
    t = [m.get("content") or ""]
    for e in m.get("embeds") or []:
        t += [e.get("title") or "", e.get("description") or ""]
        t += [f"{f.get('name', '')} {f.get('value', '')}" for f in e.get("fields") or []]
    return "\n".join(t)


def buscar_ascensao():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    canal = os.environ.get("LEVELUP_CHANNEL", "").strip() or achar_canal(token, gid, "level-up")
    if not canal:
        return []
    msgs = discord_get(f"/channels/{canal}/messages?limit=100", token)
    limite = datetime.now(timezone.utc) - timedelta(days=7)
    gente = {}
    for m in msgs:
        if datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")) < limite:
            continue
        txt = textos_msg(m)
        quem = (m.get("mentions") or [None])[0]
        if not quem:
            continue
        niveis = [int(n) for n in _NIVEL.findall(txt)]
        p = gente.setdefault(quem["id"], {"nome": cortar(str(quem.get("global_name") or quem.get("username") or ""), 32),
                                          "av": avatar_url(quem["id"], quem.get("avatar") or ""), "subidas": 0, "nivel": 0})
        p["subidas"] += 1
        if niveis:
            p["nivel"] = max(p["nivel"], max(niveis))
    return sorted(gente.values(), key=lambda p: (-p["subidas"], -p["nivel"], p["nome"].lower()))[:5]


def semana_crews(dados):
    """Crew da semana (mais pontos em 7 dias) e maior vitória, a partir das partidas do placar."""
    limite = time.time() - 7 * 86400
    pts, melhor = {}, None
    for p in (dados or {}).get("todas") or []:
        if p["ts"] < limite:
            continue
        for nome, gp, gc in ((p["a"], p["ga"], p["gb"]), (p["b"], p["gb"], p["ga"])):
            o = pts.setdefault(nome, {"crew": nome, "pts": 0, "v": 0})
            o["pts"] += 3 if gp > gc else 1 if gp == gc else 0
            o["v"] += 1 if gp > gc else 0
        if p["ga"] != p["gb"] and (not melhor or abs(p["ga"] - p["gb"]) > abs(melhor["ga"] - melhor["gb"])):
            melhor = p
    top = sorted(pts.values(), key=lambda o: (-o["pts"], -o["v"], o["crew"].lower()))
    if melhor and melhor["gb"] > melhor["ga"]:
        melhor = {**melhor, "a": melhor["b"], "b": melhor["a"], "ga": melhor["gb"], "gb": melhor["ga"]}
    return {"crew": top[0] if top else None, "maior": melhor}


@app.route("/api/hall")
def hall():
    uid = usuario()
    if debug_ok():  # para a staff conferir o formato das mensagens do # level-up
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        canal = achar_canal(token, os.environ.get("DISCORD_GUILD_ID", "").strip(), "level-up")
        msgs = discord_get(f"/channels/{canal}/messages?limit=5", token) if canal else []
        return privado(jsonify({"canal": bool(canal), "amostra": [
            {"autor_bot": bool((m.get("author") or {}).get("bot")), "mencoes": len(m.get("mentions") or []),
             "texto": cortar(re.sub(r"<@!?\d+>", "<@…>", textos_msg(m)), 200)} for m in msgs]}))
    if not uid:
        return privado(jsonify({"ok": False, "motivo": "login"}), 401)
    if not e_membro(uid):
        return privado(jsonify({"ok": False, "motivo": "membro"}), 403)
    agora = time.time()
    if (agora - _hall_cache["t"] > 300 or _hall_cache["data"] is None) and pode_tentar("hall", 60):
        try:
            _hall_cache.update(t=agora, data=buscar_ascensao())
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            falhou("hall")
            app.logger.error("hall: %s", str(e)[:120])
    saida = {"ok": True, "ascensao": _hall_cache["data"] or [], "crew_ok": bool(acesso(uid))}
    if saida["crew_ok"]:
        if _wars_cache["data"] is None or agora - _wars_cache["t"] > 60:
            try:
                _wars_cache.update(t=agora, data=buscar_wars())
            except (urllib.error.URLError, ValueError, KeyError, TypeError):
                pass
        saida.update(semana_crews(_wars_cache["data"]))
    return privado(jsonify(saida))


# ---------------------------------------------------------------------------
# Comandos do bot (/codigos /fruta /ilha /evento /frota /site)
#   O Discord manda cada comando para /api/interacoes (Developer Portal > General Information >
#   Interactions Endpoint URL). Cada pedido é conferido com a assinatura Ed25519 do Discord.
# ---------------------------------------------------------------------------
try:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
except ImportError:  # pragma: no cover
    VerifyKey = BadSignatureError = None

SITE = "https://kyzensaofc.vercel.app"
COR = 0xC9954F
_chave_pub = {"k": ""}
COMANDOS = [
    {"name": "codigos", "description": "Códigos do Vox Seas (do Trello oficial)"},
    {"name": "fruta", "description": "Poderes de uma Vox Fruit",
     "options": [{"type": 3, "name": "nome", "description": "Nome da fruta", "required": True, "autocomplete": True}]},
    {"name": "ilha", "description": "Para qual ilha ir no seu nível",
     "options": [{"type": 4, "name": "nivel", "description": "Seu nível no Vox Seas", "required": True,
                  "min_value": 0, "max_value": 5000}]},
    {"name": "drop", "description": "Onde dropa um item, ou o que um chefe dropa",
     "options": [{"type": 3, "name": "busca", "description": "Item ou chefe", "required": True, "autocomplete": True}]},
    {"name": "evento", "description": "Próximo evento do servidor"},
    {"name": "frota", "description": "Top 3 da Frota (só para quem é da crew)"},
    {"name": "site", "description": "Site da Kyzen SA com Trello, Frota e eventos"},
    {"name": "cupom", "description": "Cupom de desconto da Kyzen na The Blox, nossa parceira"},
]


def chave_publica():
    k = os.environ.get("DISCORD_PUBLIC_KEY", "").strip() or _chave_pub["k"]
    if not k:
        info = discord_get("/oauth2/applications/@me", os.environ.get("DISCORD_BOT_TOKEN", "").strip())
        k = _chave_pub["k"] = str(info["verify_key"])
        _app_id["id"] = str(info["id"])
    return k


def assinatura_ok():
    if VerifyKey is None:
        return False
    try:
        VerifyKey(bytes.fromhex(chave_publica())).verify(
            request.headers.get("X-Signature-Timestamp", "").encode() + request.get_data(),
            bytes.fromhex(request.headers.get("X-Signature-Ed25519", "")))
        return True
    except (BadSignatureError, ValueError, TypeError, KeyError, urllib.error.URLError):
        return False


def md_discord(txt, n=1500):
    """Markdown do Trello -> texto do Discord (sem imagens e sem os sufixos smartCard)."""
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", txt or "")
    txt = re.sub(r"\]\((https?://[^\s)]+)\s+\"[^\"]*\"\)", r"](\1)", txt)
    txt = re.sub(r"\[(https?://[^\]]+)\]\((https?://[^)\s]+)\)", r"\2", txt)
    txt = re.sub(r"(?m)^#{4,6} ", "### ", txt)
    txt = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", txt)
    txt = re.sub(r"@(everyone|here)", r"@​\1", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return cortar(txt, n)


def botoes(*extra):
    b = [{"type": 2, "style": 5, "label": "Abrir o site", "url": SITE}] + list(extra)
    return [{"type": 1, "components": b[:5]}]


def resposta(embed=None, texto="", privada=False, componentes=None):
    d = {"allowed_mentions": {"parse": []}}
    if embed:
        embed.setdefault("color", COR)
        embed.setdefault("footer", {"text": "Kyzen SA · kyzensaofc.vercel.app"})
        d["embeds"] = [embed]
    if texto:
        d["content"] = texto
    if privada:
        d["flags"] = 64
    d["components"] = componentes if componentes is not None else botoes()
    return jsonify({"type": 4, "data": d})


def frutas():
    return [c for c in lista_trello("Vox Fruits") if c.get("tem")]


def cmd_codigos():
    card = next((c for c in lista_trello("Info") if c["nome"].lower().startswith("codes")), None)
    if not card:
        return resposta(texto="Não consegui abrir o Trello agora. Tente de novo em instantes.", privada=True)
    det = _trello_cache["cards"].get(card["id"]) or {}
    e = {"title": "🎟️ Códigos do Vox Seas",
         "description": "Os códigos ativos ficam no cartão **Codes** do Trello oficial do jogo. "
                        "Resgate dentro do Vox Seas no menu de códigos. Corre que alguns expiram! ⏳",
         "url": det.get("url") or TRELLO_URL}
    if det.get("img"):
        e["image"] = {"url": det["img"]}
    return resposta(e, componentes=botoes({"type": 2, "style": 5, "label": "Ver no Trello", "url": det.get("url") or TRELLO_URL}))


def cmd_fruta(nome):
    alvo = slug_py(nome)
    lista = frutas()
    f = next((c for c in lista if slug_py(c["nome"]) == alvo), None) or \
        next((c for c in lista if alvo and alvo in slug_py(c["nome"])), None)
    if not f:
        return resposta(texto=f"Não achei a fruta **{sem_markdown(nome)[:40]}** no Trello. Use as sugestões da lista ao digitar.", privada=True)
    det = _trello_cache["cards"].get(f["id"]) or {}
    e = {"title": f"🍎 {f['nome']}", "description": md_discord(det.get("desc")) or "Sem descrição no Trello.",
         "url": det.get("url") or TRELLO_URL}
    if f["tags"]:
        e["fields"] = [{"name": "Raridade", "value": " · ".join(t["n"] for t in f["tags"]), "inline": True}]
    if f.get("img"):
        e["thumbnail"] = {"url": f["img"]}
    return resposta(e, componentes=botoes({"type": 2, "style": 5, "label": "Ver no Trello", "url": det.get("url") or TRELLO_URL}))


def cmd_ilha(nivel):
    ilhas = sorted(((m.group(1), int(m.group(2)), int(m.group(3)), c) for c in lista_trello("Islands")
                    if (m := _ILHA.match(c["nome"]))), key=lambda i: i[1])
    if not ilhas:
        return resposta(texto="Não consegui abrir o mapa agora. Tente de novo em instantes.", privada=True)
    certas = [i for i in ilhas if i[1] <= nivel <= i[2]]
    if not certas:
        certas = [ilhas[-1]] if nivel > ilhas[-1][2] else [min(ilhas, key=lambda i: abs(i[1] - nivel))]
    prox = next((i for i in ilhas if i[1] > nivel), None)
    nomes = " ou ".join(f"**{i[0]}** (Lvl {i[1]}-{i[2]})" for i in certas)
    e = {"title": f"🧭 Nível {nivel}: para onde navegar",
         "description": f"Seu lugar agora é {nomes}.\n" +
                        (f"Próximo destino: **{prox[0]}** a partir do nível {prox[1]}." if prox else "Você já chegou nos mares mais perigosos! 🌋"),
         "url": SITE + "/#comunidade"}
    if certas[0][3].get("img"):
        e["thumbnail"] = {"url": certas[0][3]["img"]}
    return resposta(e, componentes=botoes({"type": 2, "style": 5, "label": "Ver o mapa", "url": SITE + "/#comunidade"}))


def cmd_drop(busca):
    g = guia_cache() or {"chefes": [], "itens": []}
    alvo = slug_py(busca)
    chefe = next((c for c in g["chefes"] if slug_py(c["nome"]) == alvo), None) or \
        next((c for c in g["chefes"] if alvo and alvo in slug_py(c["nome"])), None)
    item = next((i for i in g["itens"] if slug_py(i["nome"]) == alvo), None) or \
        next((i for i in g["itens"] if alvo and alvo in slug_py(i["nome"])), None)
    if item and (not chefe or slug_py(item["nome"]) == alvo):
        linhas = [f"• **{g['chefes'][i]['nome']}**" + (f" — {g['chefes'][i]['ilha']}" if g["chefes"][i]["ilha"] else "")
                  for i in item["chefes"]]
        return resposta({"title": f"🔎 Onde dropa: {item['nome']}", "description": "\n".join(linhas)[:3500],
                         "fields": [{"name": "Tipo", "value": item["cat"], "inline": True}], "url": SITE + "/#guia"},
                        componentes=botoes({"type": 2, "style": 5, "label": "Abrir o guia", "url": SITE + "/#guia"}))
    if chefe:
        e = {"title": ("🐉 " if chefe["raid"] else "💀 ") + chefe["nome"], "url": SITE + "/#guia",
             "description": (f"📍 **{chefe['ilha']}**\n" if chefe["ilha"] else "") + ("Raid boss" if chefe["raid"] else ""),
             "fields": [{"name": cat, "value": cortar("\n".join(f"• {x}" for x in lst), 1000), "inline": True}
                        for cat, lst in chefe["drops"].items()][:6]}
        if chefe.get("img"):
            e["thumbnail"] = {"url": chefe["img"]}
        return resposta(e, componentes=botoes({"type": 2, "style": 5, "label": "Abrir o guia", "url": SITE + "/#guia"}))
    return resposta(texto=f"Não achei **{sem_markdown(busca)[:40]}**. Use as sugestões da lista ao digitar.", privada=True)


def cmd_evento():
    if _eventos_cache["data"] is None or time.time() - _eventos_cache["t"] > 120:
        try:
            _eventos_cache.update(t=time.time(), data=buscar_eventos() or [])
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            pass
    evs = _eventos_cache["data"] or []
    if not evs:
        return resposta({"title": "📅 Nenhum evento agendado agora",
                         "description": "Fique de olho na aba **Eventos** do servidor e no site: assim que tiver, aparece com contagem regressiva!"})
    ev = evs[0]
    ts = int(datetime.fromisoformat(ev["inicio"].replace("Z", "+00:00")).timestamp())
    e = {"title": ("🔴 Acontecendo agora: " if ev["ao_vivo"] else "📅 ") + ev["nome"], "url": ev["link"],
         "description": (ev["desc"] + "\n\n" if ev["desc"] else "") +
                        (f"Começou <t:{ts}:R>." if ev["ao_vivo"] else f"**<t:{ts}:F>** (<t:{ts}:R>)"),
         "fields": [{"name": "Interessados", "value": str(ev["interessados"]), "inline": True}]}
    if ev["local"]:
        e["fields"].append({"name": "Onde", "value": ev["local"], "inline": True})
    if ev["img"]:
        e["image"] = {"url": ev["img"]}
    return resposta(e, componentes=botoes({"type": 2, "style": 5, "label": "Tenho interesse", "url": ev["link"]}))


def cmd_frota(uid):
    if not uid or not acesso(uid):
        return resposta(texto="🔒 A Frota é só para quem é da crew. Quer entrar numa? Faça o apply em #kyzen-apply!", privada=True)
    if _wars_cache["data"] is None or time.time() - _wars_cache["t"] > 30:
        try:
            _wars_cache.update(t=time.time(), data=buscar_wars())
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            pass
    r = (_wars_cache["data"] or {}).get("ranking") or []
    if not r:
        return resposta(texto="⚓ Nenhuma war registrada ainda no # wars-results.", privada=True)
    linhas = [f"{['🥇', '🥈', '🥉'][i]} **{sem_markdown(c['crew'])}**: {c['pts']} pts ({c['v']}V {c['e']}E {c['d']}D)"
              for i, c in enumerate(r[:3])]
    return resposta({"title": "🏴‍☠️ Top 3 da Frota", "description": "\n".join(linhas), "url": SITE + "/#crews"},
                    privada=True)


# ---------------------------------------------------------------------------
# Moderação (staff): clique direito no membro/mensagem -> "Advertir", ou /advertir.
#   O bot aplica sozinho a tabela das regras (1 warn = mute 15 min ... 5 warns = ban),
#   avisa o membro na DM e registra tudo no canal privado #registro-punições.
#   Os warns são contados pelas mensagens desse canal (não precisa de banco).
# ---------------------------------------------------------------------------
PERM_ADMIN, PERM_KICK, PERM_BAN = 1 << 3, 1 << 1, 1 << 2
PERM_VER, PERM_HIST, PERM_MODERAR = 1 << 10, 1 << 16, 1 << 40
CANAL_TICKET = "1398749616832254002"
NOME_REGISTRO = "🛡️・registro-punições"
LIMITE_BAN = 5
PUNICOES = {1: (15 * 60, "Mute de 15 minutos"), 2: (3600, "Mute de 1 hora"),
            3: (86400, "Mute de 1 dia"), 4: (3 * 86400, "Mute de 3 dias")}
ST_WARN, ST_VERBAL, ST_PERDOADA, ST_PERDAO = "advertência", "aviso verbal", "advertência perdoada", "perdão"
_registro = {"id": ""}

COMANDOS += [
    {"type": 2, "name": "Advertir membro", "default_member_permissions": str(PERM_MODERAR)},
    {"type": 3, "name": "Advertir autor", "default_member_permissions": str(PERM_MODERAR)},
    {"name": "advertir", "description": "Dar uma advertência (o bot aplica a punição da tabela)",
     "default_member_permissions": str(PERM_MODERAR),
     "options": [{"type": 6, "name": "membro", "description": "Quem levou a advertência", "required": True},
                 {"type": 3, "name": "motivo", "description": "O que a pessoa fez", "required": True,
                  "max_length": 500},
                 {"type": 3, "name": "tipo", "description": "Advertência normal ou só aviso verbal",
                  "choices": [{"name": "Advertência (conta na tabela)", "value": "warn"},
                              {"name": "Aviso verbal (sem punição)", "value": "verbal"}]},
                 {"type": 3, "name": "prova", "description": "Link da mensagem ou do print", "max_length": 1000}]},
    {"name": "historico", "description": "Ver as advertências de um membro",
     "default_member_permissions": str(PERM_MODERAR),
     "options": [{"type": 6, "name": "membro", "description": "Membro", "required": True}]},
    {"name": "perdoar", "description": "Tirar a última advertência de um membro (e o mute atual)",
     "default_member_permissions": str(PERM_MODERAR),
     "options": [{"type": 6, "name": "membro", "description": "Membro", "required": True},
                 {"type": 3, "name": "motivo", "description": "Por que está perdoando", "max_length": 300}]},
    {"name": "denunciar", "description": "Como denunciar alguém para a staff"},
    {"name": "ajuda", "description": "Qual bot usar para cada coisa no servidor"},
    {"type": 3, "name": "Denunciar mensagem"},
]


def discord_req(metodo, path, dados=None, motivo=""):
    h = {"Authorization": f"Bot {os.environ.get('DISCORD_BOT_TOKEN', '').strip()}",
         "User-Agent": "KyzenSA-Site (1.0)", "Content-Type": "application/json"}
    if motivo:
        h["X-Audit-Log-Reason"] = urllib.parse.quote(cortar(motivo, 400))
    req = urllib.request.Request(API + path, method=metodo, headers=h,
                                 data=None if dados is None else json.dumps(dados).encode("utf-8"))
    with urllib.request.urlopen(req, timeout=4) as r:
        corpo = r.read().decode("utf-8")
        return json.loads(corpo) if corpo else {}


def tem_perm(membro, bits):
    p = int((membro or {}).get("permissions") or 0)
    return bool(p & PERM_ADMIN or p & bits)


def canal_registro(criar=False):
    if _registro["id"]:
        return _registro["id"]
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    cid = os.environ.get("CANAL_PUNICOES", "").strip() or achar_canal(token, gid, "registro-punicoes")
    if not cid and criar:
        cid = criar_registro(gid)
    _registro["id"] = cid
    return cid


def criar_registro(gid):
    """Cria o #registro-punições visível só para quem modera."""
    return criar_canal_staff(gid, NOME_REGISTRO, "Registro automático das advertências do Kyzen Bot. Não apague as "
                             "mensagens daqui: é por elas que o bot conta os warns de cada membro.", GUIA_STAFF,
                             "Registro de punições do Kyzen Bot")


GUIA_STAFF = {
    "title": "🛡️ Como advertir alguém (guia rápido da staff)", "color": 0xC9954F,
    "description":
        "**Jeito mais fácil:** clique com o botão direito no **membro** (ou na **mensagem** dele) → "
        "**Apps** → **Advertir membro** / **Advertir autor**. Escreva o motivo e pronto.\n"
        "No celular: segure o dedo em cima do nome ou da mensagem → **Apps**.\n\n"
        "**Ou pelo comando:** `/advertir membro motivo` (dá pra escolher *aviso verbal* e colar a prova).\n\n"
        "**O bot faz o resto sozinho:** conta os warns, aplica a punição certa, avisa o membro na DM "
        "e registra aqui neste canal.",
    "fields": [
        {"name": "📋 Tabela de punições", "inline": False,
         "value": "Aviso verbal → sem punição\n1º warn → mute de 15 minutos\n2º warn → mute de 1 hora\n"
                  "3º warn → mute de 1 dia\n4º warn → mute de 3 dias\n5º warn → **ban**"},
        {"name": "🔎 Outros comandos", "inline": False,
         "value": "`/historico membro` → ver os warns de alguém\n"
                  "`/perdoar membro` → tirar o último warn (e o mute atual)"},
        {"name": "🪜 Quem pode o quê", "inline": False,
         "value": "**Helper:** aviso verbal · **Junior Staff:** 1º e 2º warn · **Staff:** 3º warn · "
                  "**Senior Staff:** 4º warn e `/perdoar` · **Mod+:** 5º warn (ban)\n"
                  "Se a punição passar do seu cargo, o bot manda um **pedido de aprovação** aqui e alguém acima "
                  "aprova com um clique. O membro punido pode **contestar** pela DM."},
        {"name": "⚠️ Importante", "inline": False,
         "value": "Não apague as mensagens deste canal: o bot conta os warns por elas."},
    ],
}


def registros(uid, paginas=2):
    """Registros do membro no #registro-punições, do mais novo para o mais antigo."""
    reg = canal_registro()
    if not reg:
        raise ValueError("sem canal de registro")
    out, antes = [], ""
    for _ in range(paginas):
        lote = discord_req("GET", f"/channels/{reg}/messages?limit=100" + (f"&before={antes}" if antes else ""))
        for m in lote:
            if not (m.get("author") or {}).get("bot"):
                continue
            for e in m.get("embeds") or []:
                ach = re.match(r"^Membro (\d{5,25}) · (.+)$", ((e.get("footer") or {}).get("text") or ""))
                if ach and ach.group(1) == uid:
                    out.append({"status": ach.group(2), "msg": m, "embed": e})
        if len(lote) < 100:
            break
        antes = lote[-1]["id"]
    return out


def campo(e, nome):
    return next((f.get("value", "") for f in e.get("fields") or [] if f.get("name", "").endswith(nome)), "")


def ticket_link():
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    return f"https://discord.com/channels/{gid}/{CANAL_TICKET}"


def advertir(gid, staff_id, alvo_id, alvo_nome, motivo, prova="", verbal=False, nivel_staff=9, pedido_por=""):
    ativos = [r for r in registros(alvo_id) if r["status"] == ST_WARN]
    n = len(ativos) + (0 if verbal else 1)
    if verbal:
        punicao, segundos = "Nenhuma (aviso verbal)", 0
    elif n >= LIMITE_BAN:
        punicao, segundos = "Banimento", -1
    else:
        segundos, punicao = PUNICOES[n]
    motivo_audit = f"{'Aviso verbal' if verbal else f'Warn {n}/{LIMITE_BAN}'} por {staff_id}: {motivo}"
    req = WARN_REQ[0 if verbal else min(n, LIMITE_BAN)]
    if nivel_staff < req:
        return pedir_aprovacao(staff_id, alvo_id, motivo, prova, verbal, n, punicao, req)

    # 1) avisa na DM antes (depois de um ban o bot não consegue mais falar com a pessoa)
    dm_ok = True
    try:
        dm = discord_req("POST", "/users/@me/channels", {"recipient_id": alvo_id})
        discord_req("POST", f"/channels/{dm['id']}/messages", {"embeds": [{
            "title": "🗣️ Você recebeu um aviso na Kyzen SA" if verbal else
                     f"⚠️ Você recebeu uma advertência na Kyzen SA ({n}/{LIMITE_BAN})",
            "color": 0xE0A800 if verbal else 0xD9534F,
            "description": f"**Motivo:** {cortar(motivo, 900)}\n**Punição:** {punicao}\n\n"
                           + ("" if verbal or n >= LIMITE_BAN else
                              f"Com **{LIMITE_BAN} advertências** a punição é banimento. Leia as regras para evitar novas.\n")
                           + "Se achar que foi injusto, clique em **Contestar** e explique o que aconteceu.",
        }], "components": [{"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Contestar", "emoji": {"name": "📨"}, "custom_id": f"ct:{alvo_id}"}]}],
            "allowed_mentions": {"parse": []}})
    except (urllib.error.URLError, KeyError, ValueError):
        dm_ok = False

    # 2) aplica a punição
    aplicada, erro = True, ""
    try:
        if segundos > 0:
            ate = (datetime.now(timezone.utc) + timedelta(seconds=segundos)).isoformat()
            discord_req("PATCH", f"/guilds/{gid}/members/{alvo_id}", {"communication_disabled_until": ate}, motivo_audit)
        elif segundos < 0:
            discord_req("PUT", f"/guilds/{gid}/bans/{alvo_id}", {"delete_message_seconds": 0}, motivo_audit)
    except urllib.error.HTTPError as e:
        aplicada, erro = False, ("o cargo da pessoa está acima do bot" if e.code == 403 else
                                 "a pessoa não está mais no servidor" if e.code == 404 else f"erro {e.code}")
    except urllib.error.URLError:
        aplicada, erro = False, "o Discord não respondeu"

    # 3) registra
    reg = canal_registro()
    titulo = "🗣️ Aviso verbal" if verbal else f"⚠️ Advertência {n}/{LIMITE_BAN}"
    log = {"title": titulo, "color": 0xE0A800 if verbal else (0x8B0000 if segundos < 0 else 0xD9534F),
           "fields": [{"name": "👤 Membro", "value": f"<@{alvo_id}>" + (f" ({cortar(alvo_nome, 60)})" if alvo_nome != alvo_id else ""), "inline": True},
                      {"name": "🛡️ Staff", "value": f"<@{staff_id}>", "inline": True},
                      {"name": "⚖️ Punição", "value": punicao + ("" if aplicada else f" ❌ não aplicada: {erro}"),
                       "inline": True},
                      {"name": "📝 Motivo", "value": cortar(motivo, 1000), "inline": False}],
           "footer": {"text": f"Membro {alvo_id} · {ST_VERBAL if verbal else ST_WARN}"},
           "timestamp": datetime.now(timezone.utc).isoformat()}
    if prova:
        log["fields"].append({"name": "📎 Prova", "value": cortar(prova, 1000), "inline": False})
    if pedido_por:
        log["fields"].append({"name": "🙋 Pedido por", "value": f"<@{pedido_por}>", "inline": True})
    try:
        discord_req("POST", f"/channels/{reg}/messages", {"embeds": [log], "allowed_mentions": {"parse": []}})
    except urllib.error.URLError:
        erro = erro or "não consegui registrar no #registro-punições"

    linhas = [f"**Membro:** <@{alvo_id}>", f"**Punição:** {punicao}" + ("" if aplicada else f" ❌ ({erro})")]
    if not verbal:
        prox = "—" if n >= LIMITE_BAN else ("Banimento" if n + 1 >= LIMITE_BAN else PUNICOES[n + 1][1])
        linhas.append(f"**Advertências:** {n}/{LIMITE_BAN} · próxima punição: {prox}")
    linhas.append("📩 Avisado na DM" if dm_ok else "📪 Não consegui avisar na DM (DM fechada)")
    linhas.append(f"🗂️ Registrado em <#{reg}>")
    return resposta({"title": ("✅ Pronto: " if aplicada else "⚠️ Registrado, mas a punição falhou: ") + titulo,
                     "description": "\n".join(linhas)}, privada=True, componentes=[])


def cmd_historico(uid):
    regs = registros(uid)
    ativos = [r for r in regs if r["status"] == ST_WARN]
    if not regs:
        return resposta({"title": "🗂️ Histórico", "description": f"<@{uid}> não tem nenhum registro. 😇"},
                        privada=True, componentes=[])
    icone = {ST_WARN: "⚠️", ST_VERBAL: "🗣️", ST_PERDOADA: "~~⚠️~~", ST_PERDAO: "✅"}
    linhas = []
    for r in regs[:12]:
        ts = int(snowflake_ts(r["msg"]["id"]))
        motivo = cortar(campo(r["embed"], "Motivo").replace("\n", " "), 90)
        linhas.append(f"{icone.get(r['status'], '•')} <t:{ts}:d> · {r['status']} · {motivo} · por {campo(r['embed'], 'Staff')}")
    n = len(ativos)
    prox = "Banimento" if n + 1 >= LIMITE_BAN else PUNICOES[n + 1][1]
    return resposta({"title": "🗂️ Histórico de punições",
                     "description": f"<@{uid}> · **{n}/{LIMITE_BAN} advertências ativas** · próxima: {prox}\n\n"
                                    + "\n".join(linhas)}, privada=True, componentes=[])


def cmd_perdoar(gid, staff_id, uid, motivo):
    ativos = [r for r in registros(uid) if r["status"] == ST_WARN]
    if not ativos:
        return resposta(texto=f"<@{uid}> não tem advertência ativa para perdoar.", privada=True, componentes=[])
    alvo = ativos[0]
    e = dict(alvo["embed"])
    e["title"] = f"~~{e.get('title', 'Advertência')}~~ (perdoada)"
    e["color"] = 0x6C757D
    e["footer"] = {"text": f"Membro {uid} · {ST_PERDOADA}"}
    reg = canal_registro()
    discord_req("PATCH", f"/channels/{reg}/messages/{alvo['msg']['id']}", {"embeds": [e]})
    try:
        discord_req("PATCH", f"/guilds/{gid}/members/{uid}", {"communication_disabled_until": None},
                    f"Perdão por {staff_id}: {motivo}")
    except urllib.error.URLError:
        pass
    discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
        "title": "✅ Advertência perdoada", "color": 0x28A745,
        "fields": [{"name": "👤 Membro", "value": f"<@{uid}>", "inline": True},
                   {"name": "🛡️ Staff", "value": f"<@{staff_id}>", "inline": True},
                   {"name": "📝 Motivo", "value": cortar(motivo or "Sem motivo informado", 1000), "inline": False}],
        "footer": {"text": f"Membro {uid} · {ST_PERDAO}"},
        "timestamp": datetime.now(timezone.utc).isoformat()}], "allowed_mentions": {"parse": []}})
    return resposta({"title": "✅ Advertência perdoada",
                     "description": f"<@{uid}> agora tem **{len(ativos) - 1}/{LIMITE_BAN}** advertências ativas. "
                                    "Se estava mutado, o mute foi tirado."}, privada=True, componentes=[])


def cmd_denunciar(link_msg=""):
    passos = ("**1.** Tire **print** do que aconteceu\n"
              + (f"**2.** Copie o link da mensagem: {link_msg}\n" if link_msg else
                 "**2.** Se for uma mensagem, segure/clique com o botão direito → **Copiar link da mensagem**\n")
              + f"**3.** Abra um ticket em <#{CANAL_TICKET}> e escolha **Denúncia**\n"
                "**4.** Cole o link, mande o print e diga quem foi e o que aconteceu\n\n"
                "A staff analisa e você recebe a resposta no ticket. Sua denúncia **não é pública**. 🤝")
    return resposta({"title": "🚩 Como denunciar alguém", "description": passos},
                    privada=True, componentes=[{"type": 1, "components": [
                        {"type": 2, "style": 5, "label": "Abrir ticket", "url": ticket_link()}]}])


def modal_advertir(alvo_id, alvo_nome, prova=""):
    caixas = [{"type": 4, "custom_id": "motivo", "label": "Motivo (o que a pessoa fez?)", "style": 2,
               "required": True, "max_length": 500, "placeholder": "Ex.: ofendeu outro membro no #chat-geral"},
              {"type": 4, "custom_id": "prova", "label": "Prova: link da mensagem ou do print", "style": 2,
               "required": False, "max_length": 1000, "placeholder": "Opcional"},
              {"type": 4, "custom_id": "verbal", "label": "Só aviso verbal, sem punição? (sim/não)", "style": 1,
               "required": False, "max_length": 3, "placeholder": "Deixe vazio para advertência normal"}]
    if prova:
        caixas[1]["value"] = cortar(prova, 1000)
    return jsonify({"type": 9, "data": {"custom_id": f"adv:{alvo_id}", "title": cortar(f"Advertir {alvo_nome}", 45),
                                        "components": [{"type": 1, "components": [c]} for c in caixas]}})


def checar_alvo(staff_id, alvo, membro_alvo, nivel_staff=9):
    if not alvo or not alvo.get("id"):
        return "Não encontrei esse membro."
    if alvo.get("bot"):
        return "Não dá para advertir um bot."
    if str(alvo["id"]) == staff_id:
        return "Você não pode advertir a si mesmo. 😅"
    if membro_alvo and nivel(membro_alvo) and nivel(membro_alvo) >= nivel_staff:
        return "Essa pessoa tem cargo de staff igual ou maior que o seu. Fale com alguém acima."
    return ""


AJUDA = [
    ("🗺️ Vox Seas", "**Kyzen Bot**: `/codigos` `/fruta` `/ilha` `/drop` `/evento` `/site`"),
    ("⚔️ Entrar na crew", "Formulário no <#1398726258157949148>"),
    ("🎉 Sorteios", "Reaja com 🎉 na mensagem do sorteio para participar"),
    ("⚔️ Raids e bosses", "Use `/raid` no canal de raids para chamar a galera"),
    ("🎟️ Cupom da The Blox", "Use `/cupom`: desconto exclusivo da Kyzen na The Blox, nossa parceira"),
    ("💡 Sugestões", "Use `/sugerir` e a galera vota com 👍 / 👎 no <#1384921966254882958>"),
    ("🎫 Ajuda e denúncias", "Abra um ticket no <#1398749616832254002> · `/denunciar` mostra o passo a passo"),
    ("🎵 Música", "Entre numa call e use **/play** do **Jockie Music**"),
    ("🔊 Criar sua call", "Entre em **➕│Criar Call** e a sua sala aparece sozinha (bot **VoiceMaster**)"),
    ("📈 Níveis", "XP conversando, com a **Loritta** · tabela no <#1400524473563873300>"),
    ("🏙️ Alfacusa (jogo de cidade)", "Jogue no <#1572032026339446824>: comece com `++trabalhar` · guia no <#1572032027560120490>"),
    ("🤖 Onde usar comandos", "Use os comandos no <#1383957562696929342> para não poluir o chat"),
]
AJUDA_STAFF = ("🛡️ Staff (cada cargo tem os seus poderes)",
               "**Helper:** aviso verbal · `/historico` · tickets · formulários\n"
               "**Junior Staff:** warns de até 1h de mute · `/limpar` (20)\n"
               "**Staff:** warn até 1 dia · `/lento` · `/castigo` até 1h\n"
               "**Senior Staff:** warn até 3 dias · `/perdoar` · contestações · `/castigo` até 1 dia\n"
               "**Mod:** 5º warn (ban) · `/expulsar` · `/banir` · `/trancar` · `/limpar` (100) · `/castigo` até 7 dias\n"
               "**Senior Mod:** + `/sorteio` · `/war`\n"
               "**Adm+:** + painéis · `/agendar` · `/desbanir` · `/castigo` até 28 dias\n"
               "Passou do seu nível? O bot manda um **pedido de aprovação** para quem está acima.")


def cmd_ajuda(staff=False):
    campos = [{"name": n, "value": v, "inline": False} for n, v in AJUDA + ([AJUDA_STAFF] if staff else [])]
    return resposta({"title": "🧭 Qual bot usar para cada coisa",
                     "description": "Digite **/** e escolha o bot pelo ícone na lista. Aqui está o principal:",
                     "fields": campos}, privada=True)


# ---------------------------------------------------------------------------
# Kyzen Bot: tickets, sorteios, cargos por botão e formulário da crew
#   Tudo por botões e formulários do Discord (o bot não precisa ficar ligado).
#   Os registros ficam em canais privados da staff: #registro-bot e #formulários-crew.
# ---------------------------------------------------------------------------
PERM_GERENCIAR_SERVIDOR, PERM_GERENCIAR_CARGOS = 1 << 5, 1 << 28
PERM_ENVIAR, PERM_LINKS, PERM_ANEXOS = 1 << 11, 1 << 14, 1 << 15
PERIGOSAS = (PERM_ADMIN | PERM_KICK | PERM_BAN | PERM_MODERAR | PERM_GERENCIAR_SERVIDOR | PERM_GERENCIAR_CARGOS
             | (1 << 4) | (1 << 13) | (1 << 29) | (1 << 34))
EMOJI_SORTEIO = "🎉"
CANAIS_STAFF = {
    "bot": ("CANAL_REGISTRO_BOT", "🗂️・registro-bot", "registro-bot",
            "Registro do Kyzen Bot: cópias dos tickets fechados e controle dos sorteios. Não apague as mensagens."),
    "forms": ("CANAL_FORMULARIOS", "📋・formulários-crew", "formularios-crew",
              "Formulários de alistamento da crew. Clique em Aprovar ou Recusar em cada um."),
}
_canais_staff = {}
TICKETS = {
    "suporte": ("🎫", "Suporte / dúvida", "Conte a sua dúvida ou problema com detalhes. A staff já vai te atender."),
    "denuncia": ("🚩", "Denúncia", "Mande o **print**, o **link da mensagem** (botão direito → Copiar link) e diga "
                                 "**quem foi** e **o que aconteceu**. A sua denúncia fica só entre você e a staff."),
    "outro": ("🤝", "Outro assunto", "Parcerias, sugestões ou qualquer outro assunto. Pode falar!"),
}
DURACOES = {"prox": 0, "1d": 86400, "3d": 3 * 86400, "7d": 7 * 86400}

COMANDOS += [
    {"name": "painel-ticket", "description": "Postar neste canal o painel de abrir ticket",
     "default_member_permissions": str(PERM_GERENCIAR_SERVIDOR)},
    {"name": "sorteio", "description": "Criar um sorteio neste canal", "default_member_permissions": str(PERM_MODERAR),
     "options": [{"type": 3, "name": "premio", "description": "O que vai ser sorteado", "required": True, "max_length": 200},
                 {"type": 3, "name": "termina", "description": "Quando sai o resultado", "required": True,
                  "choices": [{"name": "No próximo horário do bot (00h, 12h ou 19h)", "value": "prox"},
                              {"name": "Em 1 dia", "value": "1d"}, {"name": "Em 3 dias", "value": "3d"},
                              {"name": "Em 7 dias", "value": "7d"}]},
                 {"type": 4, "name": "vencedores", "description": "Quantas pessoas ganham (padrão 1)",
                  "min_value": 1, "max_value": 20}]},
    {"name": "painel-cargos", "description": "Postar neste canal botões para pegar cargos",
     "default_member_permissions": str(PERM_GERENCIAR_CARGOS),
     "options": [{"type": 3, "name": "titulo", "description": "Título do painel", "required": True, "max_length": 100},
                 {"type": 8, "name": "cargo1", "description": "Cargo", "required": True}]
                + [{"type": 8, "name": f"cargo{i}", "description": "Cargo"} for i in range(2, 11)]
                + [{"type": 3, "name": "descricao", "description": "Texto do painel", "max_length": 1000}]},
    {"name": "painel-formulario", "description": "Postar neste canal o formulário de alistamento da crew",
     "default_member_permissions": str(PERM_GERENCIAR_SERVIDOR),
     "options": [{"type": 8, "name": "cargo", "description": "Cargo dado a quem for aprovado (opcional)"},
                 {"type": 3, "name": "titulo", "description": "Título do painel", "max_length": 100}]},
]


def norm_nome(s):
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower())


def cargos_staff(gid, minimo=1):
    """Cargos da hierarquia da staff a partir de um nível (1 = Helper)."""
    return [rid for n, _, rid in RANKS if n >= minimo]


def criar_canal_staff(gid, nome, topico, guia=None, motivo="Canal da staff do Kyzen Bot"):
    """Canal visível só para quem modera (admin, moderar, expulsar ou banir), na categoria da staff."""
    ow = [{"id": gid, "type": 0, "allow": "0", "deny": str(PERM_VER)}]
    ow += [{"id": r, "type": 0, "allow": str(PERM_VER | PERM_HIST), "deny": "0"} for r in cargos_staff(gid, 3)]
    corpo = {"name": nome, "type": 0, "permission_overwrites": ow, "topic": topico}
    for c in discord_req("GET", f"/guilds/{gid}/channels"):
        if c.get("type") == 4 and any(p in norm_nome(c.get("name")) for p in ("staff", "adm", "moder")):
            corpo["parent_id"] = c["id"]
            break
    canal = discord_req("POST", f"/guilds/{gid}/channels", corpo, motivo)
    if guia:
        msg = discord_req("POST", f"/channels/{canal['id']}/messages", {"embeds": [guia], "allowed_mentions": {"parse": []}})
        try:
            discord_req("PUT", f"/channels/{canal['id']}/pins/{msg['id']}", motivo="Guia da staff")
        except urllib.error.URLError:
            pass
    _canais_cache["t"] = 0
    return canal["id"]


def canal_staff(chave, criar=False):
    if _canais_staff.get(chave):
        return _canais_staff[chave]
    var, nome, busca, topico = CANAIS_STAFF[chave]
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    cid = os.environ.get(var, "").strip() or achar_canal(os.environ.get("DISCORD_BOT_TOKEN", "").strip(), gid, busca)
    if not cid and criar:
        cid = criar_canal_staff(gid, nome, topico)
    _canais_staff[chave] = cid
    return cid


def discord_arquivo(path, payload, nome_arq, conteudo):
    """Mensagem com um arquivo anexado (multipart)."""
    b = "kz" + secrets.token_hex(12)
    payload = dict(payload, attachments=[{"id": 0, "filename": nome_arq}])
    corpo = (f"--{b}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
             f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
             f"--{b}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{nome_arq}\"\r\n"
             f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode("utf-8") + conteudo + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(API + path, method="POST", data=corpo, headers={
        "Authorization": f"Bot {os.environ.get('DISCORD_BOT_TOKEN', '').strip()}",
        "User-Agent": "KyzenSA-Site (1.0)", "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def postar_painel(canal, corpo):
    discord_req("POST", f"/channels/{canal}/messages", dict(corpo, allowed_mentions={"parse": []}))
    return resposta(texto="✅ Painel postado neste canal.", privada=True, componentes=[])


def atualizar(embed, componentes=None):
    """Resposta que edita a mensagem do botão clicado."""
    return jsonify({"type": 7, "data": {"embeds": [embed], "components": componentes or [],
                                        "allowed_mentions": {"parse": []}}})


# ----- Tickets ---------------------------------------------------------------
def painel_ticket():
    return {"embeds": [{"title": "🎫 Central de atendimento da Kyzen", "color": COR,
                        "description": "Precisa falar com a staff? Escolha o assunto no botão abaixo.\n"
                                       "Um canal **privado** é criado só para você e a staff.\n\n"
                                       + "\n".join(f"{e} **{n}**" for e, n, _ in TICKETS.values()),
                        "footer": {"text": "Kyzen Bot · um ticket aberto por pessoa"}}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 4 if k == "denuncia" else 2 if k == "outro" else 1, "label": n,
                 "emoji": {"name": e}, "custom_id": f"tk:abrir:{k}"} for k, (e, n, _) in TICKETS.items()]}]}


def topico_ticket(uid, tipo):
    return f"Ticket de {uid} · {tipo} · aberto em <t:{int(time.time())}:f>"


def dono_ticket(topico):
    ach = re.match(r"^Ticket de (\d{5,25}) · (\w+)", topico or "")
    return (ach.group(1), ach.group(2)) if ach else ("", "")


def abrir_ticket(d, uid, tipo):
    gid = str(d.get("guild_id"))
    if tipo not in TICKETS:
        return resposta(texto="Assunto inválido.", privada=True, componentes=[])
    for c in discord_req("GET", f"/guilds/{gid}/channels"):
        if dono_ticket(c.get("topic"))[0] == uid:
            return resposta(texto=f"Você já tem um ticket aberto: <#{c['id']}>", privada=True, componentes=[])
    usuario = (d.get("member") or {}).get("user") or {}
    e, nome, instr = TICKETS[tipo]
    vis = PERM_VER | PERM_ENVIAR | PERM_HIST | PERM_ANEXOS | PERM_LINKS
    ow = [{"id": gid, "type": 0, "allow": "0", "deny": str(PERM_VER)},
          {"id": uid, "type": 1, "allow": str(vis), "deny": "0"}]
    ow += [{"id": r, "type": 0, "allow": str(vis), "deny": "0"} for r in cargos_staff(gid)]
    corpo = {"name": f"{tipo}-{slug_py(usuario.get('username') or uid)[:40] or uid}", "type": 0,
             "topic": topico_ticket(uid, tipo), "permission_overwrites": ow}
    pai = (d.get("channel") or {}).get("parent_id")
    if pai:
        corpo["parent_id"] = pai
    canal = discord_req("POST", f"/guilds/{gid}/channels", corpo, f"Ticket de {uid}")
    discord_req("POST", f"/channels/{canal['id']}/messages", {
        "content": f"<@{uid}>",
        "embeds": [{"title": f"{e} {nome}", "color": COR,
                    "description": f"Olá, <@{uid}>! {instr}\n\nQuando terminar, clique em **Fechar ticket**.",
                    "footer": {"text": "Kyzen Bot · só você e a staff veem este canal"}}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 4, "label": "Fechar ticket", "emoji": {"name": "🔒"}, "custom_id": "tk:fechar"}]}],
        "allowed_mentions": {"users": [uid]}})
    return resposta(texto=f"✅ Seu ticket foi aberto: <#{canal['id']}>", privada=True, componentes=[])


def fechar_ticket(d, uid, confirmado):
    canal = discord_req("GET", f"/channels/{d.get('channel_id')}")
    dono, tipo = dono_ticket(canal.get("topic"))
    if not dono:
        return resposta(texto="Este canal não é um ticket.", privada=True, componentes=[])
    if dono != uid and nivel(d.get("member")) < 1:
        return resposta(texto="Só quem abriu o ticket ou a staff pode fechar.", privada=True, componentes=[])
    if not confirmado:
        return resposta(texto="Tem certeza? O canal será apagado (a staff guarda uma cópia da conversa).",
                        privada=True, componentes=[{"type": 1, "components": [
                            {"type": 2, "style": 4, "label": "Sim, fechar", "custom_id": "tk:sim"}]}])
    linhas, antes = [], ""
    for _ in range(5):
        lote = discord_req("GET", f"/channels/{canal['id']}/messages?limit=100" + (f"&before={antes}" if antes else ""))
        for m in lote:
            a = m.get("author") or {}
            quando = datetime.fromtimestamp(snowflake_ts(m["id"]), timezone.utc) - timedelta(hours=3)
            txt = m.get("content") or ""
            txt += "".join(f"\n    [anexo] {x.get('url')}" for x in m.get("attachments") or [])
            txt += "".join(f"\n    [embed] {x.get('title') or ''} {x.get('description') or ''}" for x in m.get("embeds") or [])
            linhas.append(f"[{quando:%d/%m %H:%M}] {a.get('global_name') or a.get('username')}: {txt}")
        if len(lote) < 100:
            break
        antes = lote[-1]["id"]
    copia = (f"Ticket #{canal.get('name')} · {tipo} · aberto por {dono} · fechado por {uid}\n\n"
             + "\n".join(reversed(linhas))).encode("utf-8")
    reg = canal_staff("bot")
    if reg:
        try:
            discord_arquivo(f"/channels/{reg}/messages", {"embeds": [{
                "title": f"🔒 Ticket fechado · {TICKETS.get(tipo, ('', tipo))[1]}", "color": 0x6C757D,
                "description": f"Aberto por <@{dono}> · fechado por <@{uid}>\nMensagens: {len(linhas)}",
                "footer": {"text": f"ticket · {dono}"}, "timestamp": datetime.now(timezone.utc).isoformat()}],
                "allowed_mentions": {"parse": []}}, f"ticket-{canal.get('name')}.txt", copia)
        except urllib.error.URLError:
            return resposta(texto="Não consegui salvar a cópia do ticket, então não apaguei. Tente de novo.",
                            privada=True, componentes=[])
    discord_req("DELETE", f"/channels/{canal['id']}", motivo=f"Ticket fechado por {uid}")
    return jsonify({"type": 6})


# ----- Sorteios --------------------------------------------------------------
def proximo_horario(ts):
    """Primeiro horário do bot (00h, 12h ou 19h de Brasília) a partir de ts."""
    h = int(ts // 3600) + (1 if ts % 3600 else 0)
    for _ in range(72):
        if h % 24 in HORARIOS_UTC:
            return h * 3600
        h += 1
    return int(ts)


def rodape_sorteio(canal, msg, fim, n, status):
    return f"sorteio · {canal} · {msg} · {fim} · {n} · {status}"


def ler_sorteio(texto):
    ach = re.match(r"^sorteio · (\d+) · (\d+) · (\d+) · (\d+) · (\w+)$", texto or "")
    return ach.groups() if ach else None


def criar_sorteio(d, uid, premio, termina, n):
    canal = str(d.get("channel_id"))
    fim = proximo_horario(time.time() + DURACOES.get(termina, 0))
    msg = discord_req("POST", f"/channels/{canal}/messages", {
        "embeds": [{"title": f"🎉 SORTEIO: {cortar(premio, 200)}", "color": 0xE91E63,
                    "description": f"Reaja com {EMOJI_SORTEIO} nesta mensagem para participar!\n\n"
                                   f"🏆 **Vencedores:** {n}\n⏰ **Resultado:** <t:{fim}:f> (<t:{fim}:R>)\n"
                                   f"👤 **Criado por:** <@{uid}>",
                    "footer": {"text": "Kyzen Bot · o resultado pode levar alguns minutos para sair"}}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Encerrar agora (staff)", "emoji": {"name": "🏁"}, "custom_id": "sr:fim"}]}],
        "allowed_mentions": {"parse": []}})
    discord_req("PUT", f"/channels/{canal}/messages/{msg['id']}/reactions/{urllib.parse.quote(EMOJI_SORTEIO)}/@me")
    reg = canal_staff("bot")
    discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
        "title": f"🎉 Sorteio: {cortar(premio, 200)}", "color": 0xE91E63,
        "description": f"https://discord.com/channels/{d.get('guild_id')}/{canal}/{msg['id']}\nCriado por <@{uid}>",
        "footer": {"text": rodape_sorteio(canal, msg["id"], fim, n, "aberto")}}], "allowed_mentions": {"parse": []}})
    return resposta(texto=f"✅ Sorteio criado! O resultado sai <t:{fim}:R>.", privada=True, componentes=[])


def sortear(canal, msg, n, por="", paginas=10):
    participantes, depois = [], ""
    for _ in range(paginas):
        lote = discord_req("GET", f"/channels/{canal}/messages/{msg}/reactions/{urllib.parse.quote(EMOJI_SORTEIO)}"
                                  f"?limit=100" + (f"&after={depois}" if depois else ""))
        participantes += [str(u["id"]) for u in lote if not u.get("bot")]
        if len(lote) < 100:
            break
        depois = lote[-1]["id"]
    ganhadores = secrets.SystemRandom().sample(participantes, min(int(n), len(participantes)))
    original = discord_req("GET", f"/channels/{canal}/messages/{msg}")
    e = (original.get("embeds") or [{}])[0]
    premio = (e.get("title") or "").replace("🎉 SORTEIO: ", "")
    e["title"] = f"🏁 SORTEIO ENCERRADO: {premio}"
    e["color"] = 0x6C757D
    e["description"] = (f"🏆 **Ganhador{'es' if len(ganhadores) > 1 else ''}:** "
                        + (", ".join(f"<@{g}>" for g in ganhadores) or "ninguém participou 😢")
                        + f"\n👥 **Participantes:** {len(participantes)}" + (f"\n🏁 Encerrado por <@{por}>" if por else ""))
    discord_req("PATCH", f"/channels/{canal}/messages/{msg}", {"embeds": [e], "components": []})
    discord_req("POST", f"/channels/{canal}/messages", {
        "content": (f"🎉 Parabéns {', '.join(f'<@{g}>' for g in ganhadores)}! "
                    f"{'Vocês ganharam' if len(ganhadores) > 1 else 'Você ganhou'} **{premio}**! "
                    "Fale com a staff para receber o prêmio.") if ganhadores else
                   f"O sorteio de **{premio}** terminou sem participantes. 😢",
        "message_reference": {"message_id": msg, "fail_if_not_exists": False},
        "allowed_mentions": {"users": ganhadores}})
    return ganhadores, len(participantes)


def registros_sorteio():
    reg = canal_staff("bot")
    if not reg:
        return []
    out = []
    for m in discord_req("GET", f"/channels/{reg}/messages?limit=100"):
        for e in m.get("embeds") or []:
            s = ler_sorteio((e.get("footer") or {}).get("text"))
            if s and (m.get("author") or {}).get("bot"):
                out.append((m, e, s))
    return out


def fechar_registro_sorteio(m, e, s, ganhadores, total):
    canal, msg, fim, n, _ = s
    e = dict(e, color=0x6C757D, footer={"text": rodape_sorteio(canal, msg, fim, n, "encerrado")})
    e["description"] = (e.get("description") or "") + \
        f"\n🏆 {', '.join(f'<@{g}>' for g in ganhadores) or 'sem participantes'} · 👥 {total}"
    discord_req("PATCH", f"/channels/{canal_staff('bot')}/messages/{m['id']}", {"embeds": [e]})


def processar_sorteios():
    """Chamado pelo cron: sorteia tudo que já passou do horário."""
    feitos = 0
    for m, e, s in registros_sorteio():
        if s[4] == "aberto" and int(s[2]) <= time.time() + 300:
            try:
                ganhadores, total = sortear(s[0], s[1], s[3])
            except urllib.error.HTTPError as err:
                if err.code != 404:
                    continue
                ganhadores, total = [], 0  # a mensagem do sorteio foi apagada
            fechar_registro_sorteio(m, e, s, ganhadores, total)
            feitos += 1
    return feitos


def encerrar_sorteio(d, uid):
    if nivel(d.get("member")) < 6:
        return resposta(texto="Só Senior Mod ou acima pode encerrar o sorteio. Para participar, reaja com 🎉.",
                        privada=True, componentes=[])
    msg = str((d.get("message") or {}).get("id"))
    for m, e, s in registros_sorteio():
        if s[1] == msg and s[4] == "aberto":
            ganhadores, total = sortear(s[0], s[1], s[3], por=uid, paginas=3)
            fechar_registro_sorteio(m, e, s, ganhadores, total)
            return resposta(texto=f"🏁 Sorteio encerrado com {total} participantes.", privada=True, componentes=[])
    return resposta(texto="Não achei esse sorteio no #registro-bot (ou ele já foi encerrado).",
                    privada=True, componentes=[])


# ----- Cargos por botão ------------------------------------------------------
def painel_cargos(dados, opcoes):
    roles = (dados.get("resolved") or {}).get("roles") or {}
    escolhidos, recusados = [], []
    for i in range(1, 11):
        rid = str(opcoes.get(f"cargo{i}") or "")
        r = roles.get(rid)
        if not r or rid in [x["id"] for x in escolhidos]:
            continue
        if r.get("managed") or int(r.get("permissions") or 0) & PERIGOSAS or r.get("name") == "@everyone":
            recusados.append(r.get("name"))
        else:
            escolhidos.append(r)
    if recusados:
        return None, ("Esses cargos têm permissões de staff ou são de bots e não podem ir num painel: "
                      + ", ".join(recusados))
    linhas = [{"type": 1, "components": [
        {"type": 2, "style": 2, "label": cortar(r["name"], 80), "custom_id": f"cg:{r['id']}"}
        for r in escolhidos[i:i + 5]]} for i in range(0, len(escolhidos), 5)]
    return {"embeds": [{"title": cortar(str(opcoes.get("titulo")), 100), "color": COR,
                        "description": (str(opcoes.get("descricao") or "Clique para pegar ou tirar um cargo.")
                                        + "\n\n" + "\n".join(f"• <@&{r['id']}>" for r in escolhidos)),
                        "footer": {"text": "Kyzen Bot · clique de novo para tirar o cargo"}}],
            "components": linhas}, ""


def alternar_cargo(d, uid, rid):
    gid = str(d.get("guild_id"))
    tem = rid in ((d.get("member") or {}).get("roles") or [])
    try:
        discord_req("DELETE" if tem else "PUT", f"/guilds/{gid}/members/{uid}/roles/{rid}", motivo="Painel de cargos")
    except urllib.error.HTTPError as e:
        return resposta(texto="Não consegui mexer nesse cargo (ele está acima do meu ou foi apagado). Avise a staff."
                        if e.code in (403, 404) else "O Discord recusou agora. Tente de novo.", privada=True, componentes=[])
    return resposta(texto=f"{'➖ Tirei' if tem else '✅ Você ganhou'} o cargo <@&{rid}>.", privada=True, componentes=[])


# ----- Formulário da crew ----------------------------------------------------
PERGUNTAS = [
    ("nick", "Seu nick no Roblox", 1, True, 40),
    ("nivel", "Seu nível no Vox Seas", 1, True, 10),
    ("horario", "Que dias e horários você costuma jogar?", 1, True, 100),
    ("porque", "Por que você quer entrar na crew da Kyzen?", 2, True, 600),
    ("extra", "Já foi de outra crew? Algo mais pra contar?", 2, False, 600),
]


def painel_formulario(opcoes):
    rid = str(opcoes.get("cargo") or "0")
    return {"embeds": [{"title": cortar(str(opcoes.get("titulo") or "⚔️ Alistamento da Crew Kyzen"), 100), "color": COR,
                        "description": "Quer navegar com a gente no Vox Seas? É rapidinho:\n\n"
                                       "**1.** Clique em **Me alistar**\n**2.** Responda as 5 perguntas\n"
                                       "**3.** A staff analisa e você recebe a resposta na **DM**\n\n"
                                       "⚠️ Deixe a sua **DM aberta** para receber a resposta.",
                        "footer": {"text": "Kyzen Bot · um formulário por vez"}}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Me alistar", "emoji": {"name": "📝"}, "custom_id": f"fm:abrir:{rid}"}]}]}


def modal_formulario(rid):
    return jsonify({"type": 9, "data": {"custom_id": f"fm:env:{rid}", "title": "Alistamento da Crew Kyzen",
                                        "components": [{"type": 1, "components": [
                                            {"type": 4, "custom_id": c, "label": l, "style": st, "required": req,
                                             "max_length": mx}]} for c, l, st, req, mx in PERGUNTAS]}})


def enviar_formulario(d, uid, rid, valores):
    forms = canal_staff("forms")
    if not forms:
        return resposta(texto="O formulário ainda não está configurado. Avise a staff.", privada=True, componentes=[])
    for m in discord_req("GET", f"/channels/{forms}/messages?limit=100"):
        if any((e.get("footer") or {}).get("text") == f"Formulário de {uid} · pendente" for e in m.get("embeds") or []):
            return resposta(texto="Você já tem um formulário em análise. Aguarde a resposta na DM. 🙂",
                            privada=True, componentes=[])
    usuario = (d.get("member") or {}).get("user") or {}
    criado = int(snowflake_ts(uid))
    discord_req("POST", f"/channels/{forms}/messages", {"embeds": [{
        "title": f"📝 Novo alistamento: {cortar(valores.get('nick') or '?', 60)}", "color": 0x3498DB,
        "description": f"<@{uid}> · {usuario.get('username', '')} · conta criada <t:{criado}:R>",
        "fields": [{"name": l, "value": cortar(valores.get(c) or "—", 1000), "inline": st == 1}
                   for c, l, st, _, _ in PERGUNTAS],
        "footer": {"text": f"Formulário de {uid} · pendente"},
        "timestamp": datetime.now(timezone.utc).isoformat()}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Aprovar", "emoji": {"name": "✅"}, "custom_id": f"fm:ok:{uid}:{rid}"},
            {"type": 2, "style": 4, "label": "Recusar", "emoji": {"name": "❌"}, "custom_id": f"fm:no:{uid}:{rid}"}]}],
        "allowed_mentions": {"parse": []}})
    return resposta(texto="✅ Formulário enviado! A staff vai analisar e você recebe a resposta na **DM**. "
                          "Deixe a DM aberta.", privada=True, componentes=[])


def avisar_dm(uid, embed):
    try:
        dm = discord_req("POST", "/users/@me/channels", {"recipient_id": uid})
        discord_req("POST", f"/channels/{dm['id']}/messages", {"embeds": [embed], "allowed_mentions": {"parse": []}})
        return True
    except (urllib.error.URLError, KeyError, ValueError):
        return False


def decidir_formulario(d, staff, alvo, rid, aprovado):
    if nivel(d.get("member")) < 1:
        return resposta(texto="Só a staff pode aprovar ou recusar.", privada=True, componentes=[])
    e = dict(((d.get("message") or {}).get("embeds") or [{}])[0])
    nota = ""
    if aprovado and rid != "0":
        try:
            discord_req("PUT", f"/guilds/{d.get('guild_id')}/members/{alvo}/roles/{rid}", motivo=f"Aprovado por {staff}")
            nota = f" · cargo <@&{rid}> dado"
        except urllib.error.HTTPError as err:
            nota = " · ⚠️ não consegui dar o cargo" + (" (a pessoa saiu do servidor)" if err.code == 404 else "")
    dm = avisar_dm(alvo, {
        "title": "✅ Você foi aprovado na crew da Kyzen!" if aprovado else "Seu alistamento na crew não foi aprovado",
        "color": 0x28A745 if aprovado else 0xD9534F,
        "description": "Bem-vindo à tripulação! 🏴‍☠️ A staff vai te passar os próximos passos." if aprovado else
                       "Dessa vez não deu, mas você pode tentar de novo mais pra frente. "
                       "Se quiser saber o motivo, abra um ticket. 🤝"})
    e["color"] = 0x28A745 if aprovado else 0xD9534F
    e["footer"] = {"text": f"Formulário de {alvo} · {'aprovado' if aprovado else 'recusado'}"}
    e["fields"] = list(e.get("fields") or []) + [{
        "name": "✅ Aprovado" if aprovado else "❌ Recusado",
        "value": f"por <@{staff}>{nota}" + ("" if dm else " · 📪 DM fechada, avise a pessoa"), "inline": False}]
    return atualizar(e)


# ----- Roteador --------------------------------------------------------------
def interacao_paineis(d, tipo, dados, nome, opcoes, uid):
    """Tickets, sorteios, cargos e formulário. Devolve None se não for um deles."""
    membro = d.get("member") or {}
    if tipo == 2 and nome in ("painel-ticket", "painel-cargos", "painel-formulario", "sorteio"):
        req = 6 if nome == "sorteio" else 7
        if nivel(membro) < req:
            return so_a_partir(req)
        canal = str(d.get("channel_id"))
        if nome == "painel-ticket":
            return postar_painel(canal, painel_atendimento())
        if nome == "painel-formulario":
            if not canal_staff("forms", criar=True):
                return resposta(texto="Não consegui criar o #formulários-crew.", privada=True, componentes=[])
            return postar_painel(canal, painel_formularios())
        if nome == "painel-cargos":
            corpo, erro = painel_cargos(dados, opcoes)
            return resposta(texto=erro, privada=True, componentes=[]) if erro else postar_painel(canal, corpo)
        if not canal_staff("bot", criar=True):
            return resposta(texto="Não consegui criar o #registro-bot.", privada=True, componentes=[])
        return criar_sorteio(d, uid, str(opcoes.get("premio") or "Prêmio"), str(opcoes.get("termina") or "prox"),
                             int(opcoes.get("vencedores") or 1))
    cid = str(dados.get("custom_id") or "")
    if tipo == 3:
        if cid.startswith("tk:abrir:"):
            return abrir_ticket(d, uid, cid.split(":")[2])
        if cid in ("tk:fechar", "tk:sim"):
            return fechar_ticket(d, uid, cid == "tk:sim")
        if cid == "sr:fim":
            return encerrar_sorteio(d, uid)
        if re.fullmatch(r"cg:\d{5,25}", cid):
            return alternar_cargo(d, uid, cid[3:])
        if re.fullmatch(r"fm:abrir:\d{1,25}", cid):
            return modal_formulario(cid.split(":")[2])
        ach = re.fullmatch(r"fm:(ok|no):(\d{5,25}):(\d{1,25})", cid)
        if ach:
            return decidir_formulario(d, uid, ach.group(2), ach.group(3), ach.group(1) == "ok")
    if tipo == 5 and re.fullmatch(r"fm:env:\d{1,25}", cid):
        valores = {c.get("custom_id"): (c.get("value") or "").strip()
                   for linha in dados.get("components") or [] for c in linha.get("components") or []}
        return enviar_formulario(d, uid, cid.split(":")[2], valores)
    return None


# ---------------------------------------------------------------------------
# Kyzen Bot: sugestões com votação, escala de war, contadores e avisos agendados
# ---------------------------------------------------------------------------
PERM_EVENTOS = 1 << 33
CANAL_SUGESTOES = "1384921966254882958"
CONTADORES = {"CANAL_CONTADOR_MEMBROS": ("1398765028307505323", "approximate_member_count"),
              "CANAL_CONTADOR_BOOSTS": ("1398765040315793550", "premium_subscription_count")}
BRT = timezone(timedelta(hours=-3))
STATUS_SUGESTAO = {"ok": ("✅ Aprovada", 0x28A745), "no": ("❌ Recusada", 0xD9534F), "feita": ("🚀 Já foi feita", 0x8E44AD)}
WAR_OPCOES = (("sim", "✅ Vão", "Vou", 3), ("talvez", "🤔 Talvez", "Talvez", 2), ("nao", "❌ Não vão", "Não vou", 4))

COMANDOS += [
    {"name": "sugerir", "description": "Mandar uma sugestão para o servidor (a galera vota)",
     "options": [{"type": 3, "name": "sugestao", "description": "Sua ideia", "required": True,
                  "min_length": 10, "max_length": 1000}]},
    {"name": "war", "description": "Marcar uma war e pedir confirmação da crew",
     "default_member_permissions": str(PERM_EVENTOS),
     "options": [{"type": 3, "name": "titulo", "description": "Ex.: War contra a crew X", "required": True,
                  "max_length": 100},
                 {"type": 3, "name": "dia", "description": "Dia, ex.: 25/11", "required": True, "max_length": 10},
                 {"type": 3, "name": "hora", "description": "Hora de Brasília, ex.: 20:30", "required": True,
                  "max_length": 5},
                 {"type": 3, "name": "detalhes", "description": "Ponto de encontro, regras etc.", "max_length": 800}]},
    {"name": "agendar", "description": "Agendar um aviso para sair às 00h, 12h ou 19h",
     "default_member_permissions": str(PERM_GERENCIAR_SERVIDOR),
     "options": [{"type": 7, "name": "canal", "description": "Onde postar", "required": True, "channel_types": [0, 5]},
                 {"type": 3, "name": "mensagem", "description": "O texto do aviso", "required": True, "max_length": 2000},
                 {"type": 3, "name": "horario", "description": "Horário de Brasília", "required": True,
                  "choices": [{"name": "00h", "value": "0"}, {"name": "12h", "value": "12"},
                              {"name": "19h", "value": "19"}]},
                 {"type": 3, "name": "dia", "description": "Dia, ex.: 25/11 (vazio = o próximo)", "max_length": 10},
                 {"type": 3, "name": "marcar", "description": "Marcar alguém junto",
                  "choices": [{"name": "Ninguém", "value": "nada"}, {"name": "@everyone", "value": "everyone"},
                              {"name": "@here", "value": "here"}]}]},
]


def ler_data(dia, hora="00:00"):
    """'25/11' + '20:30' (Brasília) -> timestamp; None se for inválido."""
    a = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\s*", dia or "")
    h = re.fullmatch(r"\s*(\d{1,2})(?:[:h](\d{2}))?h?\s*", hora or "")
    if not a or not h:
        return None
    agora = datetime.now(BRT)
    ano = int(a.group(3)) if a.group(3) else agora.year
    ano += 2000 if ano < 100 else 0
    try:
        quando = datetime(ano, int(a.group(2)), int(a.group(1)), int(h.group(1)), int(h.group(2) or 0), tzinfo=BRT)
    except ValueError:
        return None
    if not a.group(3) and quando < agora - timedelta(days=1):
        quando = quando.replace(year=ano + 1)
    return int(quando.timestamp())


def registros_bot(prefixo):
    reg = canal_staff("bot")
    if not reg:
        return []
    out = []
    for m in discord_req("GET", f"/channels/{reg}/messages?limit=100"):
        if not (m.get("author") or {}).get("bot"):
            continue
        for e in m.get("embeds") or []:
            partes = ((e.get("footer") or {}).get("text") or "").split(" · ")
            if partes[0] == prefixo:
                out.append((m, e, partes))
    return out


def marcar_registro(m, e, partes, status, extra=""):
    e = dict(e, footer={"text": " · ".join(partes[:-1] + [status])}, color=0x6C757D)
    if extra:
        e["description"] = (e.get("description") or "") + "\n" + extra
    discord_req("PATCH", f"/channels/{canal_staff('bot')}/messages/{m['id']}", {"embeds": [e], "components": []})


# ----- Sugestões -------------------------------------------------------------
def cmd_sugerir(d, uid, texto):
    canal = os.environ.get("CANAL_SUGESTOES", CANAL_SUGESTOES).strip()
    u = (d.get("member") or {}).get("user") or {}
    nome = u.get("global_name") or u.get("username") or "membro"
    msg = discord_req("POST", f"/channels/{canal}/messages", {
        "embeds": [{"author": {"name": f"Sugestão de {nome}"}, "color": 0xF1C40F,
                    "description": cortar(texto, 1000) + "\n\nVote com 👍 ou 👎 e comente no tópico abaixo.",
                    "footer": {"text": f"Sugestão de {uid} · em votação"},
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Aprovar", "custom_id": "sg:ok"},
            {"type": 2, "style": 4, "label": "Recusar", "custom_id": "sg:no"},
            {"type": 2, "style": 2, "label": "Já foi feita", "custom_id": "sg:feita"}]}],
        "allowed_mentions": {"parse": []}})
    for emo in ("👍", "👎"):
        try:
            discord_req("PUT", f"/channels/{canal}/messages/{msg['id']}/reactions/{urllib.parse.quote(emo)}/@me")
        except urllib.error.URLError:
            pass
    try:
        discord_req("POST", f"/channels/{canal}/messages/{msg['id']}/threads",
                    {"name": cortar(f"💬 {texto}", 90), "auto_archive_duration": 4320})
    except urllib.error.URLError:
        pass
    gid = d.get("guild_id")
    return resposta(texto=f"✅ Sugestão enviada! https://discord.com/channels/{gid}/{canal}/{msg['id']}",
                    privada=True, componentes=[])


def status_sugestao(d, uid, chave):
    if nivel(d.get("member")) < 4:
        return resposta(texto="Só a staff muda o status. Para votar, use 👍 ou 👎.", privada=True, componentes=[])
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    rotulo, cor = STATUS_SUGESTAO[chave]
    autor = re.match(r"^Sugestão de (\d{5,25})", (e.get("footer") or {}).get("text") or "")
    e["color"] = cor
    e["footer"] = {"text": f"Sugestão de {autor.group(1) if autor else '0'} · {rotulo}"}
    e["fields"] = [f for f in e.get("fields") or [] if f.get("name") != "Status"] + \
                  [{"name": "Status", "value": f"{rotulo} por <@{uid}>", "inline": False}]
    if autor:
        avisar_dm(autor.group(1), {"title": f"Sua sugestão na Kyzen: {rotulo}", "color": cor,
                                   "description": cortar((e.get("description") or "").split("\n\nVote com")[0], 500)})
    return atualizar(e, msg.get("components") or [])


# ----- War -------------------------------------------------------------------
def cmd_war(d, uid, opcoes):
    ts = ler_data(str(opcoes.get("dia") or ""), str(opcoes.get("hora") or ""))
    if not ts:
        return resposta(texto="Não entendi a data. Use o dia como **25/11** e a hora como **20:30**.",
                        privada=True, componentes=[])
    if ts < time.time():
        return resposta(texto="Essa data já passou.", privada=True, componentes=[])
    canal = str(d.get("channel_id"))
    detalhes = str(opcoes.get("detalhes") or "").strip()
    msg = discord_req("POST", f"/channels/{canal}/messages", {
        "embeds": [{"title": f"⚔️ {cortar(str(opcoes.get('titulo')), 100)}", "color": 0xC0392B,
                    "description": f"📅 **<t:{ts}:F>** (<t:{ts}:R>)\n" + (f"\n{detalhes}\n" if detalhes else "")
                                   + "\nConfirme a sua presença nos botões abaixo. O bot avisa quem confirmou antes de começar.",
                    "fields": [{"name": f"{n} (0)", "value": "—", "inline": True} for _, n, _, _ in WAR_OPCOES],
                    "footer": {"text": f"Marcada por {uid} · Kyzen Bot"}}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": st, "label": lb, "custom_id": f"wr:{k}"} for k, _, lb, st in WAR_OPCOES]}],
        "allowed_mentions": {"parse": []}})
    reg = canal_staff("bot", criar=True)
    discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
        "title": f"⚔️ War: {cortar(str(opcoes.get('titulo')), 100)}", "color": 0xC0392B,
        "description": f"https://discord.com/channels/{d.get('guild_id')}/{canal}/{msg['id']}\n<t:{ts}:F> · marcada por <@{uid}>",
        "footer": {"text": f"war · {canal} · {msg['id']} · {ts} · aberta"}}], "allowed_mentions": {"parse": []}})
    return resposta(texto="✅ War marcada! O bot lembra quem confirmou no último horário dele (00h, 12h ou 19h) "
                          "antes de começar.", privada=True, componentes=[])


def listas_war(e):
    out = {}
    for (k, _, _, _), f in zip(WAR_OPCOES, (e.get("fields") or [])[:3]):
        out[k] = re.findall(r"<@(\d{5,25})>", f.get("value") or "")
    return out


def responder_war(d, uid, escolha):
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    listas = listas_war(e)
    ja = uid in listas.get(escolha, [])
    for k in listas:
        listas[k] = [x for x in listas[k] if x != uid]
    if not ja:
        listas.setdefault(escolha, []).append(uid)
    campos = []
    for k, n, _, _ in WAR_OPCOES:
        gente = listas.get(k, [])
        valor = " ".join(f"<@{x}>" for x in gente) or "—"
        campos.append({"name": f"{n} ({len(gente)})", "value": cortar(valor, 1024), "inline": True})
    e["fields"] = campos + list(e.get("fields") or [])[3:]
    return atualizar(e, msg.get("components") or [])


def lembrar_wars(agora):
    feitos = 0
    for m, e, p in registros_bot("war"):
        if len(p) != 5 or p[4] != "aberta":
            continue
        canal, msg, ts = p[1], p[2], int(p[3])
        if ts < agora:
            marcar_registro(m, e, p, "passou")
            continue
        if ts - agora > 12.5 * 3600:
            continue
        try:
            war = discord_req("GET", f"/channels/{canal}/messages/{msg}")
        except urllib.error.HTTPError:
            marcar_registro(m, e, p, "apagada")
            continue
        ew = (war.get("embeds") or [{}])[0]
        listas = listas_war(ew)
        gente = listas.get("sim", []) + listas.get("talvez", [])
        discord_req("POST", f"/channels/{canal}/messages", {
            "content": f"⚔️ **{(ew.get('title') or 'War').lstrip('⚔️ ')}** começa <t:{ts}:R> (<t:{ts}:t>)!\n"
                       + (" ".join(f"<@{x}>" for x in gente) if gente else "Ninguém confirmou ainda. Bora, tripulação!"),
            "message_reference": {"message_id": msg, "fail_if_not_exists": False},
            "allowed_mentions": {"users": gente[:100]}})
        marcar_registro(m, e, p, "avisada", f"🔔 Avisados: {len(gente)}")
        feitos += 1
    return feitos


# ----- Avisos agendados ------------------------------------------------------
def cmd_agendar(d, uid, opcoes, dados):
    canal = str(opcoes.get("canal") or "")
    hora = int(opcoes.get("horario") or 19)
    dia = str(opcoes.get("dia") or "").strip()
    if dia:
        ts = ler_data(dia, f"{hora}:00")
        if not ts:
            return resposta(texto="Não entendi o dia. Use, por exemplo, **25/11**.", privada=True, componentes=[])
    else:
        hoje = datetime.now(BRT).replace(hour=hora, minute=0, second=0, microsecond=0)
        ts = int((hoje if hoje.timestamp() > time.time() else hoje + timedelta(days=1)).timestamp())
    if ts <= time.time():
        return resposta(texto="Esse horário já passou.", privada=True, componentes=[])
    marcar = str(opcoes.get("marcar") or "nada")
    reg = canal_staff("bot", criar=True)
    discord_req("POST", f"/channels/{reg}/messages", {
        "embeds": [{"title": "📢 Aviso agendado", "color": 0x2980B9, "description": str(opcoes.get("mensagem")),
                    "fields": [{"name": "Canal", "value": f"<#{canal}>", "inline": True},
                               {"name": "Quando", "value": f"<t:{ts}:F>", "inline": True},
                               {"name": "Marca", "value": {"nada": "ninguém"}.get(marcar, f"@{marcar}"), "inline": True},
                               {"name": "Por", "value": f"<@{uid}>", "inline": True}],
                    "footer": {"text": f"aviso · {canal} · {ts} · {marcar} · agendado"}}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 4, "label": "Cancelar aviso", "custom_id": "av:cancelar"}]}],
        "allowed_mentions": {"parse": []}})
    return resposta(texto=f"✅ Aviso agendado para <t:{ts}:F> em <#{canal}>. Para cancelar, use o botão no "
                          f"<#{reg}>.", privada=True, componentes=[])


def enviar_avisos(agora):
    feitos = 0
    for m, e, p in registros_bot("aviso"):
        if len(p) != 5 or p[4] != "agendado" or int(p[2]) > agora + 300:
            continue
        canal, marcar = p[1], p[3]
        texto = ({"everyone": "@everyone\n", "here": "@here\n"}.get(marcar, "")) + (e.get("description") or "")
        try:
            discord_req("POST", f"/channels/{canal}/messages", {
                "content": texto[:2000],
                "allowed_mentions": {"parse": ["everyone"] if marcar in ("everyone", "here") else []}})
            marcar_registro(m, e, p, "enviado")
            feitos += 1
        except urllib.error.HTTPError as err:
            marcar_registro(m, e, p, "falhou", f"❌ O Discord recusou ({err.code})")
    return feitos


def cancelar_aviso(d, uid):
    if nivel(d.get("member")) < 7:
        return resposta(texto="Só Adm ou acima pode cancelar.", privada=True, componentes=[])
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    partes = ((e.get("footer") or {}).get("text") or "").split(" · ")
    if partes[0] != "aviso" or partes[-1] != "agendado":
        return resposta(texto="Esse aviso já saiu ou foi cancelado.", privada=True, componentes=[])
    e["footer"] = {"text": " · ".join(partes[:-1] + ["cancelado"])}
    e["color"] = 0x6C757D
    e["title"] = "📢 Aviso cancelado"
    e["fields"] = list(e.get("fields") or []) + [{"name": "Cancelado por", "value": f"<@{uid}>", "inline": True}]
    return atualizar(e)


# ----- Contadores ------------------------------------------------------------
def atualizar_contadores():
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    info = discord_req("GET", f"/guilds/{gid}?with_counts=true")
    mudou = 0
    for var, (padrao, campo_info) in CONTADORES.items():
        cid = os.environ.get(var, padrao).strip()
        valor = info.get(campo_info)
        if not cid or valor is None:
            continue
        try:
            nome = discord_req("GET", f"/channels/{cid}").get("name") or ""
            novo = re.sub(r"\d[\d.,]*", str(int(valor)), nome, count=1)
            if novo != nome:
                discord_req("PATCH", f"/channels/{cid}", {"name": novo}, "Contador do Kyzen Bot")
                mudou += 1
        except urllib.error.URLError:
            pass
    return mudou


def processar_agenda():
    """Cron (00h, 12h e 19h): sorteios, lembretes de war, avisos agendados e contadores."""
    agora = time.time()
    out = {}
    for nome, f in (("sorteios", processar_sorteios), ("wars", lambda: lembrar_wars(agora)),
                    ("avisos", lambda: enviar_avisos(agora)), ("contadores", atualizar_contadores), ("relatorio", relatorio_semanal),
                    ("convites", processar_convites)):
        try:
            out[nome] = f()
        except Exception as e:  # noqa: BLE001  (uma parte não pode travar as outras)
            out[nome] = f"erro: {str(e)[:100]}"
    return out


def interacao_extras(d, tipo, dados, nome, opcoes, uid):
    membro = d.get("member") or {}
    if tipo == 2 and nome == "sugerir":
        return cmd_sugerir(d, uid, str(opcoes.get("sugestao") or ""))
    if tipo == 2 and nome == "war":
        if nivel(membro) < 6:
            return resposta(texto="Só quem organiza eventos pode marcar war.", privada=True, componentes=[])
        return cmd_war(d, uid, opcoes)
    if tipo == 2 and nome == "agendar":
        if nivel(membro) < 7:
            return resposta(texto="Só Adm ou acima pode agendar avisos.", privada=True, componentes=[])
        return cmd_agendar(d, uid, opcoes, dados)
    cid = str(dados.get("custom_id") or "")
    if tipo == 3:
        if cid.startswith("sg:") and cid[3:] in STATUS_SUGESTAO:
            return status_sugestao(d, uid, cid[3:])
        if cid in ("wr:sim", "wr:talvez", "wr:nao"):
            return responder_war(d, uid, cid[3:])
        if cid == "av:cancelar":
            return cancelar_aviso(d, uid)
    return None


# ---------------------------------------------------------------------------
# Hierarquia da staff: cada cargo tem os seus poderes no Kyzen Bot
#   Quem precisa de algo acima do próprio nível manda um pedido de aprovação.
# ---------------------------------------------------------------------------
PERM_AUDITORIA = 1 << 7
RANKS = [  # (nível, nome, ID do cargo)
    (1, "Helper", "1497068096450465792"),
    (2, "Junior Staff", "1504676074641162240"),
    (3, "Staff", "1384722554064867328"),
    (4, "Senior Staff", "1504678116285485137"),
    (5, "Mod", "1383961426531717242"),
    (6, "Senior Mod", "1504677084532965456"),
    (7, "Adm", "1383960212641157201"),
    (8, "Head Admin", "1504675809015890072"),
    (9, "Dev", "1388595988159271063"),
    (9, "/k", "1407860349935292506"),
    (9, "Co-Owner", "1383990976728535091"),
    (9, "Owner", "1383960421932863618"),
]
NIVEL_DO_CARGO = {rid: n for n, _, rid in RANKS}
NOME_NIVEL = {1: "Helper", 2: "Junior Staff", 3: "Staff", 4: "Senior Staff", 5: "Mod", 6: "Senior Mod",
              7: "Adm", 8: "Head Admin", 9: "Owner"}
WARN_REQ = {0: 1, 1: 2, 2: 2, 3: 3, 4: 4, 5: 5}          # nº do warn (0 = aviso verbal) -> nível mínimo
LIMPAR_MAX = {2: 20, 5: 100}                              # nível -> mensagens
CASTIGO_MAX = {3: 3600, 4: 86400, 5: 7 * 86400, 7: 28 * 86400}
DURACAO_NOME = {600: "10 minutos", 3600: "1 hora", 86400: "1 dia", 604800: "7 dias", 2419200: "28 dias"}
TRAVAR = PERM_ENVIAR | (1 << 6) | (1 << 35) | (1 << 36) | (1 << 38)   # enviar, reagir, criar e usar tópicos

COMANDOS += [
    {"name": "limpar", "description": "Apagar mensagens deste canal (Junior Staff: até 20 · Mod: até 100)",
     "options": [{"type": 4, "name": "quantidade", "description": "Quantas mensagens", "required": True,
                  "min_value": 1, "max_value": 100},
                 {"type": 6, "name": "membro", "description": "Só as mensagens desta pessoa"}]},
    {"name": "lento", "description": "Modo lento neste canal (Staff+)",
     "options": [{"type": 4, "name": "segundos", "description": "Tempo entre mensagens", "required": True,
                  "choices": [{"name": "Desligar", "value": 0}, {"name": "5 segundos", "value": 5},
                              {"name": "10 segundos", "value": 10}, {"name": "30 segundos", "value": 30},
                              {"name": "1 minuto", "value": 60}, {"name": "5 minutos", "value": 300}]}]},
    {"name": "castigo", "description": "Mutar alguém por um tempo, fora da tabela de warns (Staff+)",
     "options": [{"type": 6, "name": "membro", "description": "Quem", "required": True},
                 {"type": 4, "name": "duracao", "description": "Por quanto tempo", "required": True,
                  "choices": [{"name": n, "value": s} for s, n in DURACAO_NOME.items()]},
                 {"type": 3, "name": "motivo", "description": "Por quê", "required": True, "max_length": 500}]},
    {"name": "expulsar", "description": "Expulsar alguém do servidor (Mod+)",
     "options": [{"type": 6, "name": "membro", "description": "Quem", "required": True},
                 {"type": 3, "name": "motivo", "description": "Por quê", "required": True, "max_length": 500}]},
    {"name": "banir", "description": "Banir alguém direto, para casos graves (Mod+)",
     "options": [{"type": 6, "name": "membro", "description": "Quem", "required": True},
                 {"type": 3, "name": "motivo", "description": "Por quê", "required": True, "max_length": 500},
                 {"type": 4, "name": "apagar", "description": "Apagar as mensagens recentes da pessoa",
                  "choices": [{"name": "Não apagar", "value": 0}, {"name": "Último dia", "value": 86400},
                              {"name": "Últimos 7 dias", "value": 604800}]}]},
    {"name": "desbanir", "description": "Tirar o ban de alguém (Adm+)",
     "options": [{"type": 3, "name": "id", "description": "ID da pessoa", "required": True, "max_length": 25},
                 {"type": 3, "name": "motivo", "description": "Por quê", "max_length": 300}]},
    {"name": "trancar", "description": "Trancar este canal numa emergência (Mod+)",
     "options": [{"type": 3, "name": "motivo", "description": "Por quê", "max_length": 300}]},
    {"name": "destrancar", "description": "Destrancar este canal (Mod+)"},
]

# Quem vê cada comando na lista do Discord (o bot ainda confere o nível de cada um)
_VISIVEL = {PERM_AUDITORIA: ("Advertir membro", "Advertir autor", "advertir", "historico", "perdoar", "limpar",
                             "lento", "castigo", "expulsar", "banir", "desbanir", "trancar", "destrancar"),
            PERM_GERENCIAR_SERVIDOR: ("painel-ticket", "painel-cargos", "painel-formulario", "agendar"),
            PERM_EVENTOS: ("sorteio", "war")}
for _bit, _nomes in _VISIVEL.items():
    for _c in COMANDOS:
        if _c["name"] in _nomes:
            _c["default_member_permissions"] = str(_bit)

# Tickets voltaram para o Apolo e os formulários para o Appy: esses painéis saem da lista de comandos.
# (O código continua aqui para fechar os tickets que o Kyzen Bot já abriu.)
COMANDOS[:] = [_c for _c in COMANDOS if _c["name"] not in ("painel-ticket", "painel-formulario")]


def nivel(membro):
    """Nível de staff de alguém (0 = membro comum), pelo cargo que a pessoa tem."""
    if not membro:
        return 0
    n = max([NIVEL_DO_CARGO.get(str(r), 0) for r in membro.get("roles") or []] + [0])
    if not n and int(membro.get("permissions") or 0) & PERM_ADMIN and not (membro.get("user") or {}).get("bot"):
        n = 9  # dono do servidor ou admin sem cargo da staff
    return n


def limite(tabela, n):
    vals = [v for k, v in tabela.items() if n >= k]
    return max(vals) if vals else 0


def so_a_partir(req):
    return resposta(texto=f"🔒 Isso é a partir de **{NOME_NIVEL[req]}**.", privada=True, componentes=[])


def log_acao(titulo, staff, tipo, campos, cor=0x5865F2):
    """Registra no #registro-punições uma ação que não é warn (castigo, ban, limpeza...)."""
    reg = canal_registro()
    if not reg:
        return
    try:
        discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
            "title": titulo, "color": cor,
            "fields": [{"name": "🛡️ Staff", "value": f"<@{staff}>", "inline": True}] +
                      [{"name": n, "value": cortar(str(v), 1000), "inline": len(str(v)) < 40} for n, v in campos if v],
            "footer": {"text": f"Ação · {staff} · {tipo}"},
            "timestamp": datetime.now(timezone.utc).isoformat()}], "allowed_mentions": {"parse": []}})
    except urllib.error.URLError:
        pass


def avisar_membro(alvo, titulo, texto, cor=0xD9534F):
    return avisar_dm(alvo, {"title": titulo, "color": cor,
                            "description": texto + f"\n\nSe achar que foi injusto, abra um ticket: {ticket_link()}"})


# ----- Pedido de aprovação ---------------------------------------------------
def pedir_aprovacao(staff, alvo, motivo, prova, verbal, n, punicao, req):
    reg = canal_registro()
    discord_req("POST", f"/channels/{reg}/messages", {
        "content": f"<@&{next(rid for k, _, rid in RANKS if k == req)}>",
        "embeds": [{"title": f"✋ Pedido de aprovação · {'Aviso verbal' if verbal else f'Advertência {n}/{LIMITE_BAN}'}",
                    "color": 0xF39C12,
                    "description": f"<@{staff}> quer advertir <@{alvo}>, mas a punição (**{punicao}**) precisa de "
                                   f"**{NOME_NIVEL[req]}** ou acima.",
                    "fields": [{"name": "👤 Membro", "value": f"<@{alvo}>", "inline": True},
                               {"name": "🙋 Pedido por", "value": f"<@{staff}>", "inline": True},
                               {"name": "⚖️ Punição", "value": punicao, "inline": True},
                               {"name": "📝 Motivo", "value": cortar(motivo, 1000), "inline": False}]
                              + ([{"name": "📎 Prova", "value": cortar(prova, 1000), "inline": False}] if prova else []),
                    "footer": {"text": f"Pedido · {alvo} · {req} · {'verbal' if verbal else 'warn'} · pendente"},
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Aprovar", "emoji": {"name": "✅"}, "custom_id": "ap:ok"},
            {"type": 2, "style": 4, "label": "Recusar", "emoji": {"name": "❌"}, "custom_id": "ap:no"}]}],
        "allowed_mentions": {"roles": [next(rid for k, _, rid in RANKS if k == req)]}})
    return resposta({"title": "✋ Pedido de aprovação enviado",
                     "description": f"Essa punição (**{punicao}**) é a partir de **{NOME_NIVEL[req]}**. "
                                    f"Mandei o pedido no <#{reg}> e quem for desse cargo pra cima aprova com um clique."},
                    privada=True, componentes=[])


def decidir_pedido(d, uid, aprovar):
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    partes = ((e.get("footer") or {}).get("text") or "").split(" · ")
    if len(partes) != 5 or partes[0] != "Pedido" or partes[4] != "pendente":
        return resposta(texto="Esse pedido já foi resolvido.", privada=True, componentes=[])
    alvo, req, tipo = partes[1], int(partes[2]), partes[3]
    if nivel(d.get("member")) < req:
        return so_a_partir(req)
    campos = {f.get("name", ""): f.get("value", "") for f in e.get("fields") or []}
    pedinte = re.sub(r"\D", "", campos.get("🙋 Pedido por", ""))
    if aprovar:
        gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", ""))
        advertir(gid, uid, alvo, alvo, campos.get("📝 Motivo", ""), campos.get("📎 Prova", ""), tipo == "verbal",
                 nivel_staff=9, pedido_por=pedinte)
    e["color"] = 0x28A745 if aprovar else 0x6C757D
    e["footer"] = {"text": " · ".join(partes[:4] + ["aprovado" if aprovar else "recusado"])}
    e["fields"] = list(e.get("fields") or []) + [{
        "name": "✅ Aprovado" if aprovar else "❌ Recusado", "value": f"por <@{uid}>", "inline": False}]
    if pedinte and not aprovar:
        avisar_dm(pedinte, {"title": "Seu pedido de advertência foi recusado", "color": 0x6C757D,
                            "description": f"<@{uid}> recusou o pedido para <@{alvo}>."})
    return atualizar(e)


# ----- Contestação (o membro punido pede revisão pela DM) --------------------
def modal_contestar(uid):
    return jsonify({"type": 9, "data": {"custom_id": f"ct:env:{uid}", "title": "Contestar punição", "components": [
        {"type": 1, "components": [{"type": 4, "custom_id": "texto", "style": 2, "required": True, "min_length": 20,
                                    "max_length": 1000, "label": "Por que a punição foi injusta?",
                                    "placeholder": "Explique o que aconteceu. Se tiver print, cole o link."}]}]}})


def enviar_contestacao(uid, texto):
    reg = canal_registro()
    for m in discord_req("GET", f"/channels/{reg}/messages?limit=100"):
        if any((e.get("footer") or {}).get("text") == f"Contestação · {uid} · pendente" for e in m.get("embeds") or []):
            return resposta(texto="Você já tem uma contestação em análise. Aguarde a resposta aqui na DM.",
                            privada=True, componentes=[])
    ultimo = next((r for r in registros(uid) if r["status"] == ST_WARN), None)
    discord_req("POST", f"/channels/{reg}/messages", {
        "embeds": [{"title": "📨 Contestação de punição", "color": 0x9B59B6,
                    "description": f"<@{uid}> pediu revisão da última advertência.",
                    "fields": [{"name": "💬 O que a pessoa disse", "value": cortar(texto, 1000), "inline": False}]
                              + ([{"name": "⚠️ Advertência contestada",
                                   "value": f"{ultimo['embed'].get('title')}: {cortar(campo(ultimo['embed'], 'Motivo'), 300)}"
                                            f" · por {campo(ultimo['embed'], 'Staff')}", "inline": False}] if ultimo else []),
                    "footer": {"text": f"Contestação · {uid} · pendente"},
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Aceitar (tirar a punição)", "custom_id": f"ct:ok:{uid}"},
            {"type": 2, "style": 2, "label": "Manter a punição", "custom_id": f"ct:no:{uid}"}]}],
        "allowed_mentions": {"parse": []}})
    return resposta(texto="📨 Contestação enviada! Um Senior Staff ou acima vai analisar e você recebe a resposta aqui.",
                    privada=True, componentes=[])


def decidir_contestacao(d, staff, alvo, aceitar):
    if nivel(d.get("member")) < 4:
        return so_a_partir(4)
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    if not ((e.get("footer") or {}).get("text") or "").endswith("pendente"):
        return resposta(texto="Essa contestação já foi resolvida.", privada=True, componentes=[])
    gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", ""))
    if aceitar:
        cmd_perdoar(gid, staff, alvo, "Contestação aceita")
        try:
            discord_req("DELETE", f"/guilds/{gid}/bans/{alvo}", motivo=f"Contestação aceita por {staff}")
        except urllib.error.URLError:
            pass  # não estava banido
    avisar_dm(alvo, {"title": "✅ Sua contestação foi aceita" if aceitar else "Sua contestação foi analisada",
                     "color": 0x28A745 if aceitar else 0x6C757D,
                     "description": "A advertência foi retirada. Obrigado por explicar! 🤝" if aceitar else
                                    "A staff analisou e decidiu **manter** a punição."})
    e["color"] = 0x28A745 if aceitar else 0x6C757D
    e["footer"] = {"text": f"Contestação · {alvo} · {'aceita' if aceitar else 'mantida'}"}
    e["fields"] = list(e.get("fields") or []) + [{
        "name": "✅ Aceita" if aceitar else "⚖️ Punição mantida", "value": f"por <@{staff}>", "inline": False}]
    return atualizar(e)


# ----- Ferramentas por cargo -------------------------------------------------
def cmd_limpar(d, uid, n_staff, qtd, membro_id):
    maximo = limite(LIMPAR_MAX, n_staff)
    if qtd > maximo:
        return resposta(texto=f"Seu cargo pode apagar até **{maximo}** mensagens de uma vez.", privada=True, componentes=[])
    canal = str(d.get("channel_id"))
    limite_idade = time.time() - 13.5 * 86400
    ids = [m["id"] for m in discord_req("GET", f"/channels/{canal}/messages?limit=100")
           if (not membro_id or str((m.get("author") or {}).get("id")) == membro_id)
           and not m.get("pinned") and snowflake_ts(m["id"]) > limite_idade][:qtd]
    if not ids:
        return resposta(texto="Não achei mensagens para apagar (o Discord só apaga em lote as de até 14 dias).",
                        privada=True, componentes=[])
    if len(ids) == 1:
        discord_req("DELETE", f"/channels/{canal}/messages/{ids[0]}", motivo=f"Limpeza por {uid}")
    else:
        discord_req("POST", f"/channels/{canal}/messages/bulk-delete", {"messages": ids}, f"Limpeza por {uid}")
    log_acao("🧹 Limpeza de mensagens", uid, "limpar",
             [("📍 Canal", f"<#{canal}>"), ("🗑️ Apagadas", len(ids)), ("👤 De", f"<@{membro_id}>" if membro_id else "")])
    return resposta(texto=f"🧹 Apaguei **{len(ids)}** mensagens.", privada=True, componentes=[])


def cmd_lento(d, uid, segundos):
    canal = str(d.get("channel_id"))
    discord_req("PATCH", f"/channels/{canal}", {"rate_limit_per_user": int(segundos)}, f"Modo lento por {uid}")
    log_acao("🐢 Modo lento", uid, "lento", [("📍 Canal", f"<#{canal}>"),
                                             ("⏱️ Tempo", f"{segundos}s" if segundos else "desligado")])
    return resposta(texto=f"🐢 Modo lento {'desligado' if not segundos else f'de {segundos}s ligado'} neste canal.",
                    privada=True, componentes=[])


def cmd_castigo(gid, uid, n_staff, alvo, duracao, motivo):
    maximo = limite(CASTIGO_MAX, n_staff)
    if duracao > maximo:
        return resposta(texto=f"Seu cargo pode castigar por até **{DURACAO_NOME.get(maximo, f'{maximo}s')}**. "
                              "Para mais tempo, peça a alguém acima.", privada=True, componentes=[])
    ate = (datetime.now(timezone.utc) + timedelta(seconds=duracao)).isoformat()
    try:
        discord_req("PATCH", f"/guilds/{gid}/members/{alvo}", {"communication_disabled_until": ate},
                    f"Castigo por {uid}: {motivo}")
    except urllib.error.HTTPError as e:
        return resposta(texto="Não consegui: o cargo da pessoa está acima do bot." if e.code == 403 else
                        f"O Discord recusou ({e.code}).", privada=True, componentes=[])
    avisar_membro(alvo, "🔇 Você foi castigado na Kyzen SA",
                  f"**Tempo:** {DURACAO_NOME.get(duracao)}\n**Motivo:** {cortar(motivo, 900)}")
    log_acao("🔇 Castigo", uid, "castigo", [("👤 Membro", f"<@{alvo}>"), ("⏱️ Tempo", DURACAO_NOME.get(duracao)),
                                           ("📝 Motivo", motivo)], 0xE67E22)
    return resposta(texto=f"🔇 <@{alvo}> castigado por **{DURACAO_NOME.get(duracao)}**.", privada=True, componentes=[])


def cmd_expulsar_banir(gid, uid, alvo, motivo, banir, apagar=0):
    avisar_membro(alvo, "🔨 Você foi banido da Kyzen SA" if banir else "👢 Você foi expulso da Kyzen SA",
                  f"**Motivo:** {cortar(motivo, 900)}")
    try:
        if banir:
            discord_req("PUT", f"/guilds/{gid}/bans/{alvo}", {"delete_message_seconds": int(apagar)}, f"Ban por {uid}: {motivo}")
        else:
            discord_req("DELETE", f"/guilds/{gid}/members/{alvo}", motivo=f"Expulso por {uid}: {motivo}")
    except urllib.error.HTTPError as e:
        return resposta(texto="Não consegui: o cargo da pessoa está acima do bot." if e.code == 403 else
                        "A pessoa não está no servidor." if e.code == 404 else f"O Discord recusou ({e.code}).",
                        privada=True, componentes=[])
    log_acao("🔨 Banimento direto" if banir else "👢 Expulsão", uid, "banir" if banir else "expulsar",
             [("👤 Membro", f"<@{alvo}> ({alvo})"), ("📝 Motivo", motivo)], 0x8B0000 if banir else 0xC0392B)
    return resposta(texto=f"{'🔨 Banido' if banir else '👢 Expulso'}: <@{alvo}>.", privada=True, componentes=[])


def cmd_desbanir(gid, uid, alvo, motivo):
    if not re.fullmatch(r"\d{5,25}", alvo):
        return resposta(texto="Mande só o ID da pessoa (números).", privada=True, componentes=[])
    try:
        discord_req("DELETE", f"/guilds/{gid}/bans/{alvo}", motivo=f"Desbanido por {uid}: {motivo}")
    except urllib.error.HTTPError as e:
        return resposta(texto="Essa pessoa não está banida." if e.code == 404 else f"O Discord recusou ({e.code}).",
                        privada=True, componentes=[])
    log_acao("🕊️ Ban retirado", uid, "desbanir", [("👤 Membro", f"<@{alvo}> ({alvo})"), ("📝 Motivo", motivo)], 0x28A745)
    return resposta(texto=f"🕊️ Ban de <@{alvo}> retirado.", privada=True, componentes=[])


def cmd_trancar(d, uid, trancar, motivo=""):
    gid = str(d.get("guild_id"))
    canal = str(d.get("channel_id"))
    info = discord_req("GET", f"/channels/{canal}")
    ows = {str(o["id"]): o for o in info.get("permission_overwrites") or []}
    o = ows.get(gid) or {}
    allow, deny = int(o.get("allow") or 0), int(o.get("deny") or 0)
    if trancar:
        allow, deny = allow & ~TRAVAR, deny | TRAVAR
        for rid in [r for k, _, r in RANKS if k < 9]:  # a staff continua falando no canal trancado
            s = ows.get(rid) or {}
            if not int(s.get("allow") or 0) & PERM_ENVIAR:
                discord_req("PUT", f"/channels/{canal}/permissions/{rid}",
                            {"type": 0, "allow": str(int(s.get("allow") or 0) | PERM_ENVIAR),
                             "deny": str(int(s.get("deny") or 0) & ~PERM_ENVIAR)}, "Canal trancado")
    else:
        deny &= ~TRAVAR
    discord_req("PUT", f"/channels/{canal}/permissions/{gid}", {"type": 0, "allow": str(allow), "deny": str(deny)},
                f"{'Trancado' if trancar else 'Destrancado'} por {uid}")
    discord_req("POST", f"/channels/{canal}/messages", {"embeds": [{
        "title": "🔒 Canal trancado pela staff" if trancar else "🔓 Canal destrancado",
        "color": 0xC0392B if trancar else 0x28A745,
        "description": (cortar(motivo, 500) if motivo else "Aguarde, a staff já está cuidando disso.") if trancar
                       else "Podem voltar a conversar. 🙂"}], "allowed_mentions": {"parse": []}})
    log_acao("🔒 Canal trancado" if trancar else "🔓 Canal destrancado", uid, "trancar" if trancar else "destrancar",
             [("📍 Canal", f"<#{canal}>"), ("📝 Motivo", motivo)], 0xC0392B if trancar else 0x28A745)
    return resposta(texto="🔒 Canal trancado." if trancar else "🔓 Canal destrancado.", privada=True, componentes=[])


# ----- Relatório semanal da staff --------------------------------------------
def relatorio_semanal(forcar=False):
    agora = datetime.now(BRT)
    if not forcar and not (agora.weekday() == 6 and agora.hour >= 18):
        return 0
    semana = f"{agora.isocalendar()[0]}-S{agora.isocalendar()[1]:02d}"
    reg = canal_registro()
    inicio = time.time() - 7 * 86400
    msgs, antes = [], ""
    for _ in range(5):
        lote = discord_req("GET", f"/channels/{reg}/messages?limit=100" + (f"&before={antes}" if antes else ""))
        msgs += [m for m in lote if snowflake_ts(m["id"]) > inicio]
        if len(lote) < 100 or snowflake_ts(lote[-1]["id"]) < inicio:
            break
        antes = lote[-1]["id"]
    if any((e.get("footer") or {}).get("text") == f"Relatório da staff · {semana}" for m in msgs for e in m.get("embeds") or []):
        return 0
    por, totais = {}, {}

    def soma(staff, chave):
        if staff:
            por.setdefault(staff, {}).setdefault(chave, 0)
            por[staff][chave] += 1
            totais[chave] = totais.get(chave, 0) + 1

    for m in msgs:
        for e in m.get("embeds") or []:
            rod = (e.get("footer") or {}).get("text") or ""
            staff = re.sub(r"\D", "", campo(e, "Staff"))
            if re.match(r"^Membro \d+ · (advertência|advertência perdoada)$", rod):
                soma(staff, "warns")
            elif rod.endswith("· aviso verbal"):
                soma(staff, "avisos")
            elif rod.endswith("· perdão"):
                soma(staff, "perdões")
            elif rod.startswith("Ação · "):
                soma(rod.split(" · ")[1], {"castigo": "castigos", "banir": "bans", "expulsar": "expulsões",
                                           "limpar": "limpezas"}.get(rod.split(" · ")[2], "outras"))
            elif rod.startswith("Contestação · "):
                totais["contestações"] = totais.get("contestações", 0) + 1
            elif rod.startswith("Pedido · "):
                totais["pedidos"] = totais.get("pedidos", 0) + 1
    try:
        for m in discord_req("GET", f"/channels/{canal_staff('bot')}/messages?limit=100"):
            for e in m.get("embeds") or []:
                if ((e.get("footer") or {}).get("text") or "").startswith("ticket · ") and snowflake_ts(m["id"]) > inicio:
                    quem = re.findall(r"fechado por <@(\d+)>", e.get("description") or "")
                    dono = ((e.get("footer") or {}).get("text") or "").split(" · ")[1]
                    if quem and quem[0] != dono:
                        soma(quem[0], "tickets")
        for m in discord_req("GET", f"/channels/{canal_staff('forms')}/messages?limit=100"):
            for e in m.get("embeds") or []:
                if re.search(r"· (aprovado|recusado)$", (e.get("footer") or {}).get("text") or "") and \
                        snowflake_ts(m["id"]) > inicio:
                    quem = re.findall(r"por <@(\d+)>", (e.get("fields") or [{}])[-1].get("value") or "")
                    soma(quem[0] if quem else "", "formulários")
    except urllib.error.URLError:
        pass
    linhas = []
    for staff, c in sorted(por.items(), key=lambda x: -sum(x[1].values()))[:25]:
        linhas.append(f"<@{staff}> · **{sum(c.values())}** · " + " · ".join(f"{v} {k}" for k, v in c.items()))
    discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
        "title": "📊 Relatório semanal da staff", "color": 0x2C3E50,
        "description": ("\n".join(linhas) or "Nenhuma ação registrada nesta semana.") +
                       "\n\n**Total:** " + (" · ".join(f"{v} {k}" for k, v in totais.items()) or "0"),
        "footer": {"text": f"Relatório da staff · {semana}"},
        "timestamp": datetime.now(timezone.utc).isoformat()}], "allowed_mentions": {"parse": []}})
    return 1


# ----- Roteador --------------------------------------------------------------
def interacao_cargos(d, tipo, dados, nome, opcoes, uid):
    gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", "")).strip()
    membro = d.get("member") or {}
    n = nivel(membro)
    resolv = dados.get("resolved") or {}
    cid = str(dados.get("custom_id") or "")
    if tipo == 3:
        if cid in ("ap:ok", "ap:no"):
            return decidir_pedido(d, uid, cid == "ap:ok")
        ach = re.fullmatch(r"ct:(\d{5,25})", cid)
        if ach:
            if ach.group(1) != uid:
                return resposta(texto="Esse botão é só da pessoa punida.", privada=True, componentes=[])
            return modal_contestar(uid)
        ach = re.fullmatch(r"ct:(ok|no):(\d{5,25})", cid)
        if ach:
            return decidir_contestacao(d, uid, ach.group(2), ach.group(1) == "ok")
        return None
    if tipo == 5 and re.fullmatch(r"ct:env:\d{5,25}", cid):
        valores = {c.get("custom_id"): (c.get("value") or "").strip()
                   for linha in dados.get("components") or [] for c in linha.get("components") or []}
        return enviar_contestacao(uid, valores.get("texto", ""))
    if tipo != 2:
        return None
    req = {"limpar": 2, "lento": 3, "castigo": 3, "expulsar": 5, "banir": 5, "trancar": 5, "destrancar": 5,
           "desbanir": 7}.get(nome)
    if not req:
        return None
    if n < req:
        return so_a_partir(req)
    if nome == "limpar":
        return cmd_limpar(d, uid, n, int(opcoes.get("quantidade") or 1), str(opcoes.get("membro") or ""))
    if nome == "lento":
        return cmd_lento(d, uid, int(opcoes.get("segundos") or 0))
    if nome in ("trancar", "destrancar"):
        return cmd_trancar(d, uid, nome == "trancar", str(opcoes.get("motivo") or ""))
    if nome == "desbanir":
        return cmd_desbanir(gid, uid, str(opcoes.get("id") or "").strip(), str(opcoes.get("motivo") or ""))
    alvo_id = str(opcoes.get("membro") or "")
    erro = checar_alvo(uid, (resolv.get("users") or {}).get(alvo_id), (resolv.get("members") or {}).get(alvo_id), n)
    if erro:
        return resposta(texto=erro, privada=True, componentes=[])
    motivo = str(opcoes.get("motivo") or "")
    if nome == "castigo":
        return cmd_castigo(gid, uid, n, alvo_id, int(opcoes.get("duracao") or 600), motivo)
    return cmd_expulsar_banir(gid, uid, alvo_id, motivo, nome == "banir", int(opcoes.get("apagar") or 0))


# ----- Diretrizes oficiais da staff (temporário: cria o canal e posta) ------
DIRETRIZES_INTRO = {
    "title": "📜 Diretrizes oficiais da Staff Kyzen", "color": 0xC9954F,
    "description": "A partir de hoje, **cada cargo da staff tem funções e poderes próprios**, no Discord e no "
                   "**Kyzen Bot**. Este comunicado é **oficial** e vale para toda a equipe.\n"
                   "Leiam com atenção. Dúvidas? Falem com um **Adm ou acima**.",
    "fields": [
        {"name": "⚖️ Regra de ouro", "inline": False,
         "value": "Toda punição é feita pelo **Kyzen Bot**: clique direito no membro ou na mensagem → **Apps** → "
                  "**Advertir**, ou `/advertir`. Assim ela fica registrada, o membro é avisado na DM e entra no "
                  "relatório. **Não usem o castigo/ban manual do Discord.**"},
        {"name": "🪜 Hierarquia", "inline": False,
         "value": "Helper → Junior Staff → Staff → Senior Staff → Mod → Senior Mod → Adm → Head Admin → "
                  "Dev / Co-Owner / Owner"},
        {"name": "📋 Tabela de punições", "inline": False,
         "value": "🗣️ Aviso verbal → sem punição\n1º warn → mute 15 min\n2º warn → mute 1 h\n"
                  "3º warn → mute 1 dia\n4º warn → mute 3 dias\n5º warn → **ban**"},
        {"name": "✋ Passou do seu cargo?", "inline": False,
         "value": "O bot **não aplica**: ele manda um **pedido de aprovação** no #registro-punições e marca o cargo "
                  "necessário. Quem estiver desse cargo pra cima aprova ou recusa com um clique."},
        {"name": "📨 Contestações", "inline": False,
         "value": "O membro punido pode **contestar** pela DM. Senior Staff ou acima analisa e decide: tirar a "
                  "punição ou manter. Tratem com respeito: contestar é um direito."},
        {"name": "🚫 Proibido para a staff", "inline": False,
         "value": "• Punir alguém de cargo **igual ou maior** que o seu\n• Punir **sem motivo** ou sem prova\n"
                  "• Usar punição em **briga pessoal**\n• Apagar mensagens do #registro-punições\n"
                  "• Expor denúncias ou tickets fora da staff"},
        {"name": "📊 Relatório semanal", "inline": False,
         "value": "Todo **domingo às 19h** o bot posta quantas ações cada um fez (warns, castigos, tickets, "
                  "formulários). Ele é usado para avaliar **promoções**."},
    ],
    "footer": {"text": "Kyzen SA · Diretrizes da Staff"},
}
DIRETRIZES_CARGOS = [
    ("🟢 Helper", 0x2ECC71,
     "Receber quem chega, tirar dúvidas no chat, **atender tickets** e **avaliar formulários** da crew. "
     "Orientar com conversa e aviso verbal antes de qualquer punição.",
     "🗣️ Aviso verbal · 🔎 `/historico` · 🎫 fechar tickets · 📝 aprovar/recusar formulários · 🔊 mover membros "
     "em call\nPara warns: pedido de aprovação."),
    ("🔵 Junior Staff", 0x3498DB,
     "Moderar o chat no **dia a dia**: spam, flood e desrespeito leve. Dar as **primeiras advertências**.",
     "Tudo do Helper + ⚠️ **1º e 2º warn** · 🧹 `/limpar` (até 20) · apagar mensagens · mutar em call · "
     "gerenciar tópicos"),
    ("🟣 Staff", 0x9B59B6,
     "Manter a ordem nos **chats e calls**, lidar com **reincidentes** e aprovar os pedidos de Helpers e "
     "Juniors.",
     "Tudo do Junior + ⚠️ **até o 3º warn** (mute 1 dia) · 🐢 `/lento` · 🔇 `/castigo` (até 1 h) · mudar "
     "apelidos · ensurdecer em call · vê o #registro-punições"),
    ("🟠 Senior Staff", 0xE67E22,
     "Cuidar dos **casos sérios**, revisar **contestações** e **orientar** Helpers e Juniors.",
     "Tudo do Staff + ⚠️ **até o 4º warn** (mute 3 dias) · 🕊️ `/perdoar` · 📨 decidir contestações · "
     "🔇 `/castigo` (até 1 dia) · status das sugestões"),
    ("🔴 Mod", 0xE74C3C,
     "Tomar as **decisões finais** de punição e agir em **emergências** (raids, brigas, spam em massa).",
     "Tudo do Senior Staff + ⚠️ **5º warn (ban)** · 🔨 `/banir` · 👢 `/expulsar` · 🔒 `/trancar` / `/destrancar` · "
     "🧹 `/limpar` (até 100) · 🔇 `/castigo` (até 7 dias)"),
    ("🟥 Senior Mod", 0xC0392B,
     "**Coordenar** a equipe de moderação, os **eventos**, as **wars** da crew e os **sorteios**.",
     "Tudo do Mod + 🎉 `/sorteio` · ⚔️ `/war` · gerenciar eventos · banir pelo Discord · mencionar @everyone"),
    ("👑 Adm", 0xF1C40F,
     "**Gestão do servidor**: canais, cargos, painéis e avisos oficiais. Acompanhar e **avaliar a staff**.",
     "Tudo do Senior Mod + 🏷️ `/painel-cargos` · 📢 `/agendar` · "
     "🕊️ `/desbanir` · 🔇 `/castigo` (até 28 dias) · gerenciar canais e cargos"),
    ("💎 Head Admin · Dev · Co-Owner · Owner", 0x1ABC9C,
     "**Direção geral** da Kyzen: promoções, rebaixamentos e a palavra final em qualquer caso.",
     "Todos os poderes · 📊 relatório semanal da staff"),
]


# ---------------------------------------------------------------------------
# Atendimento (igual ao Apolo) e formulários Staff Apply / Crew Apply (igual ao Appy)
# ---------------------------------------------------------------------------
CANAL_LOGS_TICKET = "1511447116265033808"
PING_TICKET = ["1497068096450465792", "1504676074641162240", "1384722554064867328"]  # Helper, Junior Staff, Staff
CATEGORIAS_TICKET = {"suporte": ("Suporte", "❓"), "denuncia": ("Denúncia", "⚠️"), "parceria": ("Parceria", "🤝")}
_icone = {"url": ""}


def icone_servidor():
    if not _icone["url"]:
        gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
        try:
            h = discord_req("GET", f"/guilds/{gid}").get("icon")
            _icone["url"] = f"https://cdn.discordapp.com/icons/{gid}/{h}.png?size=256" if h else SITE + "/img/icon-192.png"
        except urllib.error.URLError:
            return SITE + "/img/icon-192.png"
    return _icone["url"]


def avatar_de(u):
    uid = str(u.get("id") or "0")
    if u.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{uid}/{u['avatar']}.png?size=256"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6 if uid.isdigit() else 0}.png"


def jr(tipo, data):
    return jsonify({"type": tipo, "data": data})


def efemera(texto, componentes=None):
    return jr(4, {"content": texto, "flags": 64, "components": componentes or [], "allowed_mentions": {"parse": []}})


def painel_atendimento():
    return {"embeds": [{
        "author": {"name": "Kyzen SA", "icon_url": icone_servidor()},
        "title": "🎟️・Central de Atendimento", "color": 16776960,
        "description": "- Olá, seja bem-vindo(a) a central de atendimento da Kyzen SA, abaixo vamos listar os tipos de "
                       "departamentos e suporte presentes na nossa equipe.\n\n- Selecione abaixo qual departamento está "
                       "relacionado a sua dúvida ou problema e será gerado em um tópico para que seu atendimento seja "
                       "realizado de forma segura e ágil.",
        "thumbnail": {"url": icone_servidor()}, "image": {"url": SITE + "/img/og.jpg"}}],
        "components": [{"type": 1, "components": [{
            "type": 3, "custom_id": "at:menu", "placeholder": "Clique para escolher uma opção",
            "options": [{"label": n, "value": k, "emoji": {"name": e}} for k, (n, e) in CATEGORIAS_TICKET.items()]}]}]}


def numero_ticket():
    maior = 0
    try:
        for m in discord_req("GET", f"/channels/{CANAL_LOGS_TICKET}/messages?limit=50"):
            for e in m.get("embeds") or []:
                for f in e.get("fields") or []:
                    ach = re.search(r"Ticket N° (\d+)", f.get("value") or "")
                    if ach:
                        maior = max(maior, int(ach.group(1)))
    except urllib.error.URLError:
        pass
    return maior + 1


def log_ticket(titulo, cor, usuario, campos, botao=None):
    corpo = {"embeds": [{"author": {"name": usuario.get("username") or "membro", "icon_url": avatar_de(usuario)},
                         "title": titulo, "color": cor, "thumbnail": {"url": avatar_de(usuario)},
                         "fields": [{"name": n, "value": v, "inline": False} for n, v in campos]}],
             "allowed_mentions": {"parse": []}}
    if botao:
        corpo["components"] = [{"type": 1, "components": [{"type": 2, "style": 5, "label": botao[0], "url": botao[1],
                                                           "emoji": {"name": "💬"}}]}]
    return corpo


def abrir_atendimento(d, usuario, categoria, aberto_por=None):
    gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", ""))
    uid = str(usuario.get("id"))
    nome_cat = CATEGORIAS_TICKET.get(categoria, ("Formulário", "📝"))[0]
    for c in discord_req("GET", f"/guilds/{gid}/channels"):
        if dono_ticket(c.get("topic"))[0] == uid:
            return f"<@{uid}> já tem um ticket aberto: <#{c['id']}>", None
    n = numero_ticket()
    vis = PERM_VER | PERM_ENVIAR | PERM_HIST | PERM_ANEXOS | PERM_LINKS
    ow = [{"id": gid, "type": 0, "allow": "0", "deny": str(PERM_VER)},
          {"id": uid, "type": 1, "allow": str(vis), "deny": "0"}]
    ow += [{"id": r, "type": 0, "allow": str(vis), "deny": "0"} for r in cargos_staff(gid)]
    corpo = {"name": f"ticket-{slug_py(usuario.get('username') or uid)[:40] or uid}", "type": 0,
             "topic": f"Ticket de {uid} · {categoria} · N° {n} · aberto em <t:{int(time.time())}:f>",
             "permission_overwrites": ow, "parent_id": "1383999611580186644"}
    canal = discord_req("POST", f"/guilds/{gid}/channels", corpo, f"Ticket N° {n} de {uid}")
    discord_req("POST", f"/channels/{canal['id']}/messages", {
        "content": ", ".join([f"<@{uid}>"] + [f"<@&{r}>" for r in PING_TICKET]),
        "embeds": [{"author": {"name": "Kyzen SA", "icon_url": icone_servidor()}, "title": f"Ticket - {nome_cat}",
                    "color": 16777209, "thumbnail": {"url": avatar_de(usuario)},
                    "description": "Nossa equipe falará com você em breve.\n(Our team will contact you shortly.)",
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 4, "label": "Fechar", "emoji": {"name": "❌"}, "custom_id": "tk:fechar"},
            {"type": 2, "style": 3, "label": "Assumir Ticket", "emoji": {"name": "💬"}, "custom_id": "at:assumir"},
            {"type": 2, "style": 3, "label": "Chamar Staff", "emoji": {"name": "🔔"}, "custom_id": "at:staff"},
            {"type": 2, "style": 3, "label": "Chamar Membro", "emoji": {"name": "🔔"}, "custom_id": "at:membro"}]}],
        "allowed_mentions": {"users": [uid], "roles": PING_TICKET}})
    try:
        discord_req("POST", f"/channels/{CANAL_LOGS_TICKET}/messages", log_ticket(
            "Novo ticket!", 5763719, usuario,
            [("👤 Membro", f"{usuario.get('username')} (ID: `{uid}`)"), ("🎫 Id", f"Ticket N° {n}"),
             ("📂 Categoria", nome_cat + (f" · aberto por <@{aberto_por}>" if aberto_por else "")),
             ("🕒 Data", f"<t:{int(time.time())}:F>")],
            ("Abrir Ticket", f"https://discord.com/channels/{gid}/{canal['id']}")))
    except urllib.error.URLError:
        pass
    return f"✅ Ticket criado: <#{canal['id']}>", canal["id"]


def fechar_atendimento(d, uid, confirmado):
    canal = discord_req("GET", f"/channels/{d.get('channel_id')}")
    dono, tipo = dono_ticket(canal.get("topic"))
    if not dono:
        return efemera("Este canal não é um ticket.")
    if dono != uid and nivel(d.get("member")) < 1:
        return efemera("Só quem abriu o ticket ou a staff pode fechar.")
    if not confirmado:
        return efemera("Tem certeza que quer fechar o ticket? O canal será apagado (a conversa fica salva no "
                       "#logs-ticket).", [{"type": 1, "components": [
                           {"type": 2, "style": 4, "label": "Sim, fechar", "custom_id": "tk:sim"}]}])
    num = (re.search(r"N° (\d+)", canal.get("topic") or "") or [None, "?"])[1]
    linhas, antes = [], ""
    for _ in range(5):
        lote = discord_req("GET", f"/channels/{canal['id']}/messages?limit=100" + (f"&before={antes}" if antes else ""))
        for m in lote:
            a = m.get("author") or {}
            quando = datetime.fromtimestamp(snowflake_ts(m["id"]), timezone.utc) - timedelta(hours=3)
            txt = m.get("content") or ""
            txt += "".join(f"\n    [anexo] {x.get('url')}" for x in m.get("attachments") or [])
            txt += "".join(f"\n    [embed] {x.get('title') or ''} {x.get('description') or ''}" for x in m.get("embeds") or [])
            linhas.append(f"[{quando:%d/%m %H:%M}] {a.get('global_name') or a.get('username')}: {txt}")
        if len(lote) < 100:
            break
        antes = lote[-1]["id"]
    copia = (f"Ticket N° {num} · #{canal.get('name')} · {tipo}\nAberto por {dono} · fechado por {uid}\n\n"
             + "\n".join(reversed(linhas))).encode("utf-8")
    quem = (d.get("member") or {}).get("user") or {}
    try:
        dono_info = discord_req("GET", f"/users/{dono}")
    except urllib.error.URLError:
        dono_info = {"id": dono, "username": dono}
    try:
        corpo = log_ticket("Ticket fechado!", 15578705, dono_info,
                           [("👤 Membro", f"{dono_info.get('username')} (ID: `{dono}`)"), ("🎫 Id", f"Ticket N° {num}"),
                            ("🔒 Fechado por", f"{quem.get('username')} (ID: `{uid}`)"),
                            ("🕒 Data", f"<t:{int(time.time())}:F>")])
        msg = discord_arquivo(f"/channels/{CANAL_LOGS_TICKET}/messages", corpo, f"ticket-{num}.txt", copia)
        url = ((msg.get("attachments") or [{}])[0]).get("url")
        if url:
            discord_req("PATCH", f"/channels/{CANAL_LOGS_TICKET}/messages/{msg['id']}", {"components": [
                {"type": 1, "components": [{"type": 2, "style": 5, "label": "Ler o ticket", "url": url,
                                            "emoji": {"name": "💬"}}]}]})
    except urllib.error.URLError:
        return efemera("Não consegui salvar a cópia do ticket, então não apaguei. Tente de novo.")
    discord_req("DELETE", f"/channels/{canal['id']}", motivo=f"Ticket N° {num} fechado por {uid}")
    return jr(6, {})


def chamar(d, uid, quem):
    canal = discord_req("GET", f"/channels/{d.get('channel_id')}")
    dono, _ = dono_ticket(canal.get("topic"))
    if not dono:
        return efemera("Este canal não é um ticket.")
    if quem == "assumir":
        if nivel(d.get("member")) < 1:
            return efemera("Só a staff pode assumir o ticket.")
        return jr(4, {"content": f"✅ | <@{dono}> O ticket foi assumido por <@{uid}>!",
                      "allowed_mentions": {"users": [dono]}})
    if quem == "membro":
        if nivel(d.get("member")) < 1:
            return efemera("Só a staff pode chamar o membro.")
        gid = str(d.get("guild_id"))
        avisar_dm(dono, {"title": "🔔 A staff está te chamando no seu ticket", "color": 16776960,
                         "description": f"Volte ao seu ticket: https://discord.com/channels/{gid}/{canal['id']}"})
        return jr(4, {"content": f"🔔 | <@{dono}>, a staff está te chamando aqui no ticket!",
                      "allowed_mentions": {"users": [dono]}})
    for m in discord_req("GET", f"/channels/{canal['id']}/messages?limit=30"):
        if (m.get("content") or "").startswith("🔔 | A staff foi chamada") and time.time() - snowflake_ts(m["id"]) < 600:
            return efemera("A staff já foi chamada há pouco. Aguarde um pouquinho. 🙂")
    return jr(4, {"content": f"🔔 | A staff foi chamada por <@{uid}>! " + " ".join(f"<@&{r}>" for r in PING_TICKET),
                  "allowed_mentions": {"roles": PING_TICKET}})


# ----- Formulários (igual ao Appy) -------------------------------------------
FORMULARIOS = {
    "crew": {"nome": "Crew Apply", "emoji": "⚔️", "canal": "1571135541049688084", "cargo": "1446301930639986840",
             "ping": cargos_staff("", 7), "descricao": "Entre para a crew da Kyzen no Vox Seas",
             "perguntas": [
                 ("Qual seu nick do Roblox?", "Nick do Roblox", 1, 60),
                 ("Quantos anos você tem?", "Idade", 1, 10),
                 ("Você joga casualmente ou competitivamente?", "Casual ou competitivo?", 1, 100),
                 ("Quanto tempo livre você tem por dia para jogar? Se possível, especifique os horários.",
                  "Tempo livre por dia e horários", 2, 400),
                 ("O que você é? (ex: usuário de fruta, espadachim, atirador, etc.)", "O que você é?", 1, 150),
                 ("Quanto de bounty você tem atualmente?", "Bounty atual", 1, 50)]},
    "staff": {"nome": "Staff Apply", "emoji": "🛡️", "canal": "1398733203698880532", "cargo": "1497068096450465792",
              "ping": ["1383960212641157201"], "descricao": "Faça parte da equipe da staff",
              "perguntas": [
                  ("Qual seu nome no discord?", "Nome no Discord", 1, 60),
                  ("Quantos anos você tem?", "Idade", 1, 10),
                  ("Já teve experiência como staff/moderador em outros servidores? Se sim, descreva o que fazia e por "
                   "quanto tempo.", "Experiência como staff/moderador", 2, 1000),
                  ("Por que você acha que seria uma boa adição para a Staff da Kyzen?", "Por que seria uma boa adição?",
                   2, 1000),
                  ("Quanto tempo livre você tem por dia para ajudar no servidor? Se possível, especifique os horários.",
                   "Tempo livre por dia e horários", 2, 400),
                  ("Você está disposto(a) a nos ajudar quando lhe pedirmos para fazer algo?", "Disposto(a) a ajudar?",
                   1, 100),
                  ("Tem alguma habilidade adicional que gostaria de destacar?\n### (Ex: Design, Bot Management, Editing, "
                   "Languages, Event Organization, etc.)", "Habilidade adicional", 2, 600)]},
}


def painel_formularios():
    return {"embeds": [{
        "author": {"name": "Kyzen SA", "icon_url": icone_servidor()}, "title": "📋・Formulários da Kyzen",
        "color": 8972168, "thumbnail": {"url": icone_servidor()},
        "description": "Quer fazer parte da Kyzen? Escolha abaixo o formulário:\n\n"
                       + "\n".join(f"{f['emoji']} **{f['nome']}**: {f['descricao']}" for f in FORMULARIOS.values())
                       + "\n\nResponda com calma e com sinceridade. A resposta da staff chega na sua **DM**, então "
                         "deixe a DM aberta."}],
        "components": [{"type": 1, "components": [{
            "type": 3, "custom_id": "fa:menu", "placeholder": "Faça uma seleção",
            "options": [{"label": f["nome"], "value": k, "description": f["descricao"], "emoji": {"name": f["emoji"]}}
                        for k, f in FORMULARIOS.items()]}]}]}


def modal_aplicacao(tipo, parte, inicio):
    f = FORMULARIOS[tipo]
    qs = f["perguntas"][:5] if parte == 1 else f["perguntas"][5:]
    base = 0 if parte == 1 else 5
    total = 1 if len(f["perguntas"]) <= 5 else 2
    return jr(9, {"custom_id": f"fa:env:{tipo}:{parte}:{inicio}",
                  "title": cortar(f"{f['nome']} ({parte}/{total})", 45),
                  "components": [{"type": 1, "components": [{
                      "type": 4, "custom_id": f"q{base + i}", "label": cortar(f"{base + i + 1}. {curto}", 45),
                      "placeholder": cortar(texto.split(chr(10))[0], 100), "style": st, "required": True,
                      "max_length": mx}]} for i, (texto, curto, st, mx) in enumerate(qs)]})


def pendente_de(tipo, uid):
    f = FORMULARIOS[tipo]
    for m in discord_req("GET", f"/channels/{f['canal']}/messages?limit=100"):
        for e in m.get("embeds") or []:
            if (e.get("footer") or {}).get("text") == f"Kyzen Bot · {tipo} · {uid} · pendente":
                return True
    return False


def enviar_aplicacao(d, uid, tipo, parte, inicio, valores):
    f = FORMULARIOS[tipo]
    reg = canal_staff("bot")
    respostas = {k: v for k, v in valores.items() if k.startswith("q")}
    if parte == 1 and len(f["perguntas"]) > 5:
        discord_req("POST", f"/channels/{reg}/messages", {"embeds": [{
            "title": f"📝 Rascunho de {f['nome']}", "color": 0x6C757D,
            "description": "```json\n" + json.dumps(respostas, ensure_ascii=False)[:3900] + "\n```",
            "footer": {"text": f"rascunho · {tipo} · {uid}"}}], "allowed_mentions": {"parse": []}})
        return efemera(f"✅ Parte 1 salva! Clique abaixo para responder o resto do **{f['nome']}**.",
                       [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Continuar (2/2)",
                                                    "custom_id": f"fa:p2:{tipo}:{inicio}"}]}])
    if parte == 2:
        for m in discord_req("GET", f"/channels/{reg}/messages?limit=100"):
            e = (m.get("embeds") or [{}])[0]
            if (e.get("footer") or {}).get("text") == f"rascunho · {tipo} · {uid}":
                try:
                    antes = json.loads((e.get("description") or "").strip("`").replace("json\n", "", 1))
                except ValueError:
                    antes = {}
                respostas = dict(antes, **respostas)
                try:
                    discord_req("DELETE", f"/channels/{reg}/messages/{m['id']}")
                except urllib.error.URLError:
                    pass
                break
    membro = d.get("member") or {}
    u = membro.get("user") or {}
    agora = int(time.time())
    try:
        entrou = int(datetime.fromisoformat(str(membro.get("joined_at")).replace("Z", "+00:00")).timestamp())
    except ValueError:
        entrou = agora
    desc = "\n".join(f"### **{i + 1}.** {texto}\n{cortar(respostas.get(f'q{i}') or '—', 1000)}"
                     for i, (texto, _, _, _) in enumerate(f["perguntas"]))
    discord_req("POST", f"/channels/{f['canal']}/messages", {
        "content": " ".join(f"<@&{r}>" for r in f["ping"]),
        "embeds": [{"title": f"{u.get('username')}'s '{f['nome']}' Application Submitted", "color": 8972168,
                    "description": cortar(desc, 4000), "thumbnail": {"url": avatar_de(u)},
                    "fields": [{"name": "Submission stats", "value":
                                f"UserId: `{uid}`\nUsername: `{u.get('username')}`\nUser: <@{uid}>\n"
                                f"Duration: `{max(0, agora - int(inicio))}s`\nJoined guild <t:{entrou}:R>\n"
                                f"Submitted: <t:{agora}:R>"}],
                    "footer": {"text": f"Kyzen Bot · {tipo} · {uid} · pendente"}}],
        "components": [
            {"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Accept", "custom_id": f"fd:ok:{tipo}:{uid}"},
                {"type": 2, "style": 4, "label": "Deny", "custom_id": f"fd:no:{tipo}:{uid}"},
                {"type": 2, "style": 3, "label": "Accept with reason", "custom_id": f"fd:okr:{tipo}:{uid}"},
                {"type": 2, "style": 4, "label": "Deny with reason", "custom_id": f"fd:nor:{tipo}:{uid}"}]},
            {"type": 1, "components": [
                {"type": 2, "style": 1, "label": "History", "custom_id": f"fd:hist:{tipo}:{uid}"},
                {"type": 2, "style": 2, "label": "🎫 Open ticket with user", "custom_id": f"fd:tk:{tipo}:{uid}"}]}],
        "allowed_mentions": {"roles": f["ping"]}})
    return efemera(f"✅ Seu **{f['nome']}** foi enviado! A staff vai analisar e a resposta chega na sua **DM**.")


def decidir_aplicacao(d, staff, tipo, alvo, aceito, motivo=""):
    f = FORMULARIOS[tipo]
    msg = d.get("message") or {}
    e = dict((msg.get("embeds") or [{}])[0])
    if not ((e.get("footer") or {}).get("text") or "").endswith("pendente"):
        return efemera("Esse formulário já foi respondido.")
    gid = str(d.get("guild_id"))
    nota = ""
    if aceito and f["cargo"]:
        try:
            discord_req("PUT", f"/guilds/{gid}/members/{alvo}/roles/{f['cargo']}", motivo=f"{f['nome']} aceito por {staff}")
        except urllib.error.HTTPError as err:
            nota = " (⚠️ não consegui dar o cargo" + (": a pessoa saiu do servidor)" if err.code == 404 else ")")
    avisar_dm(alvo, {"title": f"{'✅' if aceito else '❌'} Seu formulário '{f['nome']}' na Kyzen SA foi "
                              f"{'aceito' if aceito else 'recusado'}",
                     "color": 0x28A745 if aceito else 0xD9534F,
                     "description": (f"**Motivo:** {cortar(motivo, 1500)}" if motivo else
                                     ("Seja bem-vindo(a)! 🏴‍☠️" if aceito else "Você pode tentar de novo mais pra frente."))})
    e["color"] = 0x28A745 if aceito else 0xD9534F
    e["footer"] = {"text": f"Kyzen Bot · {tipo} · {alvo} · {'aceito' if aceito else 'recusado'}"}
    conteudo = (f"<@{alvo}>'s submission has been {'accepted' if aceito else 'denied'} successfully by <@{staff}>"
                + (f" with reason: ```{cortar(motivo, 1500)}```" if motivo else "") + nota)
    return jr(7, {"content": conteudo, "embeds": [e], "components": [], "allowed_mentions": {"parse": []}})


def historico_aplicacao(tipo, alvo):
    f = FORMULARIOS[tipo]
    linhas = []
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    for m in discord_req("GET", f"/channels/{f['canal']}/messages?limit=100"):
        for e in m.get("embeds") or []:
            stats = " ".join(fl.get("value") or "" for fl in e.get("fields") or [])
            if f"UserId: `{alvo}`" in stats:
                c = m.get("content") or ""
                st = "✅ aceito" if "accepted" in c else "❌ recusado" if "denied" in c else "⏳ pendente"
                linhas.append(f"<t:{int(snowflake_ts(m['id']))}:d> · {st} · "
                              f"https://discord.com/channels/{gid}/{f['canal']}/{m['id']}")
    return efemera(f"**Histórico de <@{alvo}> no {f['nome']}:**\n" + ("\n".join(linhas[:10]) or "Nenhum envio encontrado."))


def interacao_atendimento(d, tipo, dados, nome, opcoes, uid):
    cid = str(dados.get("custom_id") or "")
    membro = d.get("member") or {}
    if tipo == 3:
        if cid == "at:menu":
            cat = (dados.get("values") or [""])[0]
            if cat not in CATEGORIAS_TICKET:
                return efemera("Opção inválida.")
            texto, _ = abrir_atendimento(d, membro.get("user") or {"id": uid}, cat)
            return efemera(texto)
        if cid in ("tk:fechar", "tk:sim"):
            return fechar_atendimento(d, uid, cid == "tk:sim")
        if cid in ("at:assumir", "at:staff", "at:membro"):
            return chamar(d, uid, cid[3:])
        if cid == "fa:menu":
            t = (dados.get("values") or [""])[0]
            if t not in FORMULARIOS:
                return efemera("Opção inválida.")
            if pendente_de(t, uid):
                return efemera(f"Você já tem um **{FORMULARIOS[t]['nome']}** em análise. Aguarde a resposta na DM. 🙂")
            return modal_aplicacao(t, 1, int(time.time()))
        ach = re.fullmatch(r"fa:p2:(\w+):(\d+)", cid)
        if ach and ach.group(1) in FORMULARIOS:
            return modal_aplicacao(ach.group(1), 2, int(ach.group(2)))
        ach = re.fullmatch(r"fd:(ok|no|okr|nor|hist|tk):(\w+):(\d{5,25})", cid)
        if ach and ach.group(2) in FORMULARIOS:
            acao, t, alvo = ach.groups()
            if acao in ("ok", "no"):
                return decidir_aplicacao(d, uid, t, alvo, acao == "ok")
            if acao in ("okr", "nor"):
                return jr(9, {"custom_id": f"fd:m{acao}:{t}:{alvo}", "title": "Accept with reason" if acao == "okr"
                              else "Deny with reason", "components": [{"type": 1, "components": [{
                                  "type": 4, "custom_id": "motivo", "label": "Motivo", "style": 2, "required": True,
                                  "max_length": 1500}]}]})
            if acao == "hist":
                return historico_aplicacao(t, alvo)
            try:
                usuario = discord_req("GET", f"/users/{alvo}")
            except urllib.error.URLError:
                usuario = {"id": alvo, "username": alvo}
            texto, _ = abrir_atendimento(d, usuario, "formulario", aberto_por=uid)
            return efemera(texto)
        return None
    if tipo == 5:
        valores = {c.get("custom_id"): (c.get("value") or "").strip()
                   for linha in dados.get("components") or [] for c in linha.get("components") or []}
        ach = re.fullmatch(r"fa:env:(\w+):([12]):(\d+)", cid)
        if ach and ach.group(1) in FORMULARIOS:
            return enviar_aplicacao(d, uid, ach.group(1), int(ach.group(2)), int(ach.group(3)), valores)
        ach = re.fullmatch(r"fd:m(okr|nor):(\w+):(\d{5,25})", cid)
        if ach and ach.group(2) in FORMULARIOS:
            return decidir_aplicacao(d, uid, ach.group(2), ach.group(3), ach.group(1) == "okr", valores.get("motivo", ""))
    return None


# ---------------------------------------------------------------------------
# Painel da staff no site, recompensa por convite, agenda das wars e Onboarding
# ---------------------------------------------------------------------------
CARGOS_CONVITE = [(3, "🎟️ Recrutador"), (10, "⭐ Recrutador de Elite"), (25, "👑 Lenda do Recrutamento")]
CANAL_INVITES = "1384013116194558092"
CAT_INFORMACOES = "1383949340669775954"
_nomes = {}
_painel = {"t": 0, "data": None}
_wars_site = {"t": 0, "data": None}


def nome_de(uid):
    c = _nomes.get(uid)
    if c and time.time() - c[0] < 3600:
        return c[1]
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    try:
        m = discord_req("GET", f"/guilds/{gid}/members/{uid}")
        u = m.get("user") or {}
        n = m.get("nick") or u.get("global_name") or u.get("username") or uid
    except urllib.error.URLError:
        n = uid
    _nomes[uid] = (time.time(), n)
    return n


def nivel_uid(uid):
    acesso(uid)
    c = _acesso_cache.get(uid)
    cargos = c[3] if c and len(c) > 3 else []
    return max([NIVEL_DO_CARGO.get(str(r), 0) for r in cargos] + [0])


def convites_por_pessoa():
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    tot = {}
    for i in discord_req("GET", f"/guilds/{gid}/invites"):
        u = i.get("inviter") or {}
        if u.get("id") and not u.get("bot"):
            tot[str(u["id"])] = tot.get(str(u["id"]), 0) + int(i.get("uses") or 0)
    return tot


def wars_abertas(contar=True):
    out = []
    for m, e, p in registros_bot("war"):
        if len(p) != 5 or p[4] != "aberta" or int(p[3]) < time.time() - 3 * 3600:
            continue
        canal, msg, ts = p[1], p[2], int(p[3])
        w = {"titulo": (e.get("title") or "War").replace("⚔️ War: ", ""), "ts": ts,
             "link": f"https://discord.com/channels/{os.environ.get('DISCORD_GUILD_ID', '').strip()}/{canal}/{msg}",
             "vao": 0, "talvez": 0}
        if contar:
            try:
                ew = (discord_req("GET", f"/channels/{canal}/messages/{msg}").get("embeds") or [{}])[0]
                listas = listas_war(ew)
                w["vao"], w["talvez"] = len(listas.get("sim", [])), len(listas.get("talvez", []))
            except urllib.error.URLError:
                pass
        out.append(w)
    return sorted(out, key=lambda w: w["ts"])[:6]


def painel_staff_dados():
    if _painel["data"] and time.time() - _painel["t"] < 60:
        return _painel["data"]
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    reg = canal_registro()
    inicio = time.time() - 7 * 86400
    msgs, antes = [], ""
    for _ in range(3):
        lote = discord_req("GET", f"/channels/{reg}/messages?limit=100" + (f"&before={antes}" if antes else ""))
        msgs += lote
        if len(lote) < 100 or snowflake_ts(lote[-1]["id"]) < inicio:
            break
        antes = lote[-1]["id"]
    por, totais, pendentes = {}, {}, []

    def soma(staff, chave):
        if staff:
            por.setdefault(staff, {}).setdefault(chave, 0)
            por[staff][chave] += 1
            totais[chave] = totais.get(chave, 0) + 1

    for m in msgs:
        for e in m.get("embeds") or []:
            rod = (e.get("footer") or {}).get("text") or ""
            link = f"https://discord.com/channels/{gid}/{reg}/{m['id']}"
            if rod.endswith("· pendente") and rod.startswith(("Pedido · ", "Contestação · ")):
                pendentes.append({"tipo": "Pedido de aprovação" if rod.startswith("Pedido") else "Contestação",
                                  "titulo": e.get("title") or "", "link": link, "ts": int(snowflake_ts(m["id"]))})
            if snowflake_ts(m["id"]) < inicio:
                continue
            staff = re.sub(r"\D", "", campo(e, "Staff"))
            if re.match(r"^Membro \d+ · (advertência|advertência perdoada)$", rod):
                soma(staff, "warns")
            elif rod.endswith("· aviso verbal"):
                soma(staff, "avisos")
            elif rod.endswith("· perdão"):
                soma(staff, "perdões")
            elif rod.startswith("Ação · "):
                pt = rod.split(" · ")
                soma(pt[1], {"castigo": "castigos", "banir": "bans", "expulsar": "expulsões",
                             "limpar": "limpezas"}.get(pt[2] if len(pt) > 2 else "", "outras"))
    tickets = [{"nome": c.get("name"), "link": f"https://discord.com/channels/{gid}/{c['id']}",
                "ts": int(snowflake_ts(c["id"]))}
               for c in discord_req("GET", f"/guilds/{gid}/channels") if (c.get("name") or "").startswith("ticket-")]
    try:
        conv = sorted(convites_por_pessoa().items(), key=lambda x: -x[1])[:10]
    except urllib.error.URLError:
        conv = []
    ranking = sorted(por.items(), key=lambda x: -sum(x[1].values()))[:20]
    dados = {
        "totais": totais,
        "staff": [{"nome": nome_de(u), "total": sum(c.values()), "acoes": c} for u, c in ranking],
        "pendentes": sorted(pendentes, key=lambda p: -p["ts"])[:15],
        "tickets": sorted(tickets, key=lambda t: t["ts"]),
        "wars": wars_abertas(contar=False),
        "convites": [{"nome": nome_de(u), "convites": n} for u, n in conv if n > 0],
        "atualizado": int(time.time())}
    _painel.update(t=time.time(), data=dados)
    return dados


@app.route("/api/staff/painel")
def staff_painel():
    uid = usuario()
    if not uid:
        return privado(jsonify({"ok": False, "motivo": "login"})), 401
    if nivel_uid(uid) < 1:
        return privado(jsonify({"ok": False, "motivo": "staff"})), 403
    try:
        return privado(jsonify(dict(painel_staff_dados(), ok=True, nivel=NOME_NIVEL.get(nivel_uid(uid), ""))))
    except urllib.error.URLError:
        return privado(jsonify({"ok": False, "motivo": "discord"})), 502


@app.route("/api/crew/wars")
def crew_wars():
    bloqueio = sem_acesso_crew()
    if bloqueio:
        return bloqueio
    if not _wars_site["data"] or time.time() - _wars_site["t"] > 60:
        try:
            _wars_site.update(t=time.time(), data=wars_abertas())
        except urllib.error.URLError:
            pass
    return privado(jsonify({"ok": True, "wars": _wars_site["data"] or []}))


def cargos_convite(gid):
    roles = {r.get("name"): r["id"] for r in discord_req("GET", f"/guilds/{gid}/roles")}
    out = []
    for limite_c, nome in CARGOS_CONVITE:
        rid = roles.get(nome) or discord_req("POST", f"/guilds/{gid}/roles", {
            "name": nome, "permissions": "0", "mentionable": False}, "Recompensa por convite")["id"]
        out.append((limite_c, nome, rid))
    return out


def processar_convites(forcar_ranking=False, anunciar=True):
    """Cron: dá os cargos de recrutador e, aos domingos, posta o ranking no #invites."""
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    tot = convites_por_pessoa()
    cargos = cargos_convite(gid)
    novos = []
    for uid, n in sorted(tot.items(), key=lambda x: -x[1]):
        alvo = [(nome, rid) for lim, nome, rid in cargos if n >= lim]
        if not alvo:
            continue
        try:
            tem = set(str(r) for r in discord_req("GET", f"/guilds/{gid}/members/{uid}").get("roles") or [])
        except urllib.error.HTTPError:
            continue  # saiu do servidor
        ganhou = ""
        for nome, rid in alvo:
            if rid not in tem:
                try:
                    discord_req("PUT", f"/guilds/{gid}/members/{uid}/roles/{rid}", motivo=f"{n} convites")
                    ganhou = nome  # fica com o maior cargo novo para um aviso só
                except urllib.error.HTTPError:
                    pass
        if ganhou:
            novos.append((uid, ganhou, n))
        time.sleep(0.2)
    for uid, nome, n in (novos[:10] if anunciar else []):
        try:
            discord_req("POST", f"/channels/{CANAL_INVITES}/messages", {
                "content": f"🎉 <@{uid}> já trouxe **{n} pessoas** para a Kyzen e ganhou o cargo **{nome}**! Valeu, recrutador! ⚓",
                "allowed_mentions": {"users": [uid]}})
        except urllib.error.URLError:
            pass
    agora = datetime.now(BRT)
    if forcar_ranking or (agora.weekday() == 6 and agora.hour >= 18):
        semana = f"{agora.isocalendar()[0]}-S{agora.isocalendar()[1]:02d}"
        ja = any((e.get("footer") or {}).get("text") == f"Ranking de convites · {semana}"
                 for m in discord_req("GET", f"/channels/{CANAL_INVITES}/messages?limit=50") for e in m.get("embeds") or [])
        top = [(u, n) for u, n in sorted(tot.items(), key=lambda x: -x[1]) if n > 0][:10]
        if not ja and top:
            medalha = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
            discord_req("POST", f"/channels/{CANAL_INVITES}/messages", {"embeds": [{
                "title": "🏆 Ranking de convites da Kyzen", "color": 0xF1C40F,
                "description": "\n".join(f"{medalha[i]} <@{u}> · **{n}** convites" for i, (u, n) in enumerate(top))
                               + "\n\n**Recompensas:** " + " · ".join(f"{lim} convites → {nome}" for lim, nome, _ in cargos)
                               + "\nChame os amigos com o seu link de convite e suba no ranking! 🚀",
                "footer": {"text": f"Ranking de convites · {semana}"}}], "allowed_mentions": {"parse": []}})
    return len(novos)


def configurar_invites(gid):
    canais = {c["id"]: c for c in discord_req("GET", f"/guilds/{gid}/channels")}
    cat = canais.get(CAT_INFORMACOES) or {}
    ows = [{k: o[k] for k in ("id", "type", "allow", "deny")} for o in cat.get("permission_overwrites") or []]
    # Invite Tracker (avisa quem entrou pelo convite de quem) e Loritta (boas-vindas) continuam escrevendo aqui
    escreve = str(PERM_VER | PERM_ENVIAR | PERM_LINKS | PERM_ANEXOS | PERM_HIST)
    for r in discord_req("GET", f"/guilds/{gid}/roles"):
        if str((r.get("tags") or {}).get("bot_id")) in ("720351927581278219", "297153970613387264"):
            ows = [o for o in ows if str(o["id"]) != r["id"]] + [{"id": r["id"], "type": 0, "allow": escreve, "deny": "0"}]
    pos = max([c.get("position", 0) for c in canais.values() if c.get("parent_id") == CAT_INFORMACOES] + [0]) + 1
    discord_req("PATCH", f"/channels/{CANAL_INVITES}", {
        "parent_id": CAT_INFORMACOES, "permission_overwrites": ows, "position": pos,
        "topic": "Quem entrou na Kyzen e pelo convite de quem. Traga amigos e ganhe os cargos de Recrutador! 🎟️"},
        "Canal de convites público")
    return "ok"


def configurar_onboarding(gid, max_antes=5, max_total=15):
    roles = {r.get("name"): r["id"] for r in discord_req("GET", f"/guilds/{gid}/roles")}

    def cargo(nome):
        if nome not in roles:
            roles[nome] = discord_req("POST", f"/guilds/{gid}/roles", {"name": nome, "permissions": "0"},
                                      "Cargos do Onboarding")["id"]
        return roles[nome]

    ob = discord_req("GET", f"/guilds/{gid}/onboarding")
    seq = [((int(time.time() * 1000) - 1420070400000) << 22) + 1]

    def nid():
        seq[0] += 1
        return str(seq[0])

    def op(titulo, desc, emo, cargos=(), canais=()):
        return {"id": nid(), "title": titulo, "description": desc, "emoji_name": emo,
                "role_ids": list(cargos), "channel_ids": list(canais)}

    novos = [
        {"id": nid(), "type": 0, "title": "De que lado dos mares você está?", "single_select": True, "required": True,
         "in_onboarding": True, "options": [
             op("Pirata", "Liberdade, saques e aventura", "🏴‍☠️", [cargo("🏴‍☠️ Lado Pirata")]),
             op("Marine", "Ordem, justiça e caça aos piratas", "⚓", [cargo("⚓ Lado Marine")])]},
        {"id": nid(), "type": 0, "title": "Há quanto tempo você navega no Vox Seas?", "single_select": True,
         "required": True, "in_onboarding": True, "options": [
             op("Comecei agora", "Quero dicas para começar bem", "🌱", [cargo("🌱 Novato no Vox")]),
             op("Já jogo faz um tempo", "Conheço o básico do jogo", "⚔️", [cargo("⚔️ Jogador")]),
             op("Sou veterano", "Já domino os mares", "👑", [cargo("👑 Veterano")])]},
        {"id": nid(), "type": 0, "title": "O que você procura na Kyzen?", "single_select": False, "required": False,
         "in_onboarding": True, "options": [
             op("Entrar numa crew", "Quero uma tripulação para jogar e disputar wars", "⚔️",
                canais=["1398726258157949148"]),
             op("Eventos e torneios", "Quero ser avisado dos eventos", "📅", ["1569202090482335784"],
                ["1503249256427950141"]),
             op("Jogar a Alfacusa", "A cidade dentro do Discord", "🏙️",
                canais=["1572032026339446824", "1572032027560120490"]),
             op("Criar conteúdo", "Faço vídeos ou lives", "🎥", [cargo("🎥 Criador de Conteúdo (interesse)")],
                ["1400120211516162220"])]},
    ]

    def normal(p):
        opcoes = []
        for o in p.get("options") or []:
            e = o.get("emoji") or {}
            opcoes.append({k: v for k, v in {
                "id": o.get("id"), "title": o.get("title"), "description": o.get("description"),
                "role_ids": o.get("role_ids") or [], "channel_ids": o.get("channel_ids") or [],
                "emoji_id": o.get("emoji_id") or e.get("id"), "emoji_name": o.get("emoji_name") or e.get("name"),
                "emoji_animated": o.get("emoji_animated") or e.get("animated")}.items() if v is not None})
        return {"id": p.get("id"), "type": p.get("type", 0), "title": p.get("title"), "options": opcoes,
                "single_select": p.get("single_select", False), "required": p.get("required", False),
                "in_onboarding": p.get("in_onboarding", True)}

    existentes = {c["id"] for c in discord_req("GET", f"/guilds/{gid}/channels")}
    removidos = set()

    def limpar_prompt(p):
        """Tira das respostas os canais que foram apagados; resposta sem cargo nem canal sai."""
        ops = []
        for o in p["options"]:
            removidos.update(c for c in o.get("channel_ids") or [] if c not in existentes)
            o["channel_ids"] = [c for c in o.get("channel_ids") or [] if c in existentes]
            if o["channel_ids"] or o.get("role_ids"):
                ops.append(o)
        p["options"] = ops
        return p

    atuais = [limpar_prompt(normal(p)) for p in ob.get("prompts") or []]
    atuais = [p for p in atuais if p["options"]]
    titulos = {p["title"] for p in atuais}
    adicionadas = [p["title"] for p in novos if p["title"] not in titulos]
    prompts = ([p for p in novos if p["title"] not in titulos] + atuais)[:max_total]
    # O Discord mostra no máximo 5 perguntas antes de entrar; as outras ficam em "Canais e cargos"
    antes = 0
    for p in prompts:
        if p.get("in_onboarding"):
            antes += 1
            if antes > max_antes:
                p["in_onboarding"] = False
    # Canais padrão: só os que existem; completa com canais públicos até o mínimo de 7
    padrao = [c for c in ob.get("default_channel_ids") or [] if c in existentes]
    removidos.update(c for c in ob.get("default_channel_ids") or [] if c not in existentes)
    for c in ("1383949963846877244", "1383990514701041836", "1383947381032095846", "1383957309071560725",
              "1383957562696929342", "1384921966254882958", "1572032026339446824", "1398726258157949148",
              "1503249256427950141"):
        if len(padrao) >= 7:
            break
        if c in existentes and c not in padrao:
            padrao.append(c)
    contagem = {"existentes": len(atuais), "total_enviado": len(prompts),
                "antes_de_entrar": sum(1 for p in prompts if p.get("in_onboarding"))}
    try:
        discord_req("PUT", f"/guilds/{gid}/onboarding", {
            "prompts": prompts, "default_channel_ids": padrao,
            "enabled": ob.get("enabled", True), "mode": ob.get("mode", 0)}, "Perguntas do Onboarding")
    except urllib.error.HTTPError as e:
        return dict(contagem, erro=f"{e.code} {e.read().decode('utf-8', 'replace')[:200]}")
    return {"perguntas_novas": adicionadas, "total_perguntas": len(prompts),
            "antes_de_entrar": [p["title"] for p in prompts if p.get("in_onboarding")],
            "depois_de_entrar": [p["title"] for p in prompts if not p.get("in_onboarding")],
            "canais_apagados_removidos": len(removidos), "canais_padrao": len(padrao)}


FORUM_SETS_NOME = "📚・sets-e-itens"
FORUM_SETS_TAGS = [("Set", "🧥"), ("Craft", "⚒️"), ("Drop de Boss", "👹"), ("NPC de outra ilha", "🧭"),
                   ("Material", "🪨"), ("Dúvida", "❓")]
FORUM_SETS_REGRAS = (
    "Divulgue aqui os itens importantes dos sets dos bosses: o que precisa, como craftar e quem dropa.\n\n"
    "• Um post por item ou set, com o nome do item no título\n"
    "• Só itens importantes: os que precisam de craft, de boss ou de NPC de outra ilha. "
    "O que dropa do NPC da própria ilha não precisa, porque é fácil de achar\n"
    "• Use as etiquetas e, se puder, mande um print\n"
    "• Antes de postar, veja se o item já não tem post (use a busca)")
FORUM_SETS_GUIA = (
    "Use este modelo no seu post (copie e preencha):\n\n"
    "```\n"
    "🧥 Set: \n"
    "🎯 Item: \n"
    "⚒️ Como conseguir: (craft / drop de boss / NPC)\n"
    "🪨 Materiais: \n"
    "🗺️ Onde: (ilha, boss ou NPC)\n"
    "💡 Dica: \n"
    "```\n"
    "📸 Anexe um print se tiver.\n\n"
    "**Vale postar:** item que precisa de craft, que dropa de boss ou de NPC de outra ilha.\n"
    "**Não precisa:** o que dropa do NPC da própria ilha (é fácil de achar).\n\n"
    "🔎 Antes de postar, use a busca do fórum para ver se o item já tem post.\n"
    "📖 O **Guia** do site também tem o \"Onde dropa?\" com os drops do Trello oficial: "
    "https://kyzensaofc.vercel.app/#guia")


def criar_forum_sets(gid):
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    existente = next((c for c in canais if c.get("type") == 15 and c.get("name") == FORUM_SETS_NOME), None)
    if existente:
        return {"forum": existente["id"], "criado": False}
    cat = next(c for c in canais if c["id"] == "1383947381032095844")  # 🌎 Geral
    ows = [{k: o[k] for k in ("id", "type", "allow", "deny")} for o in cat.get("permission_overwrites") or []]
    # todo mundo pode criar post, responder e mandar print aqui
    pode = PERM_VER | PERM_ENVIAR | PERM_HIST | PERM_ANEXOS | PERM_LINKS | (1 << 6) | (1 << 38)
    eo = next((o for o in ows if str(o["id"]) == gid), None)
    if eo is None:
        eo = {"id": gid, "type": 0, "allow": "0", "deny": "0"}
        ows.append(eo)
    eo["allow"] = str(int(eo.get("allow") or 0) | pode)
    eo["deny"] = str(int(eo.get("deny") or 0) & ~pode)
    forum = discord_req("POST", f"/guilds/{gid}/channels", {
        "name": FORUM_SETS_NOME, "type": 15, "parent_id": "1383947381032095844", "permission_overwrites": ows,
        "topic": FORUM_SETS_REGRAS, "rate_limit_per_user": 30, "default_thread_rate_limit_per_user": 3,
        "default_reaction_emoji": {"emoji_name": "👍"}, "default_sort_order": 0, "default_forum_layout": 1,
        "available_tags": [{"name": n, "emoji_name": e} for n, e in FORUM_SETS_TAGS]},
        "Fórum de sets e itens (sugestão da comunidade)")
    post = discord_req("POST", f"/channels/{forum['id']}/threads", {
        "name": "📌 Leia antes de postar: como divulgar um item",
        "message": {"embeds": [{"title": "📌 Como divulgar um item de set", "color": COR,
                                "description": FORUM_SETS_GUIA,
                                "footer": {"text": "Kyzen SA · ideia sugerida pela comunidade"}}],
                    "allowed_mentions": {"parse": []}}}, "Guia do fórum de sets")
    try:
        discord_req("PATCH", f"/channels/{post['id']}", {"flags": 2, "locked": True}, "Fixa o guia do fórum")
    except urllib.error.HTTPError:
        pass
    try:  # exige pelo menos uma etiqueta em cada post (fica mais fácil de filtrar)
        discord_req("PATCH", f"/channels/{forum['id']}", {"flags": 16}, "Exige etiqueta nos posts")
    except urllib.error.HTTPError:
        pass
    _canais_cache["t"] = 0
    return {"forum": forum["id"], "post_guia": post["id"], "criado": True}


PING_VOX = "1446300736454529114"
AVISO_FORUM_SETS = f"""<@&{PING_VOX}>

# 📚 NOVO CANAL: SETS E ITENS DOS BOSSES!

A pedido da comunidade, criamos o <#1572317292728160367>, um fórum só para divulgar os **itens importantes dos sets**: o que precisa, **como craftar** e **qual boss ou NPC de outra ilha dropa**.

• Cada item vira um post, fácil de achar pela busca
• Use as etiquetas (Set, Craft, Drop de Boss, NPC de outra ilha...)
• Antes de postar, leia o guia fixado e use o modelo

Ajude a tripulação: quem descobrir, posta! 🏴‍☠️"""


def avisar_forum_sets():
    for m in discord_req("GET", "/channels/1383990514701041836/messages?limit=30"):
        if "1572317292728160367" in (m.get("content") or ""):
            return {"enviou": False, "motivo": "já foi avisado", "id": m.get("id")}
    r = discord_req("POST", "/channels/1383990514701041836/messages", {
        "content": AVISO_FORUM_SETS, "allowed_mentions": {"roles": [PING_VOX]}})
    return {"enviou": True, "id": r.get("id")}


TROCAS_NOME = "🔄・trocas"
TROCAS_TOPICO = ("Trocas de frutas e itens do Vox Seas. Formato: o que você TEM 🔁 o que você QUER. "
                 "Sem golpe, sem spam, sem venda por dinheiro real. Golpe? Abra um ticket com print.")
TROCAS_GUIA = (
    "Aqui é o lugar para **trocar frutas, espadas e itens do Vox Seas** com a galera. 🏴‍☠️\n\n"
    "**📝 Como anunciar**\n"
    "Mande numa mensagem só: **o que você tem 🔁 o que você quer**\n"
    "> `Tenho: Fruta X + Fruta Y 🔁 Quero: Fruta Z`\n"
    "> `Procurando: Espada X, pago com frutas`\n"
    "> `Vendo: Fruta X por 100K 💰`\n"
    "Pode usar emoji e print do item. Quem tiver Nitro pode usar os emojis de fruta do server oficial.\n\n"
    "**📌 Regras**\n"
    "• Um anúncio a cada pouco tempo (o canal tem modo lento), nada de flood\n"
    "• Combine os detalhes no privado ou numa thread da mensagem\n"
    "• **Proibido vender ou comprar com dinheiro real ou Robux fora do jogo**\n"
    "• Nada de golpe: quem aplicar golpe é **banido**\n\n"
    "**🛡️ Dicas para não cair em golpe**\n"
    "• Faça a troca **pela janela de trade do jogo** e confira tudo antes de aceitar\n"
    "• Desconfie de oferta boa demais e de quem pede para você entregar primeiro\n"
    "• Caiu em golpe? Abra um ticket em <#1398749616832254002> com prints da conversa\n\n"
    "Boas trocas! 🔁")


AVISO_TROCAS = f"""<@&{PING_VOX}>

# 🔄 NOVO CANAL: TROCAS!

Agora a Kyzen tem o <#1572371715051495525>, o lugar certo para **trocar frutas, espadas e itens do Vox Seas** com a galera!

• Anuncie assim: **o que você tem 🔁 o que você quer**
• Pode mandar print e usar emojis (com Nitro dá para usar os de fruta do server oficial)
• Negocie no privado ou numa thread da mensagem
• Leia o guia fixado antes de postar

🛡️ Troque sempre pela janela de trade do jogo. Caiu em golpe? Abra um ticket com prints, e quem aplica golpe é **banido**.

Boas trocas, marujos! 🏴‍☠️"""


def avisar_trocas():
    for m in discord_req("GET", "/channels/1383990514701041836/messages?limit=30"):
        if "1572371715051495525" in (m.get("content") or ""):
            return {"enviou": False, "motivo": "já foi avisado", "id": m.get("id")}
    r = discord_req("POST", "/channels/1383990514701041836/messages", {
        "content": AVISO_TROCAS, "allowed_mentions": {"roles": [PING_VOX]}})
    return {"enviou": True, "id": r.get("id")}


def canais_resumo(gid):
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    cats = {c["id"]: c["name"] for c in canais if c.get("type") == 4}
    out = {}
    for c in sorted(canais, key=lambda c: c.get("position", 0)):
        if c.get("type") != 4:
            out.setdefault(cats.get(c.get("parent_id"), "(sem categoria)") + f" [{c.get('parent_id')}]", []).append(
                f"{c['name']} ({c['type']})")
    return out


def criar_trocas(gid, parent):
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    existente = next((c for c in canais if c.get("name") == TROCAS_NOME), None)
    if existente:
        return {"canal": existente["id"], "criado": False}
    cat = next(c for c in canais if c["id"] == parent)
    ows = [{k: o[k] for k in ("id", "type", "allow", "deny")} for o in cat.get("permission_overwrites") or []]
    pode = PERM_VER | PERM_ENVIAR | PERM_HIST | PERM_ANEXOS | PERM_LINKS | (1 << 6) | (1 << 18) | (1 << 38)
    eo = next((o for o in ows if str(o["id"]) == gid), None)
    if eo is None:
        eo = {"id": gid, "type": 0, "allow": "0", "deny": "0"}
        ows.append(eo)
    if not int(eo.get("deny") or 0) & PERM_VER:  # só libera escrever se a categoria já é visível para todos
        eo["allow"] = str(int(eo.get("allow") or 0) | pode)
        eo["deny"] = str(int(eo.get("deny") or 0) & ~pode)
    canal = discord_req("POST", f"/guilds/{gid}/channels", {
        "name": TROCAS_NOME, "type": 0, "parent_id": parent, "permission_overwrites": ows,
        "topic": TROCAS_TOPICO, "rate_limit_per_user": 120}, "Canal de trocas de frutas e itens")
    msg = discord_req("POST", f"/channels/{canal['id']}/messages", {
        "embeds": [{"title": "🔄 Trocas de frutas e itens", "color": COR, "description": TROCAS_GUIA,
                    "footer": {"text": "Kyzen SA · troque com responsabilidade"}}],
        "allowed_mentions": {"parse": []}})
    try:
        discord_req("PUT", f"/channels/{canal['id']}/pins/{msg['id']}", None, "Fixa o guia de trocas")
    except urllib.error.HTTPError:
        pass
    _canais_cache["t"] = 0
    return {"canal": canal["id"], "criado": True}


@app.route("/api/staff-acao", methods=["POST"])
def staff_acao():
    """Staff (DEBUG_KEY): ações de configuração do servidor, sem precisar de rota temporária."""
    if not debug_ok():
        return jsonify({"ok": False}), 404
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    acao = request.args.get("acao", "")
    try:
        if acao == "invites":
            return privado(jsonify({"ok": True, "invites": configurar_invites(gid)}))
        if acao == "convites":
            return privado(jsonify({"ok": True, "pessoas_premiadas": processar_convites(forcar_ranking=True,
                                                                                          anunciar=False)}))
        if acao == "aviso-forum-sets":
            return privado(jsonify(dict(avisar_forum_sets(), ok=True)))
        if acao == "forum-sets":
            return privado(jsonify(dict(criar_forum_sets(gid), ok=True)))
        if acao in ("voz", "voz-ver"):
            regioes = [r["id"] for r in discord_req("GET", "/voice/regions")]
            alvo = request.args.get("regiao", "brazil")
            if alvo not in regioes:
                return privado(jsonify({"ok": False, "erro": "região inválida", "regioes": regioes}))
            antes, mudou = {}, []
            for c in discord_req("GET", f"/guilds/{gid}/channels"):
                if c.get("type") not in (2, 13):
                    continue
                antes[c.get("name")] = {"regiao": c.get("rtc_region") or "automático", "bitrate": c.get("bitrate")}
                if acao == "voz" and c.get("rtc_region") != alvo:
                    try:
                        discord_req("PATCH", f"/channels/{c['id']}", {"rtc_region": alvo}, "Região de voz no Brasil")
                        mudou.append(c.get("name"))
                    except urllib.error.HTTPError as e:
                        mudou.append(f"{c.get('name')}: erro {e.code}")
                    time.sleep(0.4)
            return privado(jsonify({"ok": True, "antes": antes, "mudou": mudou, "regioes": regioes}))
        if acao == "raids":
            return privado(jsonify(dict(criar_raids(gid), ok=True)))
        if acao == "msgs":  # leitura: últimas mensagens de um canal (autor, ping e título), sem dados pessoais
            cid = request.args.get("canal", "")
            if cid and not cid.isdigit():  # aceita parte do nome do canal
                cid = next((c["id"] for c in discord_req("GET", f"/guilds/{gid}/channels")
                            if cid in c.get("name", "")), "")
            if not cid.isdigit():
                return privado(jsonify({"ok": False, "erro": "canal inválido"}))
            out = []
            for m in discord_req("GET", f"/channels/{cid}/messages?limit={min(int(request.args.get('n') or 10), 50)}"):
                a = m.get("author") or {}
                out.append({"quando": m.get("timestamp", "")[:16], "autor": a.get("username"), "bot": a.get("bot", False),
                            "app": a.get("id") if a.get("bot") else None,
                            "texto": (m.get("content") or "")[:120], "cargos_marcados": m.get("mention_roles"),
                            "embeds": [(e.get("title") or (e.get("author") or {}).get("name") or "")[:80]
                                       for e in m.get("embeds") or []]})
            return privado(jsonify({"ok": True, "msgs": out}))
        if acao == "raids-mover":
            return privado(jsonify(dict(mover_raids(gid), ok=True)))
        if acao == "aviso-raids":
            return privado(jsonify(dict(avisar_raids(gid, request.args.get("ping") == "1"), ok=True)))
        if acao == "aviso-novabux":
            return privado(jsonify(dict(avisar_novabux(), ok=True)))
        if acao == "aviso-trocas":
            return privado(jsonify(dict(avisar_trocas(), ok=True)))
        if acao == "canais":
            return privado(jsonify({"ok": True, "canais": canais_resumo(gid)}))
        if acao == "trocas":
            return privado(jsonify(dict(criar_trocas(gid, request.args.get("cat", "")), ok=True)))
        if acao == "membro-buscar":  # acha alguém pelo nome, para dar cargo próprio
            q = urllib.parse.quote(request.args.get("q", "")[:32])
            achados = [{"id": m["user"]["id"], "usuario": m["user"].get("username"),
                        "nome": m["user"].get("global_name"), "apelido": m.get("nick"),
                        "entrou": m.get("joined_at"), "cargos": len(m.get("roles") or [])}
                       for m in discord_req("GET", f"/guilds/{gid}/members/search?query={q}&limit=10")]
            g = discord_req("GET", f"/guilds/{gid}")
            return privado(jsonify({"ok": True, "achados": achados, "boost": g.get("premium_tier"),
                                    "recursos": [f for f in g.get("features", []) if "ROLE" in f]}))
        if acao == "cargo-proprio":  # cargo exclusivo e em destaque para um membro (ex.: criador de conteúdo)
            uid, nome = request.args.get("uid", ""), request.args.get("nome", "")[:100]
            if not uid.isdigit() or not nome:
                return privado(jsonify({"ok": False, "erro": "falta uid ou nome"}))
            cor = int(request.args.get("cor", "FF0000"), 16)
            cor2 = request.args.get("cor2", "")
            emoji = request.args.get("emoji", "")
            abaixo = request.args.get("abaixo", "1497068096450465792")  # padrão: logo abaixo do Helper
            g = discord_req("GET", f"/guilds/{gid}")
            feats = g.get("features", [])
            corpo = {"name": nome, "color": cor, "hoist": True, "mentionable": False, "permissions": "0"}
            if cor2 and "ENHANCED_ROLE_COLORS" in feats:
                corpo["colors"] = {"primary_color": cor, "secondary_color": int(cor2, 16)}
            if emoji and "ROLE_ICONS" in feats:
                corpo["unicode_emoji"] = emoji
            existente = next((r for r in g.get("roles", []) if r.get("name") == nome), None)
            if existente:
                cargo = discord_req("PATCH", f"/guilds/{gid}/roles/{existente['id']}", corpo, "Cargo próprio")
            else:
                cargo = discord_req("POST", f"/guilds/{gid}/roles", corpo, "Cargo próprio")
            ref = next((r for r in g.get("roles", []) if r["id"] == abaixo), None)
            posicao = None
            if ref:
                posicao = max(1, ref["position"])  # entra logo abaixo do cargo de referência
                try:
                    discord_req("PATCH", f"/guilds/{gid}/roles", [{"id": cargo["id"], "position": posicao}],
                                "Cargo próprio em destaque")
                except urllib.error.HTTPError as e:
                    posicao = f"erro {e.code}"
            discord_req("PUT", f"/guilds/{gid}/members/{uid}/roles/{cargo['id']}", None, "Cargo próprio")
            return privado(jsonify({"ok": True, "cargo": cargo["id"], "posicao": posicao,
                                    "gradiente": "colors" in corpo, "icone": "unicode_emoji" in corpo}))
        if acao == "onboarding":
            r = configurar_onboarding(gid, int(request.args.get("antes") or 5), int(request.args.get("total") or 15))
            return privado(jsonify(dict(r, ok="erro" not in r)))
        return privado(jsonify({"ok": False, "erro": "ação inválida"}))
    except urllib.error.HTTPError as e:
        return privado(jsonify({"ok": False, "acao": acao, "erro": f"{e.code} {e.read().decode('utf-8', 'replace')[:300]}"}))


def interacao_moderacao(d, tipo, dados, nome, opcoes, uid):
    """Comandos de moderação e denúncia. Devolve None se não for um deles."""
    gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", "")).strip()
    membro = d.get("member") or {}
    resolv = dados.get("resolved") or {}

    if tipo == 2 and nome == "denunciar":
        return cmd_denunciar()
    if tipo == 2 and nome == "ajuda":
        return cmd_ajuda(nivel(membro) >= 1)
    if tipo == 2 and nome == "Denunciar mensagem":
        m = (resolv.get("messages") or {}).get(str(dados.get("target_id"))) or {}
        return cmd_denunciar(f"https://discord.com/channels/{gid}/{m.get('channel_id')}/{m.get('id')}" if m else "")

    staff_cmd = (tipo == 2 and nome in ("Advertir membro", "Advertir autor", "advertir", "historico", "perdoar")) or \
                (tipo == 5 and str(dados.get("custom_id", "")).startswith("adv:"))
    if not staff_cmd:
        return None
    n_staff = nivel(membro)
    if n_staff < 1:
        return resposta(texto="Só a staff pode usar isso.", privada=True, componentes=[])

    if tipo == 2 and nome == "Advertir membro":
        alvo_id = str(dados.get("target_id"))
        alvo = (resolv.get("users") or {}).get(alvo_id)
        erro = checar_alvo(uid, alvo, (resolv.get("members") or {}).get(alvo_id), n_staff)
        if erro:
            return resposta(texto=erro, privada=True, componentes=[])
        return modal_advertir(alvo_id, alvo.get("global_name") or alvo.get("username") or "membro")
    if tipo == 2 and nome == "Advertir autor":
        m = (resolv.get("messages") or {}).get(str(dados.get("target_id"))) or {}
        alvo = m.get("author") or {}
        erro = checar_alvo(uid, alvo, (resolv.get("members") or {}).get(str(alvo.get("id"))), n_staff)
        if erro:
            return resposta(texto=erro, privada=True, componentes=[])
        prova = f"https://discord.com/channels/{gid}/{m.get('channel_id')}/{m.get('id')}"
        if m.get("content"):
            prova += "\n> " + cortar(m["content"].replace("\n", " "), 400)
        return modal_advertir(str(alvo["id"]), alvo.get("global_name") or alvo.get("username") or "membro", prova)
    if tipo == 5:
        alvo_id = str(dados["custom_id"]).split(":", 1)[1]
        valores = {c.get("custom_id"): (c.get("value") or "").strip()
                   for linha in dados.get("components") or [] for c in linha.get("components") or []}
        if not re.fullmatch(r"\d{5,25}", alvo_id) or not valores.get("motivo"):
            return resposta(texto="Faltou o motivo.", privada=True, componentes=[])
        verbal = slug_py(valores.get("verbal", "")).startswith("s")
        return advertir(gid, uid, alvo_id, alvo_id, valores["motivo"], valores.get("prova", ""), verbal,
                        nivel_staff=n_staff)

    alvo_id = str(opcoes.get("membro") or "")
    alvo = (resolv.get("users") or {}).get(alvo_id)
    if nome == "advertir":
        erro = checar_alvo(uid, alvo, (resolv.get("members") or {}).get(alvo_id), n_staff)
        if erro:
            return resposta(texto=erro, privada=True, componentes=[])
        return advertir(gid, uid, alvo_id, alvo.get("global_name") or alvo.get("username") or alvo_id,
                        str(opcoes.get("motivo") or ""), str(opcoes.get("prova") or ""), opcoes.get("tipo") == "verbal",
                        nivel_staff=n_staff)
    if nome == "historico":
        return cmd_historico(alvo_id)
    if nome == "perdoar":
        if n_staff < 4:
            return so_a_partir(4)
        return cmd_perdoar(gid, uid, alvo_id, str(opcoes.get("motivo") or ""))
    return None


@app.route("/api/interacoes", methods=["POST"])
def interacoes():
    try:
        request.max_content_length = 256 * 1024  # comandos podem ser maiores que o limite de 16 KB do site
    except AttributeError:
        pass
    if not assinatura_ok():
        return "assinatura inválida", 401
    d = request.get_json(silent=True) or {}
    tipo = d.get("type")
    if tipo == 1:  # PING do Discord
        return jsonify({"type": 1})
    dados = d.get("data") or {}
    nome = dados.get("name")
    opcoes = {o.get("name"): o.get("value") for o in dados.get("options") or []}
    uid = str(((d.get("member") or {}).get("user") or d.get("user") or {}).get("id") or "")
    if tipo == 4:  # sugestões enquanto a pessoa digita
        q = slug_py(str(opcoes.get("nome") or opcoes.get("busca") or ""))
        if nome == "fruta":
            lista = [c["nome"] for c in frutas() if q in slug_py(c["nome"])][:25]
        elif nome == "drop":
            g = guia_cache() or {"chefes": [], "itens": []}
            lista = ([c["nome"] for c in g["chefes"] if q in slug_py(c["nome"])] +
                     [i["nome"] for i in g["itens"] if q in slug_py(i["nome"])])[:25]
        else:
            lista = []
        return jsonify({"type": 8, "data": {"choices": [{"name": n[:100], "value": n[:100]} for n in lista]}})
    resultado = {}

    @copy_current_request_context
    def rodar():
        try:
            resultado["r"] = _tratar_interacao(d, tipo, dados, nome, opcoes, uid)
        except Exception as e:  # noqa: BLE001
            resultado["erro"] = e

    t = threading.Thread(target=rodar, daemon=True)
    t.start()
    t.join(2.2)
    if not t.is_alive():
        if "erro" in resultado:
            app.logger.error("interação %s: %s", nome, str(resultado["erro"])[:160])
            return resposta(texto="Algo deu errado. Tente de novo em instantes.", privada=True, componentes=[])
        return resultado["r"]
    # Passou de ~2 s: avisa o Discord que está processando e termina depois (o token vale 15 minutos)
    adiado = adiar_interacao(d, tipo, nome)
    t.join(60)
    terminar_interacao(d, adiado, resultado)
    return "", 202


# ---------------------------------------------------------------------------
# Raids e bosses: canal da crew, cargo de ping e /raid com lista de quem vai
# (sugestão da comunidade)
# ---------------------------------------------------------------------------
RAID_CANAL_NOME = "⚔️・raids-e-bosses"
RAID_CARGO_NOME = "⚔️ Caçador de Raids"
RAID_CAT = "1383947381032095844"  # 🌎 Geral (aberto para todos)
RAID_COR = 0xE8590C
_raid_ids = {"t": 0, "canal": None, "cargo": None}
RAID_GUIA = (
    "Este canal é **só para chamar a galera para raid e boss**, assim ninguém se perde no meio do chat. 🏴‍☠️\n\n"
    "**🔔 Quer ser chamado?**\n"
    "Clique no botão **🔔 Quero ser chamado** aqui embaixo e ganhe o cargo **{cargo}**. "
    "Toda vez que alguém chamar uma raid ou um boss, você recebe o ping e não perde mais nenhum drop.\n"
    "Cansou? Clique de novo e o cargo sai.\n\n"
    "**⚔️ Como chamar a galera**\n"
    "Use o comando **`/raid`** aqui no canal:\n"
    "> `/raid boss: Nome do boss ou raid` `vagas: 4` `como_entrar: servidor privado / link / hora`\n"
    "O bot marca todo mundo que tem o cargo e cria um card com o botão **✋ Tô dentro**, "
    "mostrando quem vai e quantas vagas sobram.\n\n"
    "**📌 Regras**\n"
    "• Só chamadas de raid e boss aqui. Conversa vai no chat geral\n"
    "• Nada de ping falso ou chamar sem estar indo de verdade\n"
    "• Dividam os drops como combinaram antes de começar\n\n"
    "Bora caçar! ⚔️")


def raid_ids(gid):
    """IDs do canal e do cargo de raids (procura pelo nome e guarda por 10 min)."""
    if time.time() - _raid_ids["t"] < 600 and _raid_ids["canal"]:
        return _raid_ids["canal"], _raid_ids["cargo"]
    canal = next((c["id"] for c in discord_req("GET", f"/guilds/{gid}/channels")
                  if c.get("name") == RAID_CANAL_NOME), None)
    cargo = next((r["id"] for r in discord_req("GET", f"/guilds/{gid}/roles")
                  if r.get("name") == RAID_CARGO_NOME), None)
    _raid_ids.update(t=time.time(), canal=canal, cargo=cargo)
    return canal, cargo


def criar_raids(gid):
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    cargos = discord_req("GET", f"/guilds/{gid}/roles")
    cargo = next((r for r in cargos if r.get("name") == RAID_CARGO_NOME), None)
    if not cargo:
        cargo = discord_req("POST", f"/guilds/{gid}/roles", {
            "name": RAID_CARGO_NOME, "color": RAID_COR, "hoist": False, "mentionable": True, "permissions": "0"},
            "Cargo de ping de raids e bosses")
    canal = next((c for c in canais if c.get("name") == RAID_CANAL_NOME), None)
    criado = False
    if not canal:
        cat = next(c for c in canais if c["id"] == RAID_CAT)
        ows = [{k: o[k] for k in ("id", "type", "allow", "deny")} for o in cat.get("permission_overwrites") or []]
        chat_crew = next((c for c in canais if c.get("parent_id") == RAID_CAT and "chat-crew" in c.get("name", "")),
                         None)
        canal = discord_req("POST", f"/guilds/{gid}/channels", {
            "name": RAID_CANAL_NOME, "type": 0, "parent_id": RAID_CAT, "permission_overwrites": ows,
            "topic": "Só chamadas de raid e boss. Use /raid para chamar e pegue o cargo "
                     f"{RAID_CARGO_NOME} na mensagem fixada para receber o ping.",
            "rate_limit_per_user": 10,
            "position": (chat_crew or {}).get("position", 0) + 1}, "Canal de raids e bosses (sugestão da comunidade)")
        criado = True
        msg = discord_req("POST", f"/channels/{canal['id']}/messages", {
            "embeds": [{"title": "⚔️ Raids e Bosses da Kyzen", "color": RAID_COR,
                        "description": RAID_GUIA.format(cargo=f"<@&{cargo['id']}>"),
                        "footer": {"text": "Kyzen SA · ideia sugerida pela comunidade"}}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "🔔 Quero ser chamado", "custom_id": f"cg:{cargo['id']}"}]}],
            "allowed_mentions": {"parse": []}})
        try:
            discord_req("PUT", f"/channels/{canal['id']}/pins/{msg['id']}", None, "Fixa o guia de raids")
        except urllib.error.HTTPError:
            pass
    _raid_ids.update(t=time.time(), canal=canal["id"], cargo=cargo["id"])
    _canais_cache["t"] = 0
    return {"canal": canal["id"], "cargo": cargo["id"], "criado": criado}


def mover_raids(gid):
    """Leva o canal de raids para o Geral, aberto para todos, e atualiza o guia fixado."""
    canal, cargo = raid_ids(gid)
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    cat = next(c for c in canais if c["id"] == RAID_CAT)
    ows = [{k: o[k] for k in ("id", "type", "allow", "deny")} for o in cat.get("permission_overwrites") or []]
    pode = PERM_VER | PERM_ENVIAR | PERM_HIST | PERM_ANEXOS | PERM_LINKS | (1 << 6) | (1 << 38)
    eo = next((o for o in ows if str(o["id"]) == gid), None)
    if eo is None:
        eo = {"id": gid, "type": 0, "allow": "0", "deny": "0"}
        ows.append(eo)
    eo["allow"] = str(int(eo.get("allow") or 0) | pode)
    eo["deny"] = str(int(eo.get("deny") or 0) & ~pode)
    geral = [c for c in canais if c.get("parent_id") == RAID_CAT]
    trocas = next((c for c in geral if c.get("name") == TROCAS_NOME), None)
    discord_req("PATCH", f"/channels/{canal}", {
        "parent_id": RAID_CAT, "permission_overwrites": ows,
        "position": (trocas or max(geral, key=lambda c: c.get("position", 0))).get("position", 0) + 1},
        "Canal de raids aberto para todos")
    editou = None
    for m in discord_req("GET", f"/channels/{canal}/pins"):
        if (m.get("embeds") or [{}])[0].get("title") == "⚔️ Raids e Bosses da Kyzen":
            e = m["embeds"][0]
            e["description"] = RAID_GUIA.format(cargo=f"<@&{cargo}>")
            discord_req("PATCH", f"/channels/{canal}/messages/{m['id']}", {"embeds": [e]})
            editou = m["id"]
    _canais_cache["t"] = 0
    return {"canal": canal, "guia_editado": editou}


def avisar_raids(gid, ping=False):
    canal, cargo = raid_ids(gid)
    canais = discord_req("GET", f"/guilds/{gid}/channels")
    anuncios = next(c["id"] for c in canais if c.get("parent_id") == RAID_CAT and "anúncios-crew" in c.get("name", ""))
    for m in discord_req("GET", f"/channels/{anuncios}/messages?limit=30"):
        if canal in (m.get("content") or ""):
            return {"enviou": False, "motivo": "já foi avisado", "id": m.get("id")}
    texto = (("@everyone\n\n" if ping else "") + f"# ⚔️ NOVO CANAL: RAIDS E BOSSES!\n\n"
             f"A pedido da tripulação, agora a crew tem o <#{canal}>, um canal **só para chamar raid e boss**. "
             "Nada de chamada perdida no meio do chat! 🏴‍☠️\n\n"
             f"🔔 **Clique no botão aqui embaixo** e ganhe o cargo <@&{cargo}>. Toda vez que alguém chamar "
             "uma raid ou um boss, você é avisado na hora e **não perde mais nenhum drop**.\n\n"
             "⚔️ Para chamar a galera, use **`/raid`** no canal. O bot marca os caçadores e monta a lista com "
             "o botão **✋ Tô dentro**.\n\n"
             "Quem tem o cargo chega primeiro, pega mais drop e evolui mais rápido. Vai ficar de fora? 😏")
    r = discord_req("POST", f"/channels/{anuncios}/messages", {
        "content": texto, "allowed_mentions": {"parse": ["everyone"] if ping else []},
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "🔔 Quero ser chamado", "custom_id": f"cg:{cargo}"}]}]})
    return {"enviou": True, "id": r.get("id"), "canal_aviso": anuncios}


COMANDOS += [
    {"name": "raid", "description": "Chamar a galera para uma raid ou boss (no canal de raids)",
     "options": [{"type": 3, "name": "boss", "description": "Qual raid ou boss", "required": True, "max_length": 80},
                 {"type": 4, "name": "vagas", "description": "Quantas pessoas precisa (opcional)",
                  "required": False, "min_value": 1, "max_value": 30},
                 {"type": 3, "name": "como_entrar", "description": "Servidor, link ou horário (opcional)",
                  "required": False, "max_length": 200}]},
]


def _raid_card(boss, dono, vagas, entrar, gente):
    linhas = "\n".join(f"• <@{u}>" for u in gente) or "Ninguém ainda. Seja o primeiro!"
    total = f"{len(gente)}/{vagas}" if vagas else str(len(gente))
    desc = f"**Quem chamou:** <@{dono}>\n"
    if entrar:
        desc += f"**Como entrar:** {entrar}\n"
    cheio = bool(vagas) and len(gente) >= vagas
    return {"title": f"⚔️ {boss}", "color": 0x6C757D if cheio else RAID_COR,
            "description": desc + f"\n**✋ Quem vai ({total}):**\n{linhas}"
                           + ("\n\n🔒 **Vagas preenchidas!**" if cheio else ""),
            "footer": {"text": f"vagas:{vagas or 0} · Kyzen SA · raids e bosses"}}


def interacao_raid(d, tipo, dados, nome, opcoes, uid):
    gid = str(d.get("guild_id") or os.environ.get("DISCORD_GUILD_ID", "")).strip()
    if tipo == 2 and nome == "raid":
        canal, cargo = raid_ids(gid)
        if not canal or str(d.get("channel_id")) != canal:
            return resposta(texto=f"Use o `/raid` no canal <#{canal}>." if canal else "O canal de raids ainda não existe.",
                            privada=True, componentes=[])
        boss = cortar(str(opcoes.get("boss") or "Raid"), 80)
        vagas = int(opcoes.get("vagas") or 0)
        entrar = cortar(str(opcoes.get("como_entrar") or ""), 200)
        return jsonify({"type": 4, "data": {
            "content": f"<@&{cargo}> **{boss}** chamando! Quem vem? ⚔️" if cargo else f"**{boss}** chamando!",
            "allowed_mentions": {"roles": [cargo] if cargo else []},
            "embeds": [_raid_card(boss, uid, vagas, entrar, [uid])],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "✋ Tô dentro", "custom_id": "rd:in"},
                {"type": 2, "style": 2, "label": "Sair", "custom_id": "rd:out"}]}]}})
    cid = dados.get("custom_id") or ""
    if tipo == 3 and cid in ("rd:in", "rd:out"):
        msg = d.get("message") or {}
        e = (msg.get("embeds") or [{}])[0]
        desc = e.get("description") or ""
        dono = (re.search(r"Quem chamou:\*\* <@(\d+)>", desc) or [None, uid])[1]
        entrar_m = re.search(r"\*\*Como entrar:\*\* (.*)", desc)
        lista = desc[desc.find("Quem vai"):]
        gente = re.findall(r"• <@(\d+)>", lista)
        vm = re.search(r"vagas:(\d+)", (e.get("footer") or {}).get("text") or "")
        vagas = int(vm.group(1)) if vm else 0
        if cid == "rd:in":
            if uid in gente:
                return resposta(texto="Você já está na lista. ✋", privada=True, componentes=[])
            if vagas and len(gente) >= vagas:
                return resposta(texto="As vagas já foram preenchidas. Fica para a próxima!", privada=True,
                                componentes=[])
            gente.append(uid)
        else:
            if uid not in gente:
                return resposta(texto="Você não está na lista.", privada=True, componentes=[])
            gente.remove(uid)
        boss = (e.get("title") or "⚔️ Raid").removeprefix("⚔️ ")
        return jsonify({"type": 7, "data": {
            "embeds": [_raid_card(boss, dono, vagas, entrar_m.group(1) if entrar_m else "", gente)],
            "allowed_mentions": {"parse": []}}})
    return None


PUBLICOS = {"codigos", "fruta", "ilha", "drop", "evento", "site", "cupom", "raid"}


def adiar_interacao(d, tipo, nome):
    """Resposta 'pensando...' pelo endpoint de callback. Devolve o tipo usado (5 ou 6)."""
    if tipo == 3 or (tipo == 5 and d.get("message")):
        corpo, adiado = {"type": 6}, 6
    else:
        corpo, adiado = {"type": 5, "data": {} if nome in PUBLICOS else {"flags": 64}}, 5
    try:
        discord_req("POST", f"/interactions/{d.get('id')}/{d.get('token')}/callback", corpo)
    except urllib.error.URLError as e:
        app.logger.error("adiar: %s", str(e)[:120])
    return adiado


def terminar_interacao(d, adiado, resultado):
    base = f"/webhooks/{d.get('application_id')}/{d.get('token')}"
    erro = {"content": "Algo deu errado. Tente de novo em instantes.", "flags": 64, "allowed_mentions": {"parse": []}}
    if "r" not in resultado:
        if "erro" in resultado:
            app.logger.error("interação adiada: %s", str(resultado["erro"])[:160])
        dados, tipo_r = erro, 4
    else:
        try:
            r = resultado["r"]
            corpo = r.get_json() if hasattr(r, "get_json") else None
            tipo_r, dados = (corpo or {}).get("type"), (corpo or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            tipo_r, dados = 4, erro
    if tipo_r == 9:
        dados, tipo_r = {"content": "Demorei para abrir o formulário. Clique de novo, por favor. 🙏", "flags": 64}, 4
    try:
        if adiado == 5:
            if tipo_r in (4, 7):
                discord_req("PATCH", f"{base}/messages/@original", {k: v for k, v in dados.items() if k != "flags"})
            else:
                discord_req("PATCH", f"{base}/messages/@original", {"content": "✅ Feito."})
        elif tipo_r == 7:
            discord_req("PATCH", f"{base}/messages/@original", dados)
        elif tipo_r == 4:
            discord_req("POST", base, dados)
    except urllib.error.URLError as e:
        app.logger.error("terminar: %s", str(e)[:160])


def _tratar_interacao(d, tipo, dados, nome, opcoes, uid):
    try:
        mod = interacao_raid(d, tipo, dados, nome, opcoes, uid)
        if mod is None:
            mod = interacao_atendimento(d, tipo, dados, nome, opcoes, uid)
        if mod is None:
            mod = interacao_cargos(d, tipo, dados, nome, opcoes, uid)
        if mod is None:
            mod = interacao_paineis(d, tipo, dados, nome, opcoes, uid)
        if mod is None:
            mod = interacao_extras(d, tipo, dados, nome, opcoes, uid)
        if mod is None:
            mod = interacao_moderacao(d, tipo, dados, nome, opcoes, uid)
        if mod is not None:
            return mod
    except Exception as e:  # noqa: BLE001
        app.logger.error("moderação %s: %s", nome, str(e)[:160])
        return resposta(texto="Não consegui concluir agora. Tente de novo em instantes.",
                        privada=True, componentes=[])
    try:
        if nome == "cupom":
            return resposta({"author": {"name": "The Blox · parceira oficial da Kyzen", "icon_url": BLOX_LOGO},
                             "title": "🎟️ Cupom KYZEN na The Blox", "url": BLOX, "color": 0x1E90FF,
                             "thumbnail": {"url": BLOX_LOGO},
                             "description": "Quem é da Kyzen tem **desconto exclusivo** na **The Blox**, a nossa "
                                            "patrocinadora: frutas, espadas e gamepasses por **menos do que no "
                                            "próprio Roblox**.\n\n🎟️ Cupom: **`KYZEN`**\n"
                                            "🛒 Entre pelo link e use o cupom no pagamento."},
                            componentes=[{"type": 1, "components": [
                                {"type": 2, "style": 5, "label": "🛒 Comprar com o cupom KYZEN", "url": BLOX}]}])
        if nome == "codigos":
            return cmd_codigos()
        if nome == "fruta":
            return cmd_fruta(str(opcoes.get("nome") or ""))
        if nome == "ilha":
            return cmd_ilha(int(opcoes.get("nivel") or 0))
        if nome == "evento":
            return cmd_evento()
        if nome == "drop":
            return cmd_drop(str(opcoes.get("busca") or ""))
        if nome == "frota":
            return cmd_frota(uid)
        if nome == "site":
            return resposta({"title": "🌐 Site da Kyzen SA", "url": SITE,
                             "description": "🗂️ Trello oficial do Vox Seas com busca\n🗺️ Mapa das ilhas por nível\n"
                                            "🟢 Jogadores online agora\n📅 Eventos com contagem regressiva\n"
                                            "🏴‍☠️ Frota e placar das crews\n📲 Instale como app no celular"})
    except Exception as e:  # noqa: BLE001  (o Discord precisa de uma resposta em até 3 s)
        app.logger.error("comando %s: %s", nome, str(e)[:120])
    return resposta(texto="Algo deu errado com esse comando. Tente de novo em instantes.", privada=True)


@app.route("/api/diag-app")
def diag_app():
    """Staff (com a DEBUG_KEY): qual endereço de comandos está salvo no app do Discord."""
    if not debug_ok():
        return jsonify({"ok": False}), 404
    info = discord_get("/oauth2/applications/@me", os.environ.get("DISCORD_BOT_TOKEN", "").strip())
    banco = db_ok()
    return privado(jsonify({"id": str(info.get("id")), "nome": info.get("name"),
                            "interactions_endpoint_url": info.get("interactions_endpoint_url"),
                            "chave_publica": info.get("verify_key") or discord_get(
                                "/applications/@me", os.environ.get("DISCORD_BOT_TOKEN", "").strip()).get("verify_key"),
                            "chave_na_vercel": bool(os.environ.get("DISCORD_PUBLIC_KEY", "").strip()),
                            "redirects": info.get("redirect_uris"),
                            "variaveis_de_banco": sorted(k for k in os.environ if "DATABASE" in k or "POSTGRES" in k or k.startswith("PG")),
                            "banco_ok": banco, "banco_erro": "" if banco else _db_erro["msg"]}))


@app.route("/api/diag-bots")
def diag_bots():
    """Staff (com a DEBUG_KEY): bots instalados no servidor, com cargo e comandos, para achar repetidos."""
    if not debug_ok():
        return jsonify({"ok": False}), 404
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    bots = {}
    try:
        for i in discord_req("GET", f"/guilds/{gid}/integrations"):
            a = i.get("application") or {}
            bot = a.get("bot") or {}
            if i.get("type") == "discord" and bot.get("id"):
                bots[str(bot["id"])] = {"nome": a.get("name") or bot.get("username"),
                                        "descricao": cortar(a.get("description") or "", 200),
                                        "desde": i.get("synced_at") or "", "escopos": i.get("scopes") or []}
    except urllib.error.HTTPError as e:
        return privado(jsonify({"ok": False, "erro": e.code}))
    for r in discord_req("GET", f"/guilds/{gid}/roles"):
        b = str((r.get("tags") or {}).get("bot_id") or "")
        if b in bots:
            p = int(r.get("permissions") or 0)
            bots[b]["cargo"] = r.get("name")
            bots[b]["perms"] = [n for n, bit in (("admin", PERM_ADMIN), ("banir", PERM_BAN), ("expulsar", PERM_KICK),
                                                 ("castigar", PERM_MODERAR), ("gerenciar_cargos", 1 << 28),
                                                 ("gerenciar_msgs", 1 << 13)) if p & bit]
    return privado(jsonify({"ok": True, "total": len(bots), "bots": bots}))


@app.route("/api/registrar-comandos", methods=["POST"])
def registrar_comandos():
    """Cadastra os comandos no servidor (staff, com a DEBUG_KEY)."""
    if not debug_ok():
        return jsonify({"ok": False}), 404
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not _app_id["id"]:  # com a chave pública gravada na Vercel, o ID do app não é buscado no início
        _app_id["id"] = os.environ.get("DISCORD_CLIENT_ID", "").strip() or \
            str(discord_get("/oauth2/applications/@me", token)["id"])
    req = urllib.request.Request(f"{API}/applications/{_app_id['id']}/guilds/{gid}/commands", method="PUT",
                                 data=json.dumps(COMANDOS).encode(),
                                 headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                          "User-Agent": "KyzenSA-Site (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            feitos = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return privado(jsonify({"ok": False, "erro": e.code, "discord": e.read().decode("utf-8", "replace")[:300]}), 502)
    try:
        registro = canal_registro(criar=True)
        registro = {"punicoes": registro, "bot": canal_staff("bot", criar=True),
                    "formularios": canal_staff("forms", criar=True)}
    except (urllib.error.URLError, KeyError, ValueError) as e:
        registro = f"erro: {str(e)[:120]}"
    return privado(jsonify({"ok": True, "comandos": [c.get("name") for c in feitos], "registro_punicoes": registro}))


# ---------------------------------------------------------------------------
# Notificações no celular (Web Push)
#   DATABASE_URL  -> banco Postgres da Neon (Vercel > Storage). Guarda só as inscrições (endereço do
#                    navegador e chaves), nunca nome nem conta de ninguém.
#   VAPID_PUBLIC / VAPID_PRIVATE -> chaves das notificações
#   Disparo: quando o site percebe um anúncio novo (público) ou um evento começando em até 30 min.
# ---------------------------------------------------------------------------
_db_pronto = {"ok": False}


def url_banco():
    """A Vercel pode criar a variável com outro nome ou com prefixo (ex.: STORAGE_DATABASE_URL)."""
    for k in ("DATABASE_URL", "POSTGRES_URL"):
        if os.environ.get(k, "").strip():
            return os.environ[k].strip()
    for k, v in sorted(os.environ.items()):
        if (k.endswith("_DATABASE_URL") or k.endswith("_POSTGRES_URL")) and v.strip().startswith("postgres"):
            return v.strip()
    return ""


_db_erro = {"msg": ""}


def sql(query, params=None):
    """Consulta na Neon pelo endpoint HTTP (sem driver)."""
    url = url_banco()
    if not url:
        raise ValueError("sem banco")
    host = urllib.parse.urlparse(url).hostname
    req = urllib.request.Request(f"https://{host}/sql", method="POST",
                                 data=json.dumps({"query": query, "params": params or []}).encode(),
                                 headers={"Neon-Connection-String": url, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8")).get("rows") or []


def db_ok():
    if _db_pronto["ok"]:
        return True
    if not url_banco():
        _db_erro["msg"] = "sem variável de banco"
        return False
    try:
        sql("CREATE TABLE IF NOT EXISTS push_subs (endpoint text PRIMARY KEY, p256dh text NOT NULL, "
            "auth text NOT NULL, criado timestamptz DEFAULT now())")
        sql("CREATE TABLE IF NOT EXISTS push_feitos (chave text PRIMARY KEY, em timestamptz DEFAULT now())")
        _db_pronto["ok"] = True
    except urllib.error.HTTPError as e:
        _db_erro["msg"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        app.logger.error("db: %s", _db_erro["msg"])
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
        _db_erro["msg"] = str(e)[:200]
        app.logger.error("db: %s", str(e)[:120])
    return _db_pronto["ok"]


def push_ok():
    return bool(os.environ.get("VAPID_PRIVATE") and os.environ.get("VAPID_PUBLIC")) and db_ok()


def enviar_push(titulo, corpo, url="/", chave=None):
    """Manda para todo mundo inscrito. `chave` evita mandar a mesma notificação duas vezes."""
    if not push_ok():
        return 0
    try:
        if chave and sql("INSERT INTO push_feitos (chave) VALUES ($1) ON CONFLICT DO NOTHING RETURNING chave", [chave]) == []:
            return 0  # outra instância do site já mandou
        subs = sql("SELECT endpoint, p256dh, auth FROM push_subs LIMIT 500")
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return 0
    from concurrent.futures import ThreadPoolExecutor

    from pywebpush import WebPushException, webpush
    dados = json.dumps({"t": titulo[:80], "b": corpo[:160], "u": url})

    def um(s):
        try:
            webpush({"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}, dados,
                     vapid_private_key=os.environ["VAPID_PRIVATE"], vapid_claims={"sub": SITE}, timeout=5, ttl=3600)
            return 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                try:
                    sql("DELETE FROM push_subs WHERE endpoint = $1", [s["endpoint"]])
                except (urllib.error.URLError, ValueError):
                    pass
        except Exception:  # noqa: BLE001
            pass
        return 0

    with ThreadPoolExecutor(max_workers=16) as ex:
        return sum(ex.map(um, subs))


def avisar_anuncios(itens):
    """Chamado quando o cache de novidades é atualizado. Só anúncios públicos."""
    if not push_ok():
        return
    pub = [n for n in itens or [] if n.get("tag") not in CREW_TAGS and n.get("link")]
    try:
        if not sql("SELECT 1 FROM push_feitos WHERE chave = 'inicio-anuncios'"):
            for n in pub:  # primeira vez: marca tudo como já visto, sem notificar o passado
                sql("INSERT INTO push_feitos (chave) VALUES ($1) ON CONFLICT DO NOTHING", ["a:" + n["link"]])
            sql("INSERT INTO push_feitos (chave) VALUES ('inicio-anuncios') ON CONFLICT DO NOTHING")
            return
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return
    for n in pub[:3]:
        enviar_push(f"📣 {n.get('tag', 'Novidade').title()}", n.get("titulo", ""), "/#novidades", chave="a:" + n["link"])


def avisar_eventos(evs):
    if not push_ok():
        return
    agora = time.time()
    for e in evs or []:
        try:
            ini = datetime.fromisoformat(str(e["inicio"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        falta = (ini - agora) / 60
        if e.get("ao_vivo") or 0 < falta <= 30:
            txt = "Está acontecendo agora!" if e.get("ao_vivo") else f"Começa em {max(1, round(falta))} min."
            enviar_push(f"📅 {e['nome']}", txt + (f" · {e['local']}" if e.get("local") else ""), "/#novidades",
                        chave="e:" + e["id"])


@app.route("/api/push/chave")
def push_chave():
    resp = jsonify({"ok": push_ok(), "chave": os.environ.get("VAPID_PUBLIC", "") if push_ok() else ""})
    resp.headers["Cache-Control"] = "public, s-maxage=300"
    return resp


def mesma_origem():
    return request.headers.get("Origin", "") in (f"https://{request.host}", f"http://{request.host}")


@app.route("/api/push/inscrever", methods=["POST"])
def push_inscrever():
    if not mesma_origem() or not push_ok():
        return jsonify({"ok": False}), 403
    s = request.get_json(silent=True) or {}
    ep, keys = str(s.get("endpoint") or ""), s.get("keys") or {}
    if not isinstance(keys, dict):
        return jsonify({"ok": False}), 400
    p, a = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    hosts = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com", "notify.windows.com",
             "push.services.mozilla.com")
    h = urllib.parse.urlparse(ep).hostname or ""
    if not ep.startswith("https://") or len(ep) > 800 or not any(h == x or h.endswith("." + x) for x in hosts) \
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,200}", p) or not re.fullmatch(r"[A-Za-z0-9_-]{10,60}", a):
        return jsonify({"ok": False}), 400
    try:
        sql("INSERT INTO push_subs (endpoint, p256dh, auth) VALUES ($1, $2, $3) "
            "ON CONFLICT (endpoint) DO UPDATE SET p256dh = $2, auth = $3", [ep, p, a])
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True})


@app.route("/api/push/sair", methods=["POST"])
def push_sair():
    if not mesma_origem() or not push_ok():
        return jsonify({"ok": False}), 403
    ep = str((request.get_json(silent=True) or {}).get("endpoint") or "")[:800]
    try:
        sql("DELETE FROM push_subs WHERE endpoint = $1", [ep])
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Caça ao tesouro: 5 fragmentos escondidos no site. Quem junta os 5 e entra com o Discord ganha
# o cargo "🗝️ Caçador de Tesouros" (o bot precisa da permissão Gerenciar cargos).
# ---------------------------------------------------------------------------
FRAGS = ("barco", "rota", "adeus", "cartas", "perdido")
CARGO_TESOURO = "🗝️ Caçador de Tesouros"
_pista_ip = {}
_cacadores = {"t": 0, "n": None}


def codigo_frag(f):
    return hmac.new(app.secret_key, f"tesouro:{f}".encode(), hashlib.sha256).hexdigest()[:12]


@app.route("/api/tesouro/pista/<f>")
def tesouro_pista(f):
    if f not in FRAGS:
        return jsonify({"ok": False}), 404
    ip, agora = _ip(), time.time()
    lst = [t for t in _pista_ip.get(ip, []) if agora - t < 3600]
    if len(lst) >= 30:
        return jsonify({"ok": False}), 429
    _pista_ip[ip] = lst + [agora]
    return privado(jsonify({"ok": True, "f": f, "c": codigo_frag(f), "n": FRAGS.index(f) + 1}))


def cargo_tesouro(token, gid, criar=False):
    for r in discord_get(f"/guilds/{gid}/roles", token):
        if r.get("name") == CARGO_TESOURO:
            return str(r["id"])
    if not criar:
        return ""
    return str(discord_post(f"/guilds/{gid}/roles", token, {"name": CARGO_TESOURO, "color": 0xE3B36D,
                                                            "permissions": "0", "mentionable": False})["id"])


@app.route("/api/tesouro/status")
def tesouro_status():
    """Quantas pessoas já acharam o tesouro (só o número)."""
    agora = time.time()
    if agora - _cacadores["t"] > 600 and pode_tentar("cacadores", 120):
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
        try:
            rid, n, depois = cargo_tesouro(token, gid), 0, "0"
            if rid:
                for _ in range(10):
                    lote = discord_get(f"/guilds/{gid}/members?limit=1000&after={depois}", token)
                    n += sum(1 for m in lote if rid in (m.get("roles") or []))
                    if len(lote) < 1000:
                        break
                    depois = lote[-1]["user"]["id"]
            _cacadores.update(t=agora, n=n)
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            falhou("cacadores")
    resp = jsonify({"ok": True, "cacadores": _cacadores["n"]})
    resp.headers["Cache-Control"] = "public, s-maxage=300"
    return resp


@app.route("/api/tesouro/resgatar", methods=["POST"])
def tesouro_resgatar():
    if not mesma_origem():
        return jsonify({"ok": False}), 403
    uid = usuario()
    if not uid:
        return privado(jsonify({"ok": False, "motivo": "login"}), 401)
    if not e_membro(uid):
        return privado(jsonify({"ok": False, "motivo": "membro"}), 403)
    codigos = (request.get_json(silent=True) or {}).get("codigos") or {}
    if not isinstance(codigos, dict) or not all(hmac.compare_digest(str(codigos.get(f, "")), codigo_frag(f)) for f in FRAGS):
        return privado(jsonify({"ok": False, "motivo": "incompleto"}), 400)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    try:
        rid = cargo_tesouro(token, gid, criar=True)
        req = urllib.request.Request(f"{API}/guilds/{gid}/members/{uid}/roles/{rid}", method="PUT",
                                     headers={"Authorization": f"Bot {token}", "User-Agent": "KyzenSA-Site (1.0)",
                                              "X-Audit-Log-Reason": "Caca ao tesouro do site"})
        urllib.request.urlopen(req, timeout=8).close()
    except urllib.error.HTTPError as e:
        app.logger.error("tesouro: HTTP %s", e.code)
        return privado(jsonify({"ok": False, "motivo": "permissao" if e.code == 403 else "erro"}), 502)
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        return privado(jsonify({"ok": False, "motivo": "erro"}), 502)
    _cacadores["t"] = 0
    return privado(jsonify({"ok": True}))


# ---------------------------------------------------------------------------
# Página, prévia ao compartilhar (Discord/WhatsApp leem as meta tags) e app instalável
# ---------------------------------------------------------------------------
ROBOS = re.compile(r"Discordbot|WhatsApp|facebookexternalhit|Twitterbot|TelegramBot|Slackbot|LinkedInBot|"
                   r"SkypeUriPreview|redditbot|Googlebot|bingbot", re.I)


def slug_py(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def num(n):
    return f"{int(n):,}".replace(",", ".")


def pl(n, um, varios):
    return f"{n} {um if n == 1 else varios}"


def trocar_meta(html, titulo, desc, url):
    for prop, val in (("og:title", titulo), ("og:description", desc), ("og:url", url)):
        html = re.sub(rf'(<meta property="{prop}" content=")[^"]*(")', lambda m: m.group(1) + esc(val) + m.group(2), html)
    html = re.sub(r'(<meta name="description" content=")[^"]*(")', lambda m: m.group(1) + esc(desc) + m.group(2), html)
    return html


def meta_ao_vivo(html, crew_slug=None):
    """Prévia com números ao vivo. Só busca dados novos quando quem pede é um robô de prévia."""
    if ROBOS.search(request.headers.get("User-Agent", "")):
        for f in (jogo, servidor):
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
    j, s = _jogo_cache["data"] or {}, _srv_cache["data"] or {}
    if crew_slug:
        # placar das crews é privado (categoria Crew): a prévia não mostra números
        return trocar_meta(html, "Ficha de crew — Frota Kyzen SA",
                           "Ficha de crew da Frota Kyzen SA. Entre com o seu Discord no site para ver o placar "
                           "e o histórico (só para membros com acesso à área das crews).",
                           f"https://kyzensaofc.vercel.app/crew/{crew_slug}")
    partes = []
    if j.get("jogando"):
        partes.append(f"🟢 {num(j['jogando'])} jogando Vox Seas agora")
    if s.get("membros"):
        partes.append(f"{num(s['membros'])} membros na Kyzen SA")
    if s.get("online"):
        partes.append(f"{num(s['online'])} online")
    if not partes:
        return html
    return trocar_meta(html, "Kyzen SA — Servidor de Vox Seas",
                       " · ".join(partes) + ". Crews, wars, eventos e muito mais no Discord.",
                       "https://kyzensaofc.vercel.app/")


def resposta_pagina(status=200, crew_slug=None):
    html = pagina()
    if isinstance(html, tuple):
        return Response(html[0], mimetype="text/plain"), html[1]
    if status == 200:
        html = meta_ao_vivo(html, crew_slug)
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = csp_da_pagina(html)
    return resp, status


@app.route("/")
def index():
    return resposta_pagina()


@app.route("/crew/<slug>")
def pagina_crew(slug):
    slug = slug_py(slug)[:60]
    return resposta_pagina(crew_slug=slug or None)


@app.route("/manifest.webmanifest")
def manifesto():
    resp = app.send_static_file("manifest.webmanifest")
    resp.mimetype = "application/manifest+json"
    return resp


@app.route("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.mimetype = "text/javascript"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.errorhandler(404)
def not_found(_):
    return resposta_pagina(404)


@app.errorhandler(413)
def grande_demais(_):
    return jsonify({"ok": False, "erro": "Envio grande demais."}), 413


if __name__ == "__main__":
    app.run()
