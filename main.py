"""
camera trap archive service
"""

import base64
import hashlib
import logging
import os
import secrets
import sys
import time
from argparse import Namespace
from logging.handlers import WatchedFileHandler
from pathlib import Path

import cv2
from dotenv import dotenv_values
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from markupsafe import Markup
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import camtrap_banner_decoder

config_filename = Path(__file__).with_name(".env")

if not config_filename.is_file():
    print(".env not found")
    sys.exit()

config = dotenv_values(config_filename)

DATABASE_URL = config["db_url"]
APP_ROOT = config["app_root"]
UPLOAD_FOLDER = config["upload_folder"]
APP_SECRET_KEY = config["app_secret_key"]
DEBUG = bool(int(config["debug"]))
LOG_FILE = config["log_file"]


class MyFlask(Flask):
    def log_exception(self, exc_info):
        """send exceptions in Gunicorn logger."""
        logging.getLogger("gunicorn.error").error(
            "Exception during request", exc_info=exc_info
        )


app = MyFlask(__name__)
app.secret_key = APP_SECRET_KEY  # cambia in produzione
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# Logger applicatif
handler = WatchedFileHandler(LOG_FILE, encoding="utf-8")
handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(process)d] [%(levelname)s] %(module)s:%(lineno)d - %(message)s"
    )
)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)
app.logger.propagate = False


if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# PostgreSQL connection
engine = create_engine(DATABASE_URL)

