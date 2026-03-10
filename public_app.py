"""
public_app.py - Public-facing Flask web app for CV tailoring.

Two-page flow (requires login + credits):
  Page 1 (/): Upload CVs + enter role types → generates one role-optimised CV per role
  Page 2 (/tailor): Select a role CV + enter job details → tailors CV to a specific job

Auth: Flask-Login with email/password stored in SQLite
Payments: Stripe Checkout (€1.99 = 5 runs). Credits deducted per tailoring job.
"""

import os
import re
import shutil
import sqlite3
import threading
import uuid
from functools import wraps
from pathlib import Path

import stripe
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import database
from cv_generator import generate_all_role_cvs
from cv_tailor import (
    apply_suggestions,
    extract_cv_paragraphs,
    generate_pdf,
    get_tailoring_suggestions,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="web_templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

UPLOAD_BASE = Path(__file__).parent / "uploads"
UPLOAD_BASE.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"docx"}

CREDITS_PER_PACK = 5
PACK_PRICE_EUROS = 1.99

# In-memory job store
jobs: dict = {}
jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Flask-Login
# ---------------------------------------------------------------------------

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access CV Tailor."
login_manager.login_message_category = "warning"


class User(UserMixin):
    def __init__(self, id, email, credits):
        self.id = id
        self.email = email
        self.credits = credits

    @staticmethod
    def from_row(row):
        if row is None:
            return None
        return User(row["id"], row["email"], row["credits"])


@login_manager.user_loader
def load_user(user_id):
    row = database.get_user_by_id(int(user_id))
    return User.from_row(row)


@app.context_processor
def inject_user_credits():
    """Make get_fresh_credits() available in all templates."""
    def get_fresh_credits():
        if not current_user.is_authenticated:
            return 0
        row = database.get_user_by_id(current_user.id)
        return row["credits"] if row else 0
    return {"get_fresh_credits": get_fresh_credits}


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    return key


def get_session_id() -> str:
    if "cv_session_id" not in session:
        session["cv_session_id"] = str(uuid.uuid4())
    return session["cv_session_id"]


def get_session_dir() -> Path:
    d = UPLOAD_BASE / get_session_id()
    d.mkdir(exist_ok=True)
    return d


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text).strip("_")


