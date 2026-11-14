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


def limpar(txt):
    txt = txt or ""
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
        "link": link,
        "img": imagem_da(msg),
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
    if request.args.get("debug") == "1":
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
            return guardar(f'<a href="{esc(m.group(9))}" target="_blank" rel="noopener nofollow">'
                           f'{inline_md(m.group(8), ctx)}</a>')
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
    "convites": ("CONVITES_CHANNEL", "invites"),
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
    if request.args.get("debug") == "1":
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
    if agora - _srv_cache["t"] > 300 or _srv_cache["data"] is None:
        try:
            dados = buscar_servidor()
        except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
            dados = None
            if request.args.get("debug") == "1":
                return jsonify({"erro": str(e)[:120]}), 502
        if dados:
            _srv_cache.update(t=agora, data=dados)
    if not _srv_cache["data"]:
        return jsonify({"configured": False}), 503
    resp = jsonify({"configured": True, **_srv_cache["data"],
                    "roblox": os.environ.get("ROBLOX_URL", "").strip()})
    resp.headers["Cache-Control"] = "public, s-maxage=300, stale-while-revalidate=600"
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
    tabela, partidas = {}, []

    def crew(n):
        return tabela.setdefault(n.lower(), {"crew": n, "v": 0, "d": 0, "e": 0, "pts": 0})

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
            d = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
            partidas.append({"a": ca["crew"], "b": cb["crew"], "ga": ga, "gb": gb,
                             "data": f"{d.day} {MESES[d.month - 1]} {d.year}",
                             "link": f"https://discord.com/channels/{guild_id}/{canal}/{m['id']}"})
    ranking = sorted(tabela.values(), key=lambda c: (-c["pts"], -c["v"], c["d"], c["crew"].lower()))
    return {"ranking": ranking[:20], "partidas": partidas[::-1][:8]}


@app.route("/api/wars")
def wars():
    agora = time.time()
    if agora - _wars_cache["t"] > CACHE_SECONDS or _wars_cache["data"] is None:
        try:
            dados = buscar_wars()
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            dados = None
        if dados is not None:
            _wars_cache.update(t=agora, data=dados)
    resp = jsonify({"configured": _wars_cache["data"] is not None,
                    **(_wars_cache["data"] or {"ranking": [], "partidas": []})})
    resp.headers["Cache-Control"] = "public, s-maxage=60, stale-while-revalidate=300"
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
    if agora - _staff_cache["t"] > 600 or _staff_cache["data"] is None:
        try:
            dados = buscar_staff()
        except (urllib.error.URLError, ValueError, KeyError, TypeError):
            dados = None
        if dados is not None:
            _staff_cache.update(t=agora, data=dados)
    resp = jsonify({"configured": _staff_cache["data"] is not None, "grupos": _staff_cache["data"] or []})
    resp.headers["Cache-Control"] = "public, s-maxage=600, stale-while-revalidate=1200"
    return resp


@app.route("/api/requisitos")
def requisitos():  # endereço antigo, mantido por compatibilidade
    return canal_api("requisitos")


@app.route("/")
def index():
    return Response(pagina(), mimetype="text/html")


@app.errorhandler(404)
def not_found(_):
    return Response(pagina(), mimetype="text/html"), 404


if __name__ == "__main__":
    app.run(debug=True)
