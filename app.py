from pathlib import Path
from flask import Flask, Response

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "public"),
    static_url_path="",
)


def pagina():
    # procura o index.html em templates (ou no nome antigo errado, por segurança)
    for pasta in ("templates", "teamplates"):
        arq = BASE_DIR / pasta / "index.html"
        if arq.exists():
            return arq.read_text(encoding="utf-8")
    return "index.html não encontrado", 500


@app.route("/")
def index():
    return Response(pagina(), mimetype="text/html")


@app.errorhandler(404)
def not_found(_):
    return Response(pagina(), mimetype="text/html"), 404


if __name__ == "__main__":
    app.run(debug=True)
    