def get_user_generated_dir(user_id: int) -> Path:
    """Stable, user-specific directory for generated role CVs (survives across sessions)."""
    d = UPLOAD_BASE / f"user_{user_id}" / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def credits_required(f):
    """Decorator: redirect to pricing if user has no credits."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        # Reload fresh credit count from DB
        row = database.get_user_by_id(current_user.id)
        if not row or row["credits"] < 1:
            flash("You need credits to use CV Tailor. Purchase a pack below.", "info")
            return redirect(url_for("pricing"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------

def _run_generation(job_id: str, input_paths: list, role_types: list, output_dir: str):
    """Background thread: generate role CVs from uploaded CVs."""
    try:
        api_key = get_api_key()
        results = generate_all_role_cvs(input_paths, role_types, output_dir, api_key)
        with jobs_lock:
            jobs[job_id] = {"status": "done", "results": results}
    except Exception as e:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(e)}


def _run_tailoring(
    job_id: str,
    template_path: str,
    jd_text: str,
    output_stem: str,
    output_dir: str,
):
    """Background thread: tailor a role CV to a specific job posting."""
    try:
        api_key = get_api_key()
        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(parents=True, exist_ok=True)
        docx_path = str(output_dir_path / f"{output_stem}.docx")
        pdf_path = str(output_dir_path / f"{output_stem}.pdf")

        paragraphs = extract_cv_paragraphs(template_path)
        suggestions = get_tailoring_suggestions(jd_text, paragraphs, api_key)
        applied, skipped = apply_suggestions(template_path, docx_path, suggestions)

        # PDF conversion — gracefully skipped on Linux (Railway)
        pdf_available = False
        try:
            generate_pdf(docx_path, pdf_path)
            pdf_available = Path(pdf_path).exists()
        except Exception:
            pass

        with jobs_lock:
            jobs[job_id] = {
                "status": "done",
                "result": {
                    "docx_path": docx_path,
                    "pdf_path": pdf_path if pdf_available else None,
                    "pdf_available": pdf_available,
                    "applied": applied,
                    "applied_count": len(applied),
                },
            }
    except Exception as e:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/register")
def register_get():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("register.html")


@app.post("/register")
def register_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not email or not password:
        flash("Email and password are required.", "danger")
        return render_template("register.html")
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return render_template("register.html")
    if password != confirm:
        flash("Passwords do not match.", "danger")
        return render_template("register.html")

    try:
        user_id = database.create_user(email, generate_password_hash(password))
    except sqlite3.IntegrityError:
        flash("An account with that email already exists.", "danger")
        return render_template("register.html")

    row = database.get_user_by_id(user_id)
    login_user(User.from_row(row))
    # Clear any stale CV session state from a previous user's session in this browser.
    # A brand-new user can never have legitimately generated role CVs yet.
    # (pending_generation / role_types are preserved — needed if they uploaded CVs before registering)
    session.pop("role_cvs", None)
    session.pop("generated_dir", None)
    # New users always go to pricing first (0 credits)
    return redirect(url_for("pricing"))


@app.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    row = database.get_user_by_email(email)
    if not row or not check_password_hash(row["password_hash"], password):
        flash("Invalid email or password.", "danger")
        return render_template("login.html")

    login_user(User.from_row(row))

    # If files were uploaded before login, resume generation
    if session.get("pending_generation"):
        return redirect(url_for("generate_pending"))

    # Restore role CVs from DB (works across browsers and sessions)
    saved = database.get_role_cvs(row["id"])
    if saved:
        session["role_cvs"] = saved["role_cvs"]
        session["generated_dir"] = saved["generated_dir"]
        return redirect(url_for("tailor_page"))

    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Pricing / Stripe routes
# ---------------------------------------------------------------------------

@app.get("/pricing")
@login_required
def pricing():
    row = database.get_user_by_id(current_user.id)
    credits = row["credits"] if row else 0
    return render_template(
        "pricing.html",
        credits=credits,
        credits_per_pack=CREDITS_PER_PACK,
        pack_price=PACK_PRICE_EUROS,
    )


@app.post("/checkout")
@login_required
def checkout():
    """Create a Stripe Checkout session and redirect the user to it."""
    if not stripe.api_key:
        flash("Payment is not configured. Please contact support.", "danger")
        return redirect(url_for("pricing"))

    domain = request.host_url.rstrip("/")
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": int(PACK_PRICE_EUROS * 100),  # in cents (799)
                    "product_data": {
                        "name": f"CV Tailor — {CREDITS_PER_PACK} tailoring runs",
                        "description": (
                            f"Tailor your CV to {CREDITS_PER_PACK} specific job postings. "
                            "Each run rewrites your CV for one job description using AI."
                        ),
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            client_reference_id=str(current_user.id),
            success_url=domain + url_for("payment_success") + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=domain + url_for("pricing"),
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        flash(f"Could not start checkout: {e}", "danger")
        return redirect(url_for("pricing"))


@app.get("/payment/success")
@login_required
def payment_success():
    flash(
        f"Payment successful! {CREDITS_PER_PACK} tailoring runs have been added to your account.",
        "success",
    )
    # If files were uploaded before payment, resume generation now
    if session.get("pending_generation"):
        return redirect(url_for("generate_pending"))
    return redirect(url_for("index"))


@app.post("/stripe/webhook")
def stripe_webhook():
    """Handle Stripe webhook events to credit user accounts."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(
                stripe.util.convert_to_stripe_object(
                    stripe.util.convert_to_dict(
                        stripe.api_requestor.json.loads(payload)
                    )
                ),
                stripe.api_key,
            )
    except (ValueError, stripe.error.SignatureVerificationError):
        return "Invalid payload", 400

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        user_id = checkout_session.get("client_reference_id")
        payment_status = checkout_session.get("payment_status")

        if user_id and payment_status == "paid":
            try:
                database.add_credits(int(user_id), CREDITS_PER_PACK)
            except Exception:
                pass  # Log in production; don't fail the webhook

    return "", 200


# ---------------------------------------------------------------------------
# Page 1 routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    # Logged-in users with an active session go straight to tailor
    if current_user.is_authenticated and session.get("role_cvs"):
        return redirect(url_for("tailor_page"))
    return render_template("index.html")


