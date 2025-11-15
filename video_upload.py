import hashlib
import os
import time
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import create_engine, text
from werkzeug.utils import secure_filename

import camtrap_banner_decoder
import users

app = Flask(__name__)
app.secret_key = "secret-key"  # cambia in produzione
UPLOAD_FOLDER = "uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

APP_ROOT = "/fototrappole"

# PostgreSQL connection
DATABASE_URL = "postgresql://sighting_user@localhost:5432/sighting"
engine = create_engine(DATABASE_URL)

USERS = users.USERS


# --- Login required decorator ---
def login_required(f):
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            flash("Devi effettuare il login per accedere a questa pagina.")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


@app.route(APP_ROOT)
@login_required
def index():
    return render_template("index.html")


@app.route(APP_ROOT + "/upload_video_form")
@login_required
def upload_video_form():
    return render_template("upload_video.html")


@app.route(APP_ROOT + "/sighting_list")
@login_required
def sighting_list():
    with engine.connect() as conn:
        query = text(
            "SELECT code, operator, camtrap_id FROM sighting WHERE operator = :operator"
        )
        rows = conn.execute(query, {"operator": session["username"]}).mappings().all()

    return render_template("sighting_list.html", sightings=rows)


@app.route(APP_ROOT + "/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if username in USERS and USERS[username]["pwd"] == password:
            session["username"] = username
            session["fullname"] = USERS[username]["fullname"]
            session["institution"] = USERS[username]["institution"]
            session["code"] = USERS[username]["code"]

            flash(f"Benvenuto, {session['fullname']} ({session['institution']})!")
            return redirect(url_for("index"))
        else:
            flash("Username o password non validi.")
    return render_template("login.html")


@app.route(APP_ROOT + "/logout")
def logout():
    session.pop("username", None)
    flash("Logout effettuato.")
    return redirect(url_for("login"))


@app.route(APP_ROOT + "/upload_video", methods=["POST"])
@login_required
def upload_video():
    """
    save video
    extract time and date info
    rename video
    """

    video = request.files.get("video")

    if not video:
        flash("Nessun file video caricato!")
        return redirect(url_for("index"))

    # compute MD5 incrementally
    md5 = hashlib.md5()
    chunk_size = 8192  # 8 KB

    # Read the file stream in chunks
    for chunk in iter(lambda: video.stream.read(chunk_size), b""):
        md5.update(chunk)
    video.stream.seek(0)
    file_content_md5 = md5.hexdigest()
    print(file_content_md5)

    # check if md5 already in DB
    with engine.connect() as conn:
        query = text(
            "SELECT code, operator, camtrap_id FROM media,sighting WHERE sighting.id = media.sighting_id AND file_content_md5 = :file_content_md5"
        )
        row = (
            conn.execute(query, {"file_content_md5": file_content_md5})
            .mappings()
            .fetchone()
        )
        print(row)
        if row is not None:
            flash(
                f"Il file {video.filename} è già presente nel database: {row['operator']}, {row['code']}, {row['camtrap_id']}"
            )
            return redirect(url_for("upload_video_form"))

    original_file_name = secure_filename(video.filename)
    print(f"{original_file_name=}")

    # get new file name
    new_file_name = Path(f"{int(time.time())}_{session['username']}").with_suffix(
        Path(original_file_name).suffix
    )
    print(f"{new_file_name=}")

    save_path = Path(app.config["UPLOAD_FOLDER"]) / new_file_name
    video.save(save_path)

    # md5 of file content
    # file_content_md5 = hashlib.md5(open(save_path, "rb").read()).hexdigest()
    # print(file_content_md5)

    video_url = url_for("uploaded_file", filename=new_file_name)
    flash(
        f"Video caricato con successo! <!--<a href='{video_url}' target='_blank'>Apri file</a>-->"
    )

    # check date time
    data = camtrap_banner_decoder.extract_date_time(save_path)
    if "error" not in data:
        print(f"{data=}")
        code: str = ""
        if data["date"]:
            code = data["date"][2:].replace("-", "") + session["code"]
        else:
            code = session["code"]
        try:
            time_ = data["time"][:2] + ":" + data["time"][2:4] + ":" + data["time"][4:6]
        except Exception:
            time_ = data["time"]
    else:
        code = ""
        time_ = ""

    print(session["fullname"])

    return render_template(
        "upload_info.html",
        original_file_name=original_file_name,
        new_file_name=str(new_file_name),
        video_url=video_url,
        operator=session["fullname"],
        code=code,
        date=data["date"],
        time_=time_,
        file_content_md5=file_content_md5,
    )


@app.route(APP_ROOT + "/save_info", methods=["POST"])
@login_required
def save_info():
    """
    save info into db
    """
    print(request.form)

    operator = request.form.get("operator")
    camtrap_id = request.form.get("camtrap_id")
    code = request.form.get("code")
    numero_lupi = request.form.get("numero_lupi")
    scalp = request.form.get("scalp")
    notes = request.form.get("note") if request.form.get("note") else None
    lat = request.form.get("latitude")
    lng = request.form.get("longitude")
    transect_id = (
        request.form.get("transect_id") if request.form.get("transect_id") else None
    )

    original_file_name = request.form.get("original_file_name")
    new_file_name = request.form.get("new_file_name")
    file_content_md5 = request.form.get("file_content_md5")

    print(f"{original_file_name=}")
    print(f"{new_file_name=}")
    print(f"{file_content_md5=}")

    with engine.connect() as conn:
        query = text("""
            INSERT INTO sighting
                (code, operator, institution, timestamp, camtrap_id, scalp, transect_id, wolf_number, latitude, longitude, notes)
            VALUES
                (:code, :operator, :institution, :timestamp, :camtrap_id, :scalp, :transect_id, :wolf_number, :latitude, :longitude, :notes)
            RETURNING id;
        """)

        result = conn.execute(
            query,
            {
                "code": code,
                "operator": session["username"],
                "institution": session["institution"],
                "timestamp": None,
                "camtrap_id": camtrap_id,
                "scalp": scalp,
                "transect_id": transect_id if transect_id else None,
                "wolf_number": numero_lupi,
                "latitude": lat,
                "longitude": lng,
                "notes": notes,
            },
        )
        sighting_id = result.scalar()
        conn.commit()

        query = text("""
            INSERT INTO media
                (original_file_name, new_file_name, file_content_md5, sighting_id)
            VALUES
                (:original_file_name, :new_file_name, :file_content_md5, :sighting_id)
        """)

        result = conn.execute(
            query,
            {
                "original_file_name": original_file_name,
                "new_file_name": new_file_name,
                "file_content_md5": file_content_md5,
                "sighting_id": sighting_id,
            },
        )
        conn.commit()

        flash("Avistamento salvato.")

    return redirect(url_for("index"))


@app.route(APP_ROOT + "/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
