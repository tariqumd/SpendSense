import math
import os
import secrets
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from functools import wraps
from json import loads
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Flask, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import OperationalError
from werkzeug.security import check_password_hash, generate_password_hash

from models import CategoryRule, Transaction, User, db
from parser import (
    CREDIT_CATEGORY_KEYWORDS,
    DEFAULT_CATEGORY_KEYWORDS,
    normalize_keywords,
    normalize_rule_name,
    parse_expense_input,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def load_environment_file():
    base_dir = os.path.dirname(__file__)

    def load_key_values(env_path):
        if not os.path.exists(env_path):
            return

        with open(env_path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)

    load_key_values(os.path.join(base_dir, ".env"))

    app_env = (os.getenv("APP_ENV") or "dev").strip().lower()
    if app_env not in {"dev", "prod"}:
        app_env = "dev"

    load_key_values(os.path.join(base_dir, f".env.{app_env}"))

    return app_env


APP_ENV = load_environment_file()


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_category_rules():
    rules = OrderedDict()

    for rule in CategoryRule.query.order_by(CategoryRule.created_at.asc()).all():
        rules[normalize_rule_name(rule.name)] = normalize_keywords(rule.keywords.split(","))

    for name, keywords in DEFAULT_CATEGORY_KEYWORDS.items():
        rules.setdefault(name, [])
        existing = set(rules[name])
        for keyword in normalize_keywords(keywords):
            if keyword not in existing:
                rules[name].append(keyword)
                existing.add(keyword)

    return rules


def post_form_json(url, data):
    encoded = urlencode(data).encode("utf-8")
    request_obj = Request(url, data=encoded, method="POST")
    request_obj.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(request_obj) as response:
        return loads(response.read().decode("utf-8"))


def get_json(url, headers=None):
    request_obj = Request(url, method="GET")
    for key, value in (headers or {}).items():
        request_obj.add_header(key, value)
    with urlopen(request_obj) as response:
        return loads(response.read().decode("utf-8"))


def is_safe_next_url(target):
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.netloc and (parsed.path or "").startswith("/")


def build_auth_redirect_target(target, fallback_endpoint):
    return target if is_safe_next_url(target) else url_for(fallback_endpoint)


def ensure_schema():
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())

    if "transactions" not in table_names and "expense" in table_names:
        try:
            db.session.execute(text("ALTER TABLE expense RENAME TO transactions"))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
        inspector = inspect(db.engine)
        table_names = set(inspector.get_table_names())

    if "transactions" not in table_names:
        return

    transaction_columns = {column["name"] for column in inspector.get_columns("transactions")}

    if "category_source" not in transaction_columns:
        db.session.execute(
            text("ALTER TABLE transactions ADD COLUMN category_source VARCHAR(20) DEFAULT 'auto'")
        )
        db.session.execute(
            text("UPDATE transactions SET category_source = 'auto' WHERE category_source IS NULL")
        )
        db.session.commit()

    if "transaction_type" not in transaction_columns:
        db.session.execute(
            text("ALTER TABLE transactions ADD COLUMN transaction_type VARCHAR(20) DEFAULT 'debit'")
        )
        db.session.execute(
            text("UPDATE transactions SET transaction_type = 'debit' WHERE transaction_type IS NULL")
        )
        db.session.commit()

    if "user_id" not in transaction_columns:
        db.session.execute(text("ALTER TABLE transactions ADD COLUMN user_id INTEGER"))
        db.session.commit()

    if "user" in table_names:
        user_columns = {column["name"] for column in inspector.get_columns("user")}
        if "password_hash" not in user_columns:
            db.session.execute(text("ALTER TABLE user ADD COLUMN password_hash VARCHAR(255)"))
            db.session.commit()


def get_category_options():
    debit_categories = list(build_category_rules().keys())
    credit_categories = list(CREDIT_CATEGORY_KEYWORDS.keys())
    return {
        "debit": debit_categories,
        "credit": credit_categories,
    }


def recategorize_transactions(user_id=None):
    rules = build_category_rules()
    updated_count = 0

    query = Transaction.query.filter(
        (Transaction.category_source.is_(None)) | (Transaction.category_source != "manual")
    )

    if user_id is not None:
        query = query.filter(Transaction.user_id == user_id)

    transactions = query.all()

    for transaction in transactions:
        parsed = parse_expense_input(transaction.note, extra_rules=rules)
        if transaction.category != parsed["category"]:
            transaction.category = parsed["category"]
            transaction.category_source = "auto"
            updated_count += 1

    return updated_count


