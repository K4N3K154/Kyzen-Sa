from flask import Flask, render_template

# static_folder="public" + static_url_path="" -> as imagens ficam em /img/...,
# o mesmo caminho que a Vercel serve direto pela CDN (pasta public/).
app = Flask(__name__, static_folder="public", static_url_path="", template_folder="templates")


@app.route("/")
def index():
    return render_template("index.html")


@app.errorhandler(404)
def not_found(_):
    # O site é uma página única com âncoras (#inicio, #crews...), então volta pro início.
    return render_template("index.html"), 404


if __name__ == "__main__":
    app.run(debug=True)