def get_user(username):
    """Fetch an account and its access status from PostgreSQL."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT username, password_hash, fullname, institution, code, is_active, is_admin "
                "FROM public.users WHERE username = :username"
            ),
            {"username": username},
        ).mappings().fetchone()


# --- Login required decorator ---
def login_required(f):
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user(session["username"]) if "username" in session else None
        if user is None or not user["is_active"]:
            session.clear()
            flash("Devi effettuare il login per accedere a questa pagina.", "danger")
            return redirect(url_for("login"))
        for field in ("username", "fullname", "institution", "code"):
            session[field] = user[field]
        session["is_admin"] = user["is_admin"]
        g.current_user = user
        return f(*args, **kwargs)

    return decorated_function


@app.route(APP_ROOT)
@login_required
def index():
    return render_template("index.html")


@app.route(APP_ROOT + "/administration", methods=["GET", "POST"])
@login_required
def administration():
    """Manage accounts, checking current administrator privileges on each request."""
    if not g.current_user["is_admin"]:
        abort(403)
    if "admin_csrf_token" not in session:
        session["admin_csrf_token"] = secrets.token_urlsafe(32)

    if request.method == "POST":
        if not secrets.compare_digest(
            session["admin_csrf_token"].encode(), request.form.get("csrf_token", "").encode()
        ):
            abort(400)
        action = request.form.get("action")
        if action not in ("create", "save", "deactivate", "reactivate"):
            abort(400)
        username = request.form.get("username", "").strip()
        if not username:
            abort(400)
        values = {"username": username}
        if action in ("create", "save"):
            for field in ("fullname", "institution", "code"):
                values[field] = request.form.get(field, "").strip()
            password = request.form.get("password", "")
            if not all(values.values()) or (action == "create" and not password):
                flash("Compila tutti i campi obbligatori.", "danger")
                return redirect(url_for("administration"))
            values["is_admin"] = request.form.get("is_admin") == "1"
            values["is_active"] = request.form.get("is_active") == "1"
            if password:
                values["password_hash"] = generate_password_hash(password)
        try:
            with engine.begin() as conn:
                # Serialize account changes to protect the last active administrator.
                conn.execute(text("LOCK TABLE public.users IN SHARE ROW EXCLUSIVE MODE"))
                actor = conn.execute(
                    text("SELECT is_active, is_admin FROM public.users WHERE username = :username"),
                    {"username": session["username"]},
                ).mappings().fetchone()
                if actor is None or not actor["is_active"] or not actor["is_admin"]:
                    abort(403)
                if action == "create":
                    conn.execute(text(
                        "INSERT INTO public.users "
                        "(username, password_hash, fullname, institution, code, is_active, is_admin) "
                        "VALUES (:username, :password_hash, :fullname, :institution, :code, :is_active, :is_admin)"
                    ), values)
                else:
                    target = conn.execute(
                        text("SELECT is_active, is_admin FROM public.users WHERE username = :username"),
                        {"username": username},
                    ).mappings().fetchone()
                    if target is None:
                        abort(404)
                    if action in ("deactivate", "reactivate"):
                        values["is_active"] = action == "reactivate"
                        values["is_admin"] = target["is_admin"]
                    if target["is_admin"] and target["is_active"] and not (
                        values["is_admin"] and values["is_active"]
                    ):
                        count = conn.execute(text(
                            "SELECT count(*) FROM public.users WHERE is_active AND is_admin"
                        )).scalar_one()
                        if count <= 1:
                            flash("Non puoi disattivare o rimuovere il ruolo dell'ultimo amministratore attivo.", "danger")
                            return redirect(url_for("administration"))
                    if action == "save":
                        sql = (
                            "UPDATE public.users SET fullname = :fullname, institution = :institution, "
                            "code = :code, is_active = :is_active, is_admin = :is_admin"
                        )
                        if "password_hash" in values:
                            sql += ", password_hash = :password_hash"
                        sql += " WHERE username = :username"
                    else:
                        sql = "UPDATE public.users SET is_active = :is_active WHERE username = :username"
                    conn.execute(text(sql), values)
        except IntegrityError:
            flash("Nome utente già esistente. Scegli un altro nome o riattiva il conto esistente.", "danger")
        else:
            flash("Utente creato." if action == "create" else "Utente aggiornato.", "success")
        return redirect(url_for("administration"))

    with engine.connect() as conn:
        accounts = conn.execute(text(
            "SELECT username, fullname, institution, code, is_active, is_admin "
            "FROM public.users ORDER BY username"
        )).mappings().all()
    selected_username = request.args.get("username")
    selected = next((user for user in accounts if user["username"] == selected_username), None)
    if selected_username is not None and selected is None:
        abort(404)
    return render_template("administration.html", accounts=accounts, selected=selected)


@app.route(APP_ROOT + "/upload_video_form")
@login_required
def upload_video_form():
    return render_template("upload_video.html")


@app.route(APP_ROOT + "/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password", "")
        user = get_user(username)
        if (
            user is not None
            and user["is_active"]
            and check_password_hash(user["password_hash"], password)
        ):
            session.clear()
            for field in ("username", "fullname", "institution", "code"):
                session[field] = user[field]

            flash(
                f"Benvenuto, {session['fullname']} ({session['institution']})!",
                "success",
            )
            return redirect(url_for("index"))
        else:
            flash("Username o password non validi.", "danger")
    return render_template("login.html")


@app.route(APP_ROOT + "/logout")
def logout():
    session.clear()
    flash("Logout effettuato.", "success")
    return redirect(url_for("login"))


@app.route(APP_ROOT + "/elenco_fototrappole", methods=["GET"])
@login_required
def elenco_fototrappole():
    """
    elenco fototrappola
    """
    with engine.connect() as conn:
        sql = text("SELECT * FROM fototrappole WHERE operator = :operator")
        fototrappole = (
            conn.execute(sql, {"operator": session["username"]}).mappings().all()
        )
    return render_template("elenco_fototrappole.html", fototrappole=fototrappole)


def extract_frame(video_path, time_sec):
    cap = cv2.VideoCapture(video_path)

    # Set video position (in milliseconds)
    cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)

    success, frame = cap.read()
    if success:
        # target width
        new_width = 640
        # compute scale factor
        h, w = frame.shape[:2]
        scale = new_width / w
        new_height = int(h * scale)

        # resize frame
        resized = cv2.resize(frame, (new_width, new_height))

        ok, jpg = cv2.imencode(".jpg", resized)
        if not ok:
            cap.release()
            return None

        return jpg.tobytes()
    else:
        print("Failed to extract frame")

    cap.release()


@app.route("/get_fototrappola_data", methods=["GET"])
def get_fototrappola_data():
    camtrap_id = request.args.get("camtrap_id")
    if not camtrap_id:
        return ""
    print(f"{camtrap_id=}")
    with engine.connect() as conn:
        fototrappola = (
            conn.execute(
                text("SELECT * FROM fototrappole WHERE codice = :codice"),
                {"codice": camtrap_id},
            )
            .mappings()
            .fetchone()
        )
    out = (
        f"{fototrappola.tipo}<br>{fototrappola.nome} {fototrappola.cognome}<br>"
        f"{fototrappola.comune} {fototrappola.provincia} {fototrappola.regione}"
    )

    return out


@app.route(APP_ROOT + "/nuova_fototrappola", methods=["GET", "POST"])
@login_required
def nuova_fototrappola():
    """
    save nuova fototrappola
    """
    if request.method == "GET":
        return render_template("nuova_fototrappola.html")


@app.route(APP_ROOT + "/save_info", methods=["POST"])
@login_required
def save_info():
    """
    save info into db
    """

    # operator = request.form.get("operator")
    camtrap_id = request.form.get("camtrap_id")
    code = request.form.get("code")
    wolf_number = request.form.get("wolf_number")
    scalp = request.form.get("scalp")
    notes = request.form.get("note") if request.form.get("note") else None
    latitude = request.form.get("latitude")
    longitude = request.form.get("longitude")
    transect_id = (
        request.form.get("transect_id") if request.form.get("transect_id") else None
    )

    original_file_name = request.form.get("original_file_name")
    new_file_name = request.form.get("new_file_name")
    file_content_md5 = request.form.get("file_content_md5")
    date = request.form.get("date")
    time_ = request.form.get("time_")
    video_url = url_for("uploaded_file", filename=new_file_name)

    with engine.connect() as conn:
        # check if code already present in database
        query = text("SELECT COUNT(*) FROM sighting WHERE code = :code")
        n_code = conn.execute(query, {"code": code}).scalar()
        if n_code:
            flash(
                Markup(
                    f"Il codice dell'avistamento <b>{code}</b> è già presente nel database"
                ),
                "danger",
            )

            # list of fototrappole
            with engine.connect() as conn:
                fototrappole = (
                    conn.execute(
                        text(
                            "SELECT codice FROM fototrappole WHERE operator = :operator"
                        ),
                        {"operator": session["username"]},
                    )
                    .mappings()
                    .all()
                )

                fototrappola = (
                    conn.execute(
                        text("SELECT * FROM fototrappole WHERE codice = :camtrap_id"),
                        {"camtrap_id": camtrap_id},
                    )
                    .mappings()
                    .fetchone()
                )
                fototrappola_details = (
                    f"{fototrappola.tipo}<br>{fototrappola.nome} {fototrappola.cognome}<br>"
                    f"{fototrappola.comune} {fototrappola.provincia} {fototrappola.regione}"
                )

            return render_template(
                "upload_info.html",
                original_file_name=original_file_name,
                new_file_name=str(new_file_name),
                video_url=video_url,
                operator=session["fullname"],
                code=code,
                date=date,
                time_=time_,
                file_content_md5=file_content_md5,
                camtrap_id=camtrap_id,
                wolf_number=wolf_number,
                notes=notes if notes is not None else "",
                latitude=latitude,
                longitude=longitude,
                transect_id=transect_id if transect_id is not None else "",
                scalp=scalp,
                fototrappole=fototrappole,
                fototrappola_details=Markup(fototrappola_details),
            )

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
                "wolf_number": wolf_number,
                "latitude": latitude,
                "longitude": longitude,
                "notes": notes,
            },
        )
        sighting_id = result.scalar()
        conn.commit()

        # save media
        jpg_content = extract_frame(
            str(Path(app.config["UPLOAD_FOLDER"]) / Path(new_file_name)), 1
        )

        query = text("""
            INSERT INTO media
                (original_file_name, new_file_name, file_content_md5, sighting_id, image)
            VALUES
                (:original_file_name, :new_file_name, :file_content_md5, :sighting_id, :image)
        """)

        result = conn.execute(
            query,
            {
                "original_file_name": original_file_name,
                "new_file_name": new_file_name,
                "file_content_md5": file_content_md5,
                "sighting_id": sighting_id,
                "image": jpg_content,
            },
        )
        conn.commit()

        flash("Avistamento salvato.", "success")

    return redirect(url_for("index"))


@app.route(APP_ROOT + "/save_fototrappola", methods=["POST"])
@login_required
def save_fototrappola():
    """
    save nuova fototrappola
    """
    if request.method == "POST":
        try:
            # Recupero dati dalla form
            codice = request.form.get("codice")
            tipo = request.form.get("tipo")
            data_inizio = request.form.get("data_inizio")
            data_fine = request.form.get("data_fine")
            nome = request.form.get("nome")
            cognome = request.form.get("cognome")
            regione = request.form.get("regione")
            provincia = request.form.get("provincia")
            comune = request.form.get("comune")
            country = request.form.get("country", "Italia")
            latitudine = request.form.get("latitudine")
            longitudine = request.form.get("longitudine")
            altitudine = request.form.get("altitudine")
            intersezioni = request.form.get("intersezioni")

            # Validazione minima (puoi ampliarla)
            if not codice or not tipo:
                flash("Codice e Tipo sono obbligatori", "error")
                return redirect(url_for("nuova_fototrappola"))

            # Query SQL parametrizzata (sicura contro SQL injection)
            query = text("""
                INSERT INTO fototrappole (
                    codice, tipo, data_inizio, data_fine, nome, cognome,
                    regione, provincia, comune, country,
                    latitudine, longitudine, altitudine, intersezioni, operator
                )
                VALUES (
                    :codice, :tipo, :data_inizio, :data_fine, :nome, :cognome,
                    :regione, :provincia, :comune, :country,
                    :latitudine, :longitudine, :altitudine, :intersezioni, :operator
                )
            """)

            # Esecuzione query
            with engine.connect() as conn:
                conn.execute(
                    query,
                    {
                        "codice": codice,
                        "tipo": tipo,
                        "data_inizio": data_inizio,
                        "data_fine": data_fine if data_fine != "" else None,
                        "nome": nome,
                        "cognome": cognome,
                        "regione": regione,
                        "provincia": provincia,
                        "comune": comune,
                        "country": country,
                        "latitudine": float(latitudine),
                        "longitudine": float(longitudine),
                        "altitudine": float(altitudine) if altitudine else None,
                        "intersezioni": intersezioni,
                        "operator": session["username"],
                    },
                )

                conn.commit()

            flash("Nuova fototrappola inserita con successo!", "success")
            return redirect(url_for("index"))

        except Exception as e:
            print("Errore INSERT fototrappola:", e)
            flash(f"Errore durante il salvataggio {e}", "error")
            return redirect(url_for("nuova_fototrappola"))


@app.route(APP_ROOT + "/sighting_list")
@login_required
def sighting_list():
    with engine.connect() as conn:
        query = text(
            "SELECT code, operator, camtrap_id, image FROM sighting, media WHERE sighting.id=media.sighting_id AND operator = :operator"
        )
        rows = conn.execute(query, {"operator": session["username"]}).mappings().all()
    results = []
    for row in rows:
        results.append(
            {
                # "image": base64.b64encode(row["image"]).decode("ascii"),
                "data_uri": f"data:image/jpeg;base64,{base64.b64encode(row['image']).decode('ascii')}",
                "code": row["code"],
            }
        )
    return render_template("sighting_list.html", sightings=results)


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
        app.logger.info("Nessun file video caricato")
        flash("Nessun file video caricato!", "danger")
        return redirect(url_for("index"))

    # compute MD5 incrementally
    md5 = hashlib.md5()
    chunk_size = 8192  # 8 KB

    # Read the file stream in chunks
    for chunk in iter(lambda: video.stream.read(chunk_size), b""):
        md5.update(chunk)
    video.stream.seek(0)
    file_content_md5 = md5.hexdigest()
    app.logger.info(f"{file_content_md5=}")

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
        if row is not None:
            flash(
                f"Il file {video.filename} è già presente nel database: {row['operator']}, {row['code']}, {row['camtrap_id']}",
                "danger",
            )
            return redirect(url_for("upload_video_form"))

    original_file_name = secure_filename(video.filename)

    app.logger.info(f"{original_file_name=}")

    # get new file name
    new_file_name = Path(f"{int(time.time())}_{session['username']}").with_suffix(
        Path(original_file_name).suffix
    )
    app.logger.info(f"{new_file_name=}")

    save_path = Path(app.config["UPLOAD_FOLDER"]) / new_file_name
    video.save(save_path)

    # md5 of file content
    # file_content_md5 = hashlib.md5(open(save_path, "rb").read()).hexdigest()
    # print(file_content_md5)

    video_url = url_for("uploaded_file", filename=new_file_name)
    flash("Video caricato con successo!", "success")

    # check date time
    data = camtrap_banner_decoder.extract_date_time(
        save_path,
        Namespace(
            debug=False,
        ),
    )
    app.logger.info(f"{data=}")

    time_ = ""

    code: str = ""
    if data["date"]:
        code = data["date"][2:].replace("-", "") + session["code"]
    else:
        code = session["code"]
    try:
        time_ = data["time"][:2] + ":" + data["time"][2:4] + ":" + data["time"][4:6]
    except Exception:
        time_ = data["time"]

    print(session["fullname"])

    # list of fototrappole
    with engine.connect() as conn:
        fototrappole = (
            conn.execute(
                text("SELECT codice FROM fototrappole WHERE operator = :operator"),
                {"operator": session["username"]},
            )
            .mappings()
            .all()
        )

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
        fototrappole=fototrappole,
    )


@app.route(APP_ROOT + "/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route(APP_ROOT + "/view/<int:sighting_id>")
@login_required
def view(sighting_id: int):
    with engine.connect() as conn:
        query = text(
            (
                "SELECT sighting.id, code, operator, camtrap_id, scalp, image, wolf_number , notes "
                "FROM media, sighting WHERE sighting.id = media.sighting_id AND sighting.id = :sighting_id"
            )
        )
        row = conn.execute(query, {"sighting_id": sighting_id}).mappings().fetchone()

    user = get_user(row["operator"])
    return render_template(
        "view.html",
        row=row,
        user=user["fullname"],
        institution=user["institution"],
        data_uri=f"data:image/jpeg;base64,{base64.b64encode(row['image']).decode('ascii')}",
    )


if __name__ == "__main__":
    app.run(debug=DEBUG, host="0.0.0.0", port=5000)
