import base64
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "public"),
    static_url_path="",
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # nenhum envio do site passa de 16 KB


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
              ("CREW", "crew-post")]


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
    resp = jsonify({"news": _cache["data"], "configured": True})
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
    resp.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=300"
    return resp


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
            partidas.append({"a": ca["crew"], "b": cb["crew"], "ga": ga, "gb": gb,
                             "data": f"{d.day} {MESES[d.month - 1]} {d.year}",
                             "link": f"https://discord.com/channels/{guild_id}/{canal}/{m['id']}"})
    for c in tabela.values():
        c["h"] = c["h"][-5:][::-1]  # últimas 5, da mais recente para a mais antiga
        c["jogos"] = c["jogos"][-40:][::-1]  # histórico para a página da crew
    ranking = sorted(tabela.values(), key=lambda c: (-c["pts"], -c["v"], c["d"], c["crew"].lower()))
    return {"ranking": ranking[:20], "partidas": partidas[::-1][:10],
            "ultima": int(ultima.timestamp()) if ultima else None}


@app.route("/api/wars")
def wars():
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
    resp.headers["Cache-Control"] = "public, s-maxage=15, stale-while-revalidate=60"
    return resp


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
        for f in (jogo, servidor, wars):
            try:
                f()
            except Exception:  # noqa: BLE001
                pass
    j, s, w = _jogo_cache["data"] or {}, _srv_cache["data"] or {}, _wars_cache["data"] or {}
    if crew_slug:
        for i, c in enumerate(w.get("ranking") or []):
            if slug_py(c["crew"]) == crew_slug:
                ult = " ".join(c.get("h") or []) or "nenhuma ainda"
                return trocar_meta(html, f"{c['crew']} — #{i + 1} da Frota Kyzen SA",
                                   f"🏴‍☠️ {pl(c['v'], 'vitória', 'vitórias')} · {pl(c['e'], 'empate', 'empates')} · "
                                   f"{pl(c['d'], 'derrota', 'derrotas')} · {pl(c['pts'], 'ponto', 'pontos')}. "
                                   f"Últimas batalhas: {ult}. Veja a ficha completa da crew no site da Kyzen SA.",
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