@app.post("/setup")
def setup():
    """Handle CV uploads + role types. Saves files then checks auth/credits."""
    files = request.files.getlist("cv_files")
    role_types_raw = request.form.getlist("role_types")
    role_types = [r.strip() for r in role_types_raw if r.strip()]

    valid_files = [f for f in files if f and f.filename and allowed_file(f.filename)]
    if not valid_files:
        return jsonify({"error": "Please upload at least one .docx CV file."}), 400
    if not role_types:
        return jsonify({"error": "Please enter at least one role type."}), 400

    session_dir = get_session_dir()
    input_dir = session_dir / "input"
    input_dir.mkdir(exist_ok=True)
    # Use stable user-based dir when logged in so files persist across browser sessions
    if current_user.is_authenticated:
        generated_dir = get_user_generated_dir(current_user.id)
    else:
        generated_dir = session_dir / "generated"
        generated_dir.mkdir(exist_ok=True)

    saved_paths = []
    for f in valid_files:
        filename = secure_filename(f.filename)
        save_path = input_dir / filename
        f.save(str(save_path))
        saved_paths.append(str(save_path))

    session["role_types"] = role_types
    session["generated_dir"] = str(generated_dir)

    # Not logged in → save state, tell frontend to go register
    if not current_user.is_authenticated:
        session["pending_generation"] = True
        return jsonify({"status": "need_auth", "redirect": url_for("register_get")})

    # Logged in but no credits → save state, send to pricing
    row = database.get_user_by_id(current_user.id)
    if not row or row["credits"] < 1:
        session["pending_generation"] = True
        return jsonify({"status": "need_payment", "redirect": url_for("pricing")})

    # Ready — start generation immediately
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running"}

    t = threading.Thread(
        target=_run_generation,
        args=(job_id, saved_paths, role_types, str(generated_dir)),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "ok", "job_id": job_id})


@app.get("/generate")
@login_required
def generate_pending():
    """Resume generation using files uploaded before login/payment."""
    row = database.get_user_by_id(current_user.id)
    if not row or row["credits"] < 1:
        flash("Please purchase runs to continue.", "info")
        return redirect(url_for("pricing"))

    role_types = session.get("role_types", [])
    cv_session_id = session.get("cv_session_id")

    if not role_types or not cv_session_id:
        flash("Upload data expired. Please upload your CVs again.", "warning")
        return redirect(url_for("index"))

    input_dir = UPLOAD_BASE / cv_session_id / "input"
    saved_paths = sorted([str(p) for p in input_dir.glob("*.docx")]) if input_dir.exists() else []

    if not saved_paths:
        flash("Upload data expired. Please upload your CVs again.", "warning")
        return redirect(url_for("index"))

    # Always use the stable user-based dir
    generated_dir = get_user_generated_dir(current_user.id)
    session["generated_dir"] = str(generated_dir)
    session.pop("pending_generation", None)

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running"}

    t = threading.Thread(
        target=_run_generation,
        args=(job_id, saved_paths, role_types, str(generated_dir)),
        daemon=True,
    )
    t.start()

    return render_template("index.html", pending_job_id=job_id, role_count=len(role_types))


@app.get("/setup-status/<job_id>")
def setup_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404

    if job["status"] == "done":
        results = job["results"]
        role_cvs = [{"role": r["role"], "filename": r["filename"]} for r in results]
        session["role_cvs"] = role_cvs
        # Persist to DB so returning users on any browser go straight to tailor
        if current_user.is_authenticated:
            generated_dir = session.get("generated_dir", "")
            database.save_role_cvs(current_user.id, role_cvs, generated_dir)
            # Also store each DOCX as a blob so files survive server restarts
            for r in results:
                file_path = Path(generated_dir) / r["filename"]
                if file_path.exists():
                    database.save_role_cv_file(
                        current_user.id, r["filename"], file_path.read_bytes()
                    )
        return jsonify({"status": "done", "count": len(results)})

    return jsonify({"status": job["status"], "error": job.get("error")})


# ---------------------------------------------------------------------------
# Page 2 routes
# ---------------------------------------------------------------------------

def _restore_role_cvs_from_db():
    """Populate session from DB if role_cvs is missing (e.g. session cookie expired).
    Returns True if role_cvs are now available in session, False otherwise."""
    if session.get("role_cvs"):
        return True
    if not current_user.is_authenticated:
        return False
    saved = database.get_role_cvs(current_user.id)
    if saved:
        session["role_cvs"] = saved["role_cvs"]
        session["generated_dir"] = saved["generated_dir"]
        return True
    return False


def _ensure_role_cv_files_on_disk():
    """Restore any missing role CV DOCX files from DB blobs (handles server restarts).
    Called before any operation that reads role CV files from disk."""
    if not current_user.is_authenticated:
        return
    generated_dir = session.get("generated_dir")
    role_cvs = session.get("role_cvs")
    if not generated_dir or not role_cvs:
        return
    generated_path = Path(generated_dir)
    missing = [cv["filename"] for cv in role_cvs
               if not (generated_path / cv["filename"]).exists()]
    if not missing:
        return
    generated_path.mkdir(parents=True, exist_ok=True)
    blobs = database.get_role_cv_files(current_user.id)
    blob_map = {b["filename"]: b["content"] for b in blobs}
    for filename in missing:
        if filename in blob_map:
            (generated_path / filename).write_bytes(blob_map[filename])


