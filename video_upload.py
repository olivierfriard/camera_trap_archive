import os


from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
    session,
)
from werkzeug.utils import secure_filename
import camtrap_banner_decoder
from sqlalchemy import create_engine, text

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

USERS = {
    "admin": {
        "pwd": "admin123",
        "fullname": "Administrator",
        "institution": "Università di Torino",
        "code": "UNI",
    },
    "user": {
        "pwd": "user123",
        "fullname": "User Name",
        "institution": "Università di Torino",
        "code": "UNI",
    },
}


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
    video = request.files.get("video")

    if not video:
        flash("Nessun file video caricato!")
        return redirect(url_for("index"))

    original_filename = secure_filename(video.filename)
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], original_filename)
    video.save(save_path)

    video_url = url_for("uploaded_file", filename=original_filename)
    flash(
        f"Video caricato con successo! <!--<a href='{video_url}' target='_blank'>Apri file</a>-->"
    )

    # check date time
    data = camtrap_banner_decoder.extract_date_time(save_path)
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

    print(session['fullname'])

    return render_template(
        "upload_info.html",
        video_filename=original_filename,
        video_url=video_url,
        operator=session['fullname'],
        code=code,
        date=data["date"],
        time_=time_,
    )


@app.route(APP_ROOT + "/save_info", methods=["POST"])
@login_required
def save_info():
    operator = request.form.get("operator")
    camtrap_id = request.form.get("camtrap_id")
    code = request.form.get("code")
    numero_lupi = request.form.get("numero_lupi")
    scalp = request.form.get("scalp")
    notes = request.form.get("note") if request.form.get("note") else None

    lat = request.form.get("latitude")
    lng = request.form.get("longitude")
    transect_id = request.form.get("transect_id") if request.form.get("transect_id") else None


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
                "operator": operator,
                "institution":session['institution'],
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

    return f"OK {sighting_id}"


@app.route(APP_ROOT + "/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
