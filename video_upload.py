import base64
import hashlib
import os
import time
from pathlib import Path

import cv2
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
from markupsafe import Markup
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
            flash("Devi effettuare il login per accedere a questa pagina.", "danger")
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
    session.pop("username", None)
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
            # 1️⃣  Recupero dati dalla form
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

            # 2️⃣ Validazione minima (puoi ampliarla)
            if not codice or not tipo:
                flash("Codice e Tipo sono obbligatori", "error")
                return redirect(url_for("nuova_fototrappola"))

            # 3️⃣ Query SQL parametrizzata (sicura contro SQL injection)
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

            # 4️⃣ Esecuzione query
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
            raise
            print("Errore INSERT fototrappola:", e)
            flash("Errore durante il salvataggio.", "error")
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
                f"Il file {video.filename} è già presente nel database: {row['operator']}, {row['code']}, {row['camtrap_id']}",
                "danger",
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
    flash("Video caricato con successo!", "success")

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

    return render_template(
        "view.html",
        row=row,
        user=USERS[row["operator"]]["fullname"],
        institution=USERS[row["operator"]]["institution"],
        data_uri=f"data:image/jpeg;base64,{base64.b64encode(row['image']).decode('ascii')}",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
