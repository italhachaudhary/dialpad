import os
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, session

# Optional Firebase Admin Integration
try:
    import firebase_admin
    from firebase_admin import credentials, firestore, auth as firebase_auth
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dialpad-vercel-secret-key-2026")

# Initialize Firebase Firestore & Auth
db = None
if FIREBASE_AVAILABLE:
    # 1. Check if Vercel Environment Variable FIREBASE_SERVICE_ACCOUNT is set
    firebase_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if firebase_env:
        try:
            cred_dict = json.loads(firebase_env)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("✅ Connected to Firebase Firestore via Vercel Environment Variable")
        except Exception as e:
            print(f"❌ Failed to parse FIREBASE_SERVICE_ACCOUNT env var: {e}")
    # 2. Fallback to local serviceAccountKey.json file
    elif os.path.exists("serviceAccountKey.json"):
        try:
            cred = credentials.Certificate("serviceAccountKey.json")
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            print("✅ Connected to Firebase Firestore via local serviceAccountKey.json")
        except Exception as e:
            print(f"❌ Failed to load local serviceAccountKey.json: {e}")

if not db:
    print("ℹ️ Running with local JSON database fallback (bookings_fallback.json)")

FALLBACK_FILE = "bookings_fallback.json"
ET_TZ = ZoneInfo("America/New_York")
ALLOWED_DIALPADS = ["Hamzy Dialpad", "Shimra Dialpad"]


# --- Database Helpers ---
def get_all_bookings_from_db():
    if db:
        docs = db.collection("bookings").stream()
        return [doc.to_dict() for doc in docs]
    else:
        if not os.path.exists(FALLBACK_FILE):
            return []
        with open(FALLBACK_FILE, "r") as f:
            return json.load(f)


def save_booking_to_db(booking_data):
    if db:
        db.collection("bookings").document(booking_data["id"]).set(booking_data)
    else:
        bookings = get_all_bookings_from_db()
        bookings = [b for b in bookings if b["id"] != booking_data["id"]]
        bookings.append(booking_data)
        with open(FALLBACK_FILE, "w") as f:
            json.dump(bookings, f, indent=2)


def delete_booking_from_db(booking_id):
    if db:
        db.collection("bookings").document(booking_id).delete()
    else:
        bookings = get_all_bookings_from_db()
        bookings = [b for b in bookings if b["id"] != booking_id]
        with open(FALLBACK_FILE, "w") as f:
            json.dump(bookings, f, indent=2)


# --- Timezone & Formatting ---
def parse_et_to_utc(date_str, time_str):
    naive_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    aware_et = naive_dt.replace(tzinfo=ET_TZ)
    return aware_et.astimezone(timezone.utc)


def enrich_booking_et(booking):
    start_utc = datetime.fromisoformat(booking["start_utc"])
    end_utc = datetime.fromisoformat(booking["end_utc"])

    start_et = start_utc.astimezone(ET_TZ)
    end_et = end_utc.astimezone(ET_TZ)
    tz_abbr = start_et.strftime("%Z")

    return {
        **booking,
        "et_date": start_et.strftime("%Y-%m-%d"),
        "et_display_date": start_et.strftime("%A, %b %d, %Y"),
        "et_time_range": f"{start_et.strftime('%I:%M %p')} – {end_et.strftime('%I:%M %p')} {tz_abbr}",
        "et_start_24": start_et.strftime("%H:%M"),
        "et_end_24": end_et.strftime("%H:%M"),
    }


# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    user = session.get("user")
    today_et = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    return jsonify({
        "authenticated": bool(user),
        "user": user,
        "today_et": today_et
    })


@app.route("/api/auth/google", methods=["POST"])
def google_login():
    data = request.json or {}
    id_token = data.get("id_token")

    # Verify cryptographic ID token if Firebase Admin Auth is initialized
    if id_token and db and FIREBASE_AVAILABLE:
        try:
            decoded_token = firebase_auth.verify_id_token(id_token)
            email = decoded_token.get("email", "").strip().lower()
            name = decoded_token.get("name") or email.split("@")[0].title()
            picture = decoded_token.get("picture", "")
            user_data = {"email": email, "name": name, "picture": picture}
            session["user"] = user_data
            return jsonify({"message": "Signed in with Google", "user": user_data})
        except Exception as e:
            print(f"Token verification warning: {e}")

    # Fallback payload from client Google login
    email = data.get("email", "").strip().lower()
    name = data.get("name", "").strip()
    picture = data.get("picture", "")

    if not email or not name:
        return jsonify({"error": "Google account name and email are required."}), 400

    user_data = {"email": email, "name": name, "picture": picture}
    session["user"] = user_data
    return jsonify({"message": "Signed in with Google successfully", "user": user_data})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"message": "Logged out successfully"})


