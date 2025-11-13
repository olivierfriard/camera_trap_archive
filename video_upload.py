import os

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "secret-key"  # cambia in produzione
UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

APP_ROOT = "/fototrappole"


@app.route(APP_ROOT)
def index():
    return render_template("upload_video.html")


@app.route(APP_ROOT + "/upload_video", methods=["POST"])
def upload_video():
    # title = request.form.get("title")
    # description = request.form.get("description")
    # lat = request.form.get("latitude")
    # lng = request.form.get("longitude")
    video = request.files.get("video")

    if not video:
        flash("Nessun file video caricato!")
        return redirect(url_for("index"))

    filename = secure_filename(video.filename)
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    video.save(save_path)

    video_url = url_for("uploaded_file", filename=filename)
    flash(
        f"Video caricato con successo! <!--<a href='{video_url}' target='_blank'>Apri file</a>-->"
    )

    return render_template(
        "upload_info.html",
        video_url=video_url,
        # title=title,
        # description=description,
        # lat=lat,
        # lng=lng,
    )


@app.route(APP_ROOT + "/upload_info", methods=["POST"])
def upload_info():
    operatore = request.form.get("operatore")
    numero_lupi = request.form.get("numero_lupi")
    # lat = request.form.get("latitude")
    # lng = request.form.get("longitude")
    video = request.files.get("video")

    if not video:
        flash("Nessun file video caricato!")
        return redirect(url_for("index"))

    filename = secure_filename(video.filename)
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    video.save(save_path)

    video_url = url_for("uploaded_file", filename=filename)
    flash(
        f"Video caricato con successo! <!--<a href='{video_url}' target='_blank'>Apri file</a>-->"
    )

    return render_template(
        "upload_info.html",
        video_url=video_url,
        # title=title,
        # description=description,
        # lat=lat,
        # lng=lng,
    )


@app.route(APP_ROOT + "/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