@app.get("/tailor")
@login_required
def tailor_page():
    _restore_role_cvs_from_db()
    role_cvs = session.get("role_cvs")
    if not role_cvs:
        flash("Please upload your CVs first.", "warning")
        return redirect(url_for("index"))
    row = database.get_user_by_id(current_user.id)
    credits = row["credits"] if row else 0
    return render_template("tailor.html", role_cvs=role_cvs, credits=credits)


@app.post("/tailor")
@login_required
def tailor_submit():
    _restore_role_cvs_from_db()
    _ensure_role_cv_files_on_disk()
    role_cvs = session.get("role_cvs")
    generated_dir = session.get("generated_dir")

    if not role_cvs or not generated_dir:
        return jsonify({"error": "Session expired. Please start over."}), 400

    # Check and deduct credit BEFORE spawning the job
    try:
        database.deduct_credit(current_user.id)
    except ValueError:
        return jsonify({"error": "No credits remaining. Please purchase more runs."}), 402

    cv_index = request.form.get("cv_index", type=int)
    company = request.form.get("company", "").strip()
    job_title = request.form.get("job_title", "").strip()
    jd_text = request.form.get("jd_text", "").strip()

    jd_file = request.files.get("jd_file")
    if not jd_text and jd_file and jd_file.filename:
        jd_text = jd_file.read().decode("utf-8", errors="replace").strip()

    if cv_index is None or cv_index < 0 or cv_index >= len(role_cvs):
        return jsonify({"error": "Invalid CV selection."}), 400
    if not company:
        return jsonify({"error": "Please enter a company name."}), 400
    if not job_title:
        return jsonify({"error": "Please enter a job title."}), 400
    if not jd_text:
        return jsonify({"error": "Please provide a job description."}), 400

    selected_cv = role_cvs[cv_index]
    template_path = str(Path(generated_dir) / selected_cv["filename"])

    if not Path(template_path).exists():
        return jsonify({"error": "Selected CV file not found. Please start over."}), 400

    session_dir = get_session_dir()
    output_dir = str(session_dir / "output")
    output_stem = f"CV_{safe_name(company)}_{safe_name(job_title)}"

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running"}

    t = threading.Thread(
        target=_run_tailoring,
        args=(job_id, template_path, jd_text, output_stem, output_dir),
        daemon=True,
    )
    t.start()

    # Return updated credit count so UI can refresh
    row = database.get_user_by_id(current_user.id)
    credits_remaining = row["credits"] if row else 0

    return jsonify({"job_id": job_id, "credits_remaining": credits_remaining})


@app.get("/job/<job_id>")
@login_required
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.get("/download-role/<int:cv_index>")
@login_required
def download_role_cv(cv_index):
    """Download a generated role CV (template version, before job tailoring)."""
    _restore_role_cvs_from_db()
    _ensure_role_cv_files_on_disk()
    role_cvs = session.get("role_cvs", [])
    generated_dir = session.get("generated_dir")

    if cv_index < 0 or cv_index >= len(role_cvs) or not generated_dir:
        return "File not found", 404

    cv = role_cvs[cv_index]
    path = Path(generated_dir) / cv["filename"]

    if not path.exists():
        return "File not found", 404

    return send_file(
        str(path),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"CV_{safe_name(cv['role'])}.docx",
    )


@app.get("/download/<job_id>/<filetype>")
@login_required
def download(job_id, filetype):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job.get("status") != "done":
        return "File not ready", 404

    result = job.get("result", {})

    if filetype == "docx":
        path = result.get("docx_path")
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        suffix = ".docx"
    elif filetype == "pdf":
        path = result.get("pdf_path")
        mimetype = "application/pdf"
        suffix = ".pdf"
    else:
        return "Invalid file type", 400

    if not path or not Path(path).exists():
        return "File not found", 404

    return send_file(
        path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=Path(path).stem + suffix,
    )


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

@app.route("/reset", methods=["GET", "POST"])
@login_required
def reset():
    cv_session_id = session.get("cv_session_id")
    if cv_session_id:
        session_dir = UPLOAD_BASE / cv_session_id
        if session_dir.exists():
            shutil.rmtree(str(session_dir), ignore_errors=True)
    # Clear only CV-related session keys, keep login session
    for key in ("cv_session_id", "role_types", "generated_dir", "role_cvs"):
        session.pop(key, None)
    # Also clear DB so _restore_role_cvs_from_db doesn't repopulate session
    if current_user.is_authenticated:
        database.clear_role_cvs(current_user.id)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

database.init_db()

if __name__ == "__main__":
    app.run(debug=False, port=5001, threaded=True)