@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    date_filter = request.args.get("date", "").strip()
    dialpad_filter = request.args.get("dialpad", "ALL").strip()

    raw_bookings = get_all_bookings_from_db()
    raw_bookings.sort(key=lambda b: b["start_utc"])
    enriched = [enrich_booking_et(b) for b in raw_bookings]

    if date_filter:
        enriched = [b for b in enriched if b["et_date"] == date_filter]

    if dialpad_filter and dialpad_filter != "ALL":
        enriched = [b for b in enriched if b["dialpad"] == dialpad_filter]

    return jsonify(enriched)


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    user = session.get("user")
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    data = request.json
    member_name = data.get("name", user["name"]).strip()
    dialpad = data.get("dialpad", "").strip()
    date_str = data.get("date", "").strip()
    start_time_str = data.get("start_time", "").strip()
    end_time_str = data.get("end_time", "").strip()

    if not all([member_name, dialpad, date_str, start_time_str, end_time_str]):
        return jsonify({"error": "All fields are required."}), 400

    if dialpad not in ALLOWED_DIALPADS:
        return jsonify({"error": "Please select either Hamzy Dialpad or Shimra Dialpad."}), 400

    start_utc = parse_et_to_utc(date_str, start_time_str)
    end_utc = parse_et_to_utc(date_str, end_time_str)

    if end_utc <= start_utc:
        return jsonify({"error": "End time must be after start time."}), 400

    # Clash Check
    existing_bookings = get_all_bookings_from_db()
    for b in existing_bookings:
        if b["dialpad"] == dialpad:
            b_start = datetime.fromisoformat(b["start_utc"])
            b_end = datetime.fromisoformat(b["end_utc"])
            if start_utc < b_end and end_utc > b_start:
                clash_info = enrich_booking_et(b)
                return jsonify({
                    "error": (
                        f"Schedule Clash: {dialpad} is already reserved by {b['name']} "
                        f"on {clash_info['et_display_date']} from {clash_info['et_time_range']}."
                    )
                }), 409

    new_booking = {
        "id": str(uuid.uuid4()),
        "name": member_name,
        "owner_email": user["email"],
        "dialpad": dialpad,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
    }

    save_booking_to_db(new_booking)
    return jsonify(enrich_booking_et(new_booking)), 201


@app.route("/api/bookings/<booking_id>", methods=["PUT"])
def update_booking(booking_id):
    user = session.get("user")
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    existing_bookings = get_all_bookings_from_db()
    target = next((b for b in existing_bookings if b["id"] == booking_id), None)

    if not target:
        return jsonify({"error": "Booking not found."}), 404

    if target["owner_email"] != user["email"]:
        return jsonify({"error": "Permission denied: You can only edit your own bookings."}), 403

    data = request.json
    member_name = data.get("name", target["name"]).strip()
    dialpad = data.get("dialpad", target["dialpad"]).strip()
    date_str = data.get("date", "").strip()
    start_time_str = data.get("start_time", "").strip()
    end_time_str = data.get("end_time", "").strip()

    if dialpad not in ALLOWED_DIALPADS:
        return jsonify({"error": "Please select either Hamzy Dialpad or Shimra Dialpad."}), 400

    start_utc = parse_et_to_utc(date_str, start_time_str)
    end_utc = parse_et_to_utc(date_str, end_time_str)

    if end_utc <= start_utc:
        return jsonify({"error": "End time must be after start time."}), 400

    for b in existing_bookings:
        if b["id"] != booking_id and b["dialpad"] == dialpad:
            b_start = datetime.fromisoformat(b["start_utc"])
            b_end = datetime.fromisoformat(b["end_utc"])
            if start_utc < b_end and end_utc > b_start:
                clash_info = enrich_booking_et(b)
                return jsonify({
                    "error": (
                        f"Schedule Clash: {dialpad} is already reserved by {b['name']} "
                        f"on {clash_info['et_display_date']} from {clash_info['et_time_range']}."
                    )
                }), 409

    updated_booking = {
        "id": booking_id,
        "name": member_name,
        "owner_email": user["email"],
        "dialpad": dialpad,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
    }

    save_booking_to_db(updated_booking)
    return jsonify(enrich_booking_et(updated_booking))


@app.route("/api/bookings/<booking_id>", methods=["DELETE"])
def delete_booking(booking_id):
    user = session.get("user")
    if not user:
        return jsonify({"error": "Authentication required."}), 401

    existing_bookings = get_all_bookings_from_db()
    target = next((b for b in existing_bookings if b["id"] == booking_id), None)

    if not target:
        return jsonify({"error": "Booking not found."}), 404

    if target["owner_email"] != user["email"]:
        return jsonify({"error": "Permission denied: You can only delete your own bookings."}), 403

    delete_booking_from_db(booking_id)
    return jsonify({"message": "Booking deleted successfully."})


if __name__ == "__main__":
    app.run(debug=True, port=5000)