def get_day_bounds(target_date):
    return datetime.combine(target_date, time.min), datetime.combine(target_date, time.max)


def parse_date_value(raw_value, fallback):
    value = (raw_value or "").strip()
    if not value:
        return fallback

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def build_redirect_target(target_url, fallback_endpoint, **fallback_values):
    target = (target_url or "").strip()
    return target or url_for(fallback_endpoint, **fallback_values)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path))
        return view_func(*args, **kwargs)

    return wrapped_view


def get_oauth_redirect_uri():
    configured_uri = (os.getenv("GOOGLE_REDIRECT_URI") or "").strip()
    if configured_uri:
        return configured_uri
    return url_for("google_callback", _external=True)


def upsert_google_user(user_info):
    email = (user_info.get("email") or "").strip().lower()
    name = (user_info.get("name") or email or "SpendSense User").strip()
    google_sub = (user_info.get("sub") or "").strip()
    profile_picture = (user_info.get("picture") or "").strip() or None

    if not email or not google_sub:
        raise ValueError("Google login did not return the required account details.")

    if not user_info.get("email_verified"):
        raise ValueError("Please use a Google account with a verified email.")

    user = User.query.filter(
        (User.google_sub == google_sub) | (func.lower(User.email) == email)
    ).first()

    if user is None:
        user = User(
            email=email,
            name=name,
            google_sub=google_sub,
            profile_picture=profile_picture,
            last_login_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.flush()
    else:
        user.email = email
        user.name = name
        user.google_sub = google_sub
        user.profile_picture = profile_picture
        user.last_login_at = datetime.utcnow()

    return user


def validate_account_email(email):
    normalized_email = (email or "").strip().lower()

    if not normalized_email:
        raise ValueError("Email is required.")

    return normalized_email


def create_or_update_email_user(name, email, password):
    normalized_email = validate_account_email(email)
    display_name = (name or "").strip() or normalized_email.split("@")[0].title()

    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters long.")

    user = User.query.filter(func.lower(User.email) == normalized_email).first()
    password_hash = generate_password_hash(password)

    if user is None:
        user = User(
            email=normalized_email,
            name=display_name,
            google_sub=f"email::{normalized_email}",
            password_hash=password_hash,
            last_login_at=datetime.utcnow(),
        )
        db.session.add(user)
        db.session.flush()
    else:
        if user.password_hash:
            raise ValueError("An account already exists for this email. Please sign in instead.")
        user.name = display_name or user.name
        user.password_hash = password_hash
        user.last_login_at = datetime.utcnow()

    return user


def authenticate_email_user(email, password):
    normalized_email = validate_account_email(email)
    user = User.query.filter(func.lower(User.email) == normalized_email).first()

    if user is None or not user.password_hash:
        raise ValueError("No email/password account was found for this address.")

    if not check_password_hash(user.password_hash, password or ""):
        raise ValueError("Incorrect email or password.")

    user.last_login_at = datetime.utcnow()
    return user


def build_dashboard_filters(args):
    today = date.today()
    filter_type = (args.get("range") or "month").strip().lower()
    start_date_raw = (args.get("start_date") or "").strip()
    end_date_raw = (args.get("end_date") or "").strip()

    if filter_type == "today":
        start_date = today
        end_date = today
    elif filter_type == "yesterday":
        start_date = today - timedelta(days=1)
        end_date = today - timedelta(days=1)
    elif filter_type == "week":
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    elif filter_type == "custom" and start_date_raw and end_date_raw:
        try:
            start_date = datetime.strptime(start_date_raw, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_raw, "%Y-%m-%d").date()
        except ValueError:
            start_date = today.replace(day=1)
            end_date = today
            filter_type = "month"
    else:
        start_date = today.replace(day=1)
        end_date = today
        filter_type = "month"

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    start_dt, end_dt = get_day_bounds(start_date), get_day_bounds(end_date)

    return {
        "range": filter_type,
        "start_date": start_date,
        "end_date": end_date,
        "start_date_value": start_date.strftime("%Y-%m-%d"),
        "end_date_value": end_date.strftime("%Y-%m-%d"),
        "start_datetime": start_dt[0],
        "end_datetime": end_dt[1],
    }


def build_snapshot_summary(
    total_spending,
    breakdown,
    transaction_count,
    start_date,
    end_date,
    chart_base_amount,
    chart_basis_label,
):
    if not breakdown or transaction_count == 0:
        return {
            "headline": "Quiet window.",
            "summary": "No spend has landed in this range yet, so the snapshot is waiting for its first signal.",
            "details": [],
        }

    top_category = breakdown[0]
    share_of_spend = (top_category["total"] / total_spending * 100) if total_spending else 0
    share_of_base = (top_category["total"] / chart_base_amount * 100) if chart_base_amount else 0
    day_count = max((end_date - start_date).days + 1, 1)
    average_expense = total_spending / transaction_count if transaction_count else 0
    daily_average = total_spending / day_count if day_count else total_spending
    runner_up = breakdown[1] if len(breakdown) > 1 else None

    if share_of_spend >= 60:
        headline = f"{top_category['category'].replace('_', ' ').title()} is driving this period."
    elif share_of_spend >= 35:
        headline = f"{top_category['category'].replace('_', ' ').title()} leads the mix."
    else:
        headline = "Spending is spread across categories."

    summary = (
        f"{transaction_count} transaction{'s' if transaction_count != 1 else ''} logged, "
        f"with {top_category['category'].replace('_', ' ')} contributing "
        f"₹{top_category['total']:.2f} ({share_of_spend:.0f}% of spend, {share_of_base:.0f}% of {chart_basis_label})."
    )

    details = [
        f"Average spend per entry: ₹{average_expense:.2f}.",
        f"Average spend per day across this range: ₹{daily_average:.2f}.",
        f"Snapshot basis: {chart_basis_label.title()} at ₹{chart_base_amount:.2f}.",
    ]

    if runner_up:
        details.append(
            f"Next biggest pull came from {runner_up['category'].replace('_', ' ')} at ₹{runner_up['total']:.2f}."
        )

    if share_of_spend >= 60:
        details.append("This window is highly concentrated in one category.")
    elif share_of_spend >= 35:
        details.append("One category is clearly ahead, but the rest still matter.")
    else:
        details.append("The mix is balanced, with spend spread across multiple buckets.")

    return {
        "headline": headline,
        "summary": summary,
        "details": details,
    }


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
    app.config["APP_ENV"] = APP_ENV
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL", f"sqlite:///spendsense_{APP_ENV}.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        ensure_schema()
        db.create_all()
        ensure_schema()

    @app.before_request
    def load_current_user():
        g.user = current_user()

    @app.context_processor
    def inject_auth_state():
        return {
            "current_user": g.get("user"),
            "is_authenticated": g.get("user") is not None,
        }

    @app.route("/manifest.webmanifest")
    def manifest():
        return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")

    @app.route("/service-worker.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/login")
    def login():
        if g.user is not None:
            return redirect(url_for("home"))
        return render_template(
            "login.html",
            next_url=build_auth_redirect_target(request.args.get("next"), "home"),
            google_login_ready=bool(
                (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
                and (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
            ),
        )

    @app.route("/signup", methods=["POST"])
    def signup():
        if g.user is not None:
            return redirect(url_for("home"))

        next_url = build_auth_redirect_target(request.form.get("next_url"), "home")

        try:
            user = create_or_update_email_user(
                request.form.get("name", ""),
                request.form.get("email", ""),
                request.form.get("password", ""),
            )
            db.session.commit()
            session["user_id"] = user.id
            flash("Your account is ready. You're signed in.", "success")
            return redirect(next_url)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            flash("Couldn't create the account right now.", "danger")

        return redirect(url_for("login", next=next_url))

    @app.route("/signin", methods=["POST"])
    def signin():
        if g.user is not None:
            return redirect(url_for("home"))

        next_url = build_auth_redirect_target(request.form.get("next_url"), "home")

        try:
            user = authenticate_email_user(
                request.form.get("email", ""),
                request.form.get("password", ""),
            )
            db.session.commit()
            session["user_id"] = user.id
            flash(f"Welcome back, {user.name}.", "success")
            return redirect(next_url)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            flash("Couldn't sign you in right now.", "danger")

        return redirect(url_for("login", next=next_url))

    @app.route("/auth/google")
    def google_login():
        if g.user is not None:
            return redirect(url_for("home"))

        client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
        if not client_id:
            flash("Google login is not configured yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "danger")
            return redirect(url_for("login"))

        state = secrets.token_urlsafe(24)
        session["google_oauth_state"] = state
        session["post_login_redirect"] = build_auth_redirect_target(request.args.get("next"), "home")

        params = {
            "client_id": client_id,
            "redirect_uri": get_oauth_redirect_uri(),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        }
        return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

    @app.route("/auth/google/callback")
    def google_callback():
        if request.args.get("state") != session.get("google_oauth_state"):
            flash("Google login couldn't be verified. Please try again.", "danger")
            return redirect(url_for("login"))

        if request.args.get("error"):
            flash("Google login was cancelled.", "warning")
            return redirect(url_for("login"))

        code = (request.args.get("code") or "").strip()
        if not code:
            flash("Google login did not return an authorization code.", "danger")
            return redirect(url_for("login"))

        client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
        if not client_id or not client_secret:
            flash("Google login is not configured yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "danger")
            return redirect(url_for("login"))

        try:
            token_response = post_form_json(
                GOOGLE_TOKEN_URL,
                {
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": get_oauth_redirect_uri(),
                    "grant_type": "authorization_code",
                },
            )
            user_info = get_json(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {token_response['access_token']}"},
            )
            user = upsert_google_user(user_info)
            db.session.commit()
            session["user_id"] = user.id
            flash(f"Welcome back, {user.name}.", "success")
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("login"))
        except (HTTPError, URLError, KeyError):
            db.session.rollback()
            flash("Google login failed while talking to Google. Please check your OAuth settings.", "danger")
            return redirect(url_for("login"))
        finally:
            session.pop("google_oauth_state", None)

        return redirect(session.pop("post_login_redirect", url_for("home")))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("login"))

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def home():
        category_options = get_category_options()
        selected_date = parse_date_value(
            request.values.get("selected_date"),
            date.today(),
        )

        if request.method == "POST":
            user_input = request.form.get("transaction_input", "")
            transaction_type = (request.form.get("transaction_type") or "debit").strip().lower()
            if transaction_type not in {"debit", "credit"}:
                transaction_type = "debit"
            selected_category = normalize_rule_name(request.form.get("category", ""))

            try:
                parsed = parse_expense_input(
                    user_input,
                    extra_rules=build_category_rules(),
                    transaction_type=transaction_type,
                )
                if selected_category and selected_category != "auto":
                    parsed["category"] = selected_category
                    parsed["category_source"] = "manual"
                else:
                    parsed["category_source"] = "auto"

                now = datetime.now()
                parsed["created_at"] = datetime.combine(selected_date, now.time())
                parsed["transaction_type"] = transaction_type
                parsed["user_id"] = g.user.id
                transaction = Transaction(**parsed)
                db.session.add(transaction)
                db.session.commit()
                flash(
                    f"{transaction.category.replace('_', ' ').title()} transaction saved for {selected_date.strftime('%d %b')}.",
                    "success",
                )
            except ValueError as exc:
                flash(str(exc), "danger")
            except Exception:
                db.session.rollback()
                flash("Something went wrong while saving the transaction.", "danger")

            return redirect(url_for("home", selected_date=selected_date.strftime("%Y-%m-%d")))

        recent_transactions = (
            Transaction.query.filter(
                Transaction.user_id == g.user.id,
                Transaction.created_at >= get_day_bounds(selected_date)[0],
                Transaction.created_at <= get_day_bounds(selected_date)[1],
            )
            .order_by(Transaction.created_at.desc())
            .limit(10)
            .all()
        )
        return render_template(
            "home.html",
            transactions=recent_transactions,
            category_options=category_options,
            selected_date=selected_date,
            selected_date_value=selected_date.strftime("%Y-%m-%d"),
            today_label=selected_date.strftime("%a, %d %b %Y"),
            is_today=selected_date == date.today(),
            home_return_url=url_for("home", selected_date=selected_date.strftime("%Y-%m-%d")),
            transaction_type_options={"debit": "Expense", "credit": "Credit"},
        )

    @app.route("/transactions/<int:transaction_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_transaction(transaction_id):
        transaction = Transaction.query.filter_by(id=transaction_id, user_id=g.user.id).first_or_404()
        next_url = request.values.get("next_url", "")

        if request.method == "POST":
            user_input = request.form.get("transaction_input", "")
            transaction_type = (request.form.get("transaction_type") or "debit").strip().lower()
            if transaction_type not in {"debit", "credit"}:
                transaction_type = transaction.transaction_type or "debit"
            selected_category = normalize_rule_name(request.form.get("category", ""))
            selected_date = parse_date_value(
                request.form.get("transaction_date"),
                transaction.created_at.date(),
            )

            try:
                parsed = parse_expense_input(
                    user_input,
                    extra_rules=build_category_rules(),
                    transaction_type=transaction_type,
                )
                if selected_category and selected_category != "auto":
                    parsed["category"] = selected_category
                    parsed["category_source"] = "manual"
                else:
                    parsed["category_source"] = "auto"

                transaction.amount = parsed["amount"]
                transaction.note = parsed["note"]
                transaction.category = parsed["category"]
                transaction.category_source = parsed["category_source"]
                transaction.transaction_type = transaction_type
                transaction.created_at = datetime.combine(selected_date, transaction.created_at.time())
                db.session.commit()
                flash("Transaction updated.", "success")
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
            except Exception:
                db.session.rollback()
                flash("Couldn't update the transaction right now.", "danger")

            return redirect(build_redirect_target(next_url, "home"))

        return render_template(
            "edit_transaction.html",
            transaction=transaction,
            category_options=get_category_options(),
            next_url=build_redirect_target(next_url, "home"),
            transaction_type_options={"debit": "Expense", "credit": "Credit"},
        )

    @app.route("/transactions/<int:transaction_id>/delete", methods=["POST"])
    @login_required
    def delete_transaction(transaction_id):
        transaction = Transaction.query.filter_by(id=transaction_id, user_id=g.user.id).first_or_404()
        next_url = request.form.get("next_url", "")

        try:
            db.session.delete(transaction)
            db.session.commit()
            flash("Transaction deleted.", "success")
        except Exception:
            db.session.rollback()
            flash("Couldn't delete the entry right now.", "danger")

        return redirect(build_redirect_target(next_url, "home"))

    @app.route("/categories", methods=["POST"])
    @login_required
    def add_category():
        name = request.form.get("name", "")
        keywords_input = request.form.get("keywords", "")

        normalized_name = normalize_rule_name(name)
        keywords = normalize_keywords(keywords_input.split(","))

        if not name.strip():
            flash("Category name is required.", "danger")
            return redirect(url_for("dashboard"))

        if not keywords:
            flash("Add at least one keyword.", "danger")
            return redirect(url_for("dashboard"))

        try:
            existing_rule = CategoryRule.query.filter(
                func.lower(CategoryRule.name) == normalized_name
            ).first()

            if existing_rule:
                existing_keywords = normalize_keywords(existing_rule.keywords.split(","))
                merged_keywords = list(dict.fromkeys(existing_keywords + keywords))
                existing_rule.keywords = ", ".join(merged_keywords)
                success_message = "Category keywords updated."
            else:
                db.session.add(
                    CategoryRule(name=normalized_name, keywords=", ".join(keywords))
                )
                success_message = "Custom category added."

            db.session.flush()
            updated_count = recategorize_transactions(user_id=g.user.id)
            db.session.commit()
            flash(f"{success_message} Reclassified {updated_count} existing transaction(s).", "success")
        except Exception:
            db.session.rollback()
            flash("Couldn't save the category right now.", "danger")

        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        raw_selected_category = (request.args.get("category", "") or "").strip()
        selected_category = normalize_rule_name(raw_selected_category) if raw_selected_category else ""
        dashboard_view = (request.args.get("view") or "category").strip().lower()
        if dashboard_view not in {"category", "all"}:
            dashboard_view = "category"
        filters = build_dashboard_filters(request.args)
        filtered_transactions = Transaction.query.filter(
            Transaction.user_id == g.user.id,
            Transaction.created_at >= filters["start_datetime"],
            Transaction.created_at <= filters["end_datetime"],
        )

        total_spending = (
            filtered_transactions.filter_by(transaction_type="debit")
            .with_entities(func.coalesce(func.sum(Transaction.amount), 0))
            .scalar()
        )
        total_credits = (
            filtered_transactions.filter_by(transaction_type="credit")
            .with_entities(func.coalesce(func.sum(Transaction.amount), 0))
            .scalar()
        )
        current_balance = float(total_credits or 0) - float(total_spending or 0)
        transaction_count = filtered_transactions.with_entities(func.count(Transaction.id)).scalar() or 0
        category_rows = (
            filtered_transactions.filter_by(transaction_type="debit").with_entities(
                Transaction.category,
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count"),
            )
            .group_by(Transaction.category)
            .order_by(func.sum(Transaction.amount).desc())
            .all()
        )

        breakdown = [
            {
                "category": row.category,
                "total": float(row.total),
                "count": row.count,
                "share": (float(row.total) / float(total_spending or 1) * 100) if total_spending else 0,
            }
            for row in category_rows
        ]
        chart_data = OrderedDict((item["category"].title(), item["total"]) for item in breakdown)
        chart_base_amount = float(total_credits or 0) if float(total_credits or 0) > 0 else float(total_spending or 0)
        chart_basis_label = "money in" if float(total_credits or 0) > 0 else "total spend"
        for item in breakdown:
            item["chart_percent"] = (item["total"] / chart_base_amount * 100) if chart_base_amount else 0
        chart_share_data = OrderedDict(
            (item["category"].title(), round(item["chart_percent"], 1)) for item in breakdown
        )
        chart_axis_max = max(
            10,
            math.ceil((max(chart_share_data.values(), default=0) or 0) / 10) * 10,
        )
        active_rules = build_category_rules()
        snapshot_summary = build_snapshot_summary(
            float(total_spending or 0),
            breakdown,
            transaction_count,
            filters["start_date"],
            filters["end_date"],
            chart_base_amount or 0,
            chart_basis_label,
        )

        if selected_category:
            category_transactions = (
                filtered_transactions.filter_by(category=selected_category)
                .order_by(Transaction.created_at.desc())
                .limit(50)
                .all()
            )
        else:
            category_transactions = []

        all_logs = (
            filtered_transactions.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(50).all()
        )

        return render_template(
            "dashboard.html",
            total_spending=float(total_spending or 0),
            total_credits=float(total_credits or 0),
            current_balance=current_balance,
            breakdown=breakdown,
            chart_labels=list(chart_data.keys()),
            chart_values=list(chart_data.values()),
            chart_share_values=list(chart_share_data.values()),
            chart_axis_max=chart_axis_max,
            chart_basis_label=chart_basis_label,
            category_rules=active_rules,
            category_options=list(active_rules.keys()),
            selected_category=selected_category,
            category_transactions=category_transactions,
            dashboard_view=dashboard_view,
            all_logs=all_logs,
            filters=filters,
            transaction_count=transaction_count,
            snapshot_summary=snapshot_summary,
            dashboard_return_url=url_for(
                "dashboard",
                range=filters["range"],
                start_date=filters["start_date_value"],
                end_date=filters["end_date_value"],
                view=dashboard_view,
                category=selected_category or None,
            ),
        )

    @app.route("/dashboard/rescan", methods=["POST"])
    @login_required
    def rescan_dashboard():
        try:
            updated_count = recategorize_transactions(user_id=g.user.id)
            db.session.commit()
            flash(f"Rescan complete. Reclassified {updated_count} transaction(s).", "success")
        except Exception:
            db.session.rollback()
            flash("Couldn't complete the rescan right now.", "danger")

        return redirect(url_for("dashboard"))

    @app.route("/dashboard/reset-categories", methods=["POST"])
    @login_required
    def reset_custom_categories():
        try:
            deleted_count = CategoryRule.query.delete()
            updated_count = recategorize_transactions(user_id=g.user.id)
            db.session.commit()
            flash(
                f"Reset complete. Removed {deleted_count} custom categor"
                f"{'y' if deleted_count == 1 else 'ies'} and reclassified {updated_count} transaction(s).",
                "success",
            )
        except Exception:
            db.session.rollback()
            flash("Couldn't reset custom categories right now.", "danger")

        return redirect(url_for("dashboard"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG", False), port=5001)
