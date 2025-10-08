# I_suck_at_golf — Telegram bot
# Background Worker friendly. No web server. Long-polling only.
# Requires: python-telegram-bot==21.3

import os, sys, traceback, platform, io, csv, uuid
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter
from datetime import datetime

from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

# ======= STARTUP / ENV =======
print("Starting I_suck_at_golf…", flush=True)
print(f"Python: {platform.python_version()}", flush=True)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    print("ERROR: BOT_TOKEN env var is missing. Set it in Render → Settings → Environment.", file=sys.stderr, flush=True)
    sys.exit(1)

BOT_NAME = "I_suck_at_golf"

# ======= CONSTANTS / LABELS =======
ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT = "⬆️", "⬇️", "➡️", "⬅️"
CHECK, CROSS = "✅", "❌"
BACK, CANCEL, CONFIRM = "⬅ Back", "✖ Cancel", "✅ Confirm"

# Lie & Clubs (as requested)
LIES = ["tee", "fairway", "rough", "deep rough", "fringe", "green", "sand", "mat", "bare lie", "divot"]
CLUBS = ["Dr", "3w", "5w", "7w", "3h", "3", "4", "5", "6", "7", "8", "9",
         "GW", "PW", "SW", "LW", "54", "56", "58", "60", "Putter"]

SHOT_TYPES = [
    "full swing", "3/4", "half swing",
    "pitch shot", "bunker shot", "chip shot",
    "bump and run", "flop shot", "putt"
]

# Non-putt steps
RESULT_NON_PUTT = [ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT, f"{CHECK}"]
CONTACT_NON_PUTT = ["thin", "fat", "toe", "heel", "shank", "high on face", "low on face", f"good {CHECK}"]
PLAN_CHOICES = [f"shot as planned {CHECK}", f"not as planned {CROSS}"]

# Putt steps
PUTT_DISTANCE = ["Long putt", "Short putt"]
RESULT_PUTT = [ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT, f"{CHECK}"]
CONTACT_PUTT = ["toe", "heel", f"good {CHECK}"]
LAG_PUTT = ["good reading", "poor reading"]

# ======= DATA MODELS =======
@dataclass
class Shot:
    timestamp: str
    mode: str            # "practice" or "oncourse"
    session_id: str
    hole: int | None = None

    # sticky for practice; explicit for oncourse
    lie: str | None = None
    club: str | None = None

    shot_type: str | None = None

    # non-putt path
    result: str | None = None
    contact: str | None = None
    plan: str | None = None

    # putt path
    putt_distance: str | None = None
    putt_result: str | None = None
    putt_contact: str | None = None
    putt_plan_1: str | None = None
    lag_reading: str | None = None
    putt_plan_2: str | None = None

    def as_row(self):
        return [
            self.timestamp, self.mode, self.session_id, self.hole,
            self.lie, self.club, self.shot_type,
            self.result, self.contact, self.plan,
            self.putt_distance, self.putt_result, self.putt_contact,
            self.putt_plan_1, self.lag_reading, self.putt_plan_2
        ]

RAW_HEADER = [
    "timestamp","mode","session_id","hole",
    "lie","club","shot_type",
    "result","contact","plan",
    "putt_distance","putt_result","putt_contact",
    "putt_plan_1","lag_reading","putt_plan_2"
]

# ======= HELPERS =======
def kb(rows): return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def now_iso(): return datetime.now().isoformat(timespec="seconds")

def pct(a, b): return 0.0 if not b else round(a * 100.0 / b, 1)

def ensure_session(ctx: ContextTypes.DEFAULT_TYPE):
    """Initialize per-user state buckets."""
    if "core" not in ctx.user_data:
        ctx.user_data["core"] = {}
    core = ctx.user_data["core"]
    core.setdefault("session_id", str(uuid.uuid4()))
    core.setdefault("mode", None)             # "practice" / "oncourse"
    core.setdefault("shots", [])              # list[Shot] for current session
    core.setdefault("current", None)          # building Shot
    core.setdefault("stack", [])              # back stack (snapshots)
    core.setdefault("practice", {"lie": None, "club": None})
    core.setdefault("round", {"hole": 1})
    return core

def start_new_shot(core):
    s = Shot(timestamp=now_iso(), mode=core["mode"], session_id=core["session_id"])
    if core["mode"] == "oncourse":
        s.hole = core["round"]["hole"]
    if core["mode"] == "practice":
        s.lie = core["practice"]["lie"]
        s.club = core["practice"]["club"]
    core["current"] = s
    core["stack"] = []

def push_state(core):
    snap = asdict(core["current"])
    core["stack"].append(snap)

def pop_state(core):
    if core["stack"]:
        prev = core["stack"].pop()
        core["current"] = Shot(**prev)
        return True
    return False

def summarize_shot(s: Shot) -> str:
    lines = [f"Mode: {s.mode}"]
    if s.hole: lines.append(f"Hole: {s.hole}")
    if s.lie: lines.append(f"Lie: {s.lie}")
    if s.club: lines.append(f"Club: {s.club}")
    if s.shot_type: lines.append(f"Type: {s.shot_type}")

    if s.shot_type == "putt":
        if s.putt_distance: lines.append(f"Distance: {s.putt_distance}")
        if s.putt_result:   lines.append(f"Result: {s.putt_result}")
        if s.putt_contact:  lines.append(f"Contact: {s.putt_contact}")
        if s.putt_plan_1:   lines.append(f"Plan #1: {s.putt_plan_1}")
        if s.lag_reading:   lines.append(f"Lag: {s.lag_reading}")
        if s.putt_plan_2:   lines.append(f"Plan #2: {s.putt_plan_2}")
    else:
        if s.result:  lines.append(f"Result: {s.result}")
        if s.contact: lines.append(f"Contact: {s.contact}")
        if s.plan:    lines.append(f"Plan: {s.plan}")

    return "\n".join(lines)

def club_basename(c: str | None) -> str:
    return c or "—"

# ======= KEYBOARDS =======
def kb_mode(): return kb([["practice", "on course"]])

def kb_lie():
    rows = [LIES[i:i+3] for i in range(0, len(LIES), 3)]
    rows += [[BACK]]
    return kb(rows)

def kb_club():
    rows = [CLUBS[i:i+5] for i in range(0, len(CLUBS), 5)]
    rows += [[BACK]]
    return kb(rows)

def kb_type():
    rows = [SHOT_TYPES[i:i+3] for i in range(0, len(SHOT_TYPES), 3)]
    rows += [[BACK]]
    return kb(rows)

def kb_result(is_putt: bool):
    src = RESULT_PUTT if is_putt else RESULT_NON_PUTT
    rows = [src[i:i+3] for i in range(0, len(src), 3)]
    rows += [[BACK]]
    return kb(rows)

def kb_contact(is_putt: bool):
    src = CONTACT_PUTT if is_putt else CONTACT_NON_PUTT
    rows = [src[i:i+3] for i in range(0, len(src), 3)]
    rows += [[BACK]]
    return kb(rows)

def kb_plan():
    rows = [PLAN_CHOICES, [BACK]]
    return kb(rows)

def kb_putt_distance():
    rows = [PUTT_DISTANCE, [BACK]]
    return kb(rows)

def kb_lag():
    rows = [LAG_PUTT, [BACK]]
    return kb(rows)

def kb_confirm():
    return kb([[CONFIRM, CANCEL], [BACK]])

# ======= HANDLERS =======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    await update.message.reply_text(
        f"Hi! This is {BOT_NAME}.\nChoose mode:",
        reply_markup=kb_mode()
    )

async def handle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    text = update.message.text
    if text not in ["practice", "on course"]:
        return await update.message.reply_text("Choose mode:", reply_markup=kb_mode())

    # new session id per mode selection
    core["mode"] = text
    core["session_id"] = str(uuid.uuid4())
    core["shots"] = []
    core["current"] = None
    core["stack"] = []

    if text == "practice":
        core["practice"] = {"lie": None, "club": None}
        return await update.message.reply_text("Practice mode selected.\nPick Lie:", reply_markup=kb_lie())
    else:
        core["round"] = {"hole": 1}
        return await update.message.reply_text(
            "On-course mode selected.\nHole = 1.\nStart a shot with /shot\nUse /next_hole to advance hole.",
            reply_markup=ReplyKeyboardRemove()
        )

# ---- Practice setup (sticky Lie & Club) ----
async def handle_practice_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "practice": return
    text = update.message.text

    # LIE
    if core["practice"]["lie"] is None:
        if text == BACK:
            return await update.message.reply_text("Choose mode:", reply_markup=kb_mode())
        if text in LIES:
            core["practice"]["lie"] = text
            return await update.message.reply_text(f"Lie: {text}\nNow pick Club:", reply_markup=kb_club())
        return await update.message.reply_text("Pick Lie:", reply_markup=kb_lie())

    # CLUB
    if core["practice"]["club"] is None:
        if text == BACK:
            core["practice"]["lie"] = None
            return await update.message.reply_text("Pick Lie:", reply_markup=kb_lie())
        if text in CLUBS:
            core["practice"]["club"] = text
            start_new_shot(core)  # prefill with sticky values
            return await update.message.reply_text(
                f"Sticky set ✅\nLie: {core['practice']['lie']} | Club: {core['practice']['club']}\nStart a shot: choose Type",
                reply_markup=kb_type()
            )
        return await update.message.reply_text("Pick Club:", reply_markup=kb_club())

    # both set → enter shot flow
    await shot_flow(update, context)

# ---- On-course helpers ----
async def cmd_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "on course":
        return await update.message.reply_text("You are not in on-course mode. Use /start.")
    start_new_shot(core)
    await update.message.reply_text(f"Hole {core['round']['hole']}: choose Type", reply_markup=kb_type())

async def cmd_next_hole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "on course":
        return await update.message.reply_text("You are not in on-course mode.")
    core["round"]["hole"] += 1
    await update.message.reply_text(f"Moved to hole {core['round']['hole']}. Add a shot: /shot")

# ---- Stats / Session control ----
def compute_stats_by_club(shots: list[Shot]):
    by_club = defaultdict(list)
    for s in shots:
        by_club[club_basename(s.club)].append(s)

    result_keys = list(dict.fromkeys(RESULT_NON_PUTT + RESULT_PUTT))
    contact_keys = list(dict.fromkeys([*CONTACT_NON_PUTT, *CONTACT_PUTT]))
    plan_keys = PLAN_CHOICES
    lag_keys = LAG_PUTT

    rows = []
    header = ["Club", "n"] \
        + [f"Result % {k}" for k in result_keys] \
        + [f"Contact % {k}" for k in contact_keys] \
        + [f"Plan % {k}" for k in plan_keys] \
        + [f"Lag % {k}" for k in lag_keys]
    rows.append(header)

    for club, lst in by_club.items():
        N = len(lst)
        res, con, plan, lag = [], [], [], []
        for s in lst:
            if s.shot_type == "putt":
                if s.putt_result:  res.append(s.putt_result)
                if s.putt_contact: con.append(s.putt_contact)
                if s.putt_plan_1:  plan.append(s.putt_plan_1)
                if s.putt_plan_2:  plan.append(s.putt_plan_2)
                if s.lag_reading:  lag.append(s.lag_reading)
            else:
                if s.result:  res.append(s.result)
                if s.contact: con.append(s.contact)
                if s.plan:    plan.append(s.plan)

        rc, cc, pc, lc = Counter(res), Counter(con), Counter(plan), Counter(lag)

        row = [club, N] \
            + [pct(rc.get(k, 0), N) for k in result_keys] \
            + [pct(cc.get(k, 0), N) for k in contact_keys] \
            + [pct(pc.get(k, 0), N) for k in plan_keys] \
            + [pct(lc.get(k, 0), N) for k in lag_keys]
        rows.append(row)
    return rows

def csv_bytes_from_rows(rows: list[list]):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for r in rows: writer.writerow(r)
    return io.BytesIO(buf.getvalue().encode("utf-8"))

def raw_csv_bytes(shots: list[Shot]):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(RAW_HEADER)
    for s in shots: w.writerow(s.as_row())
    return io.BytesIO(buf.getvalue().encode("utf-8"))

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if not core["shots"]:
        return await update.message.reply_text("No shots yet in this session.")
    rows = compute_stats_by_club(core["shots"])
    stats_file = csv_bytes_from_rows(rows); stats_file.name = "stats_by_club.csv"
    raw_file = raw_csv_bytes(core["shots"]); raw_file.name = "raw_shots.csv"

    await update.message.reply_text(
        "Statistics are percentages per club within the current session.\n"
        "Sending two CSVs for Google Sheets:"
    )
    await update.message.reply_document(InputFile(stats_file))
    await update.message.reply_document(InputFile(raw_file))

async def cmd_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Optional: вручную завершить текущую сессию и начать новую."""
    core = ensure_session(context)
    core["session_id"] = str(uuid.uuid4())
    core["shots"] = []
    core["current"] = None
    core["stack"] = []
    if core["mode"] == "practice":
        core["practice"] = {"lie": None, "club": None}
        return await update.message.reply_text("Session reset. Practice setup: pick Lie.", reply_markup=kb_lie())
    else:
        core["round"] = {"hole": 1}
        return await update.message.reply_text("Session reset. On-course: Hole = 1. Use /shot.")

# ---- Common shot flow (both modes) ----
async def shot_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    s: Shot | None = core["current"]

    # Prepare for practice if sticky set already
    if s is None:
        if core["mode"] == "practice":
            if core["practice"]["lie"] and core["practice"]["club"]:
                start_new_shot(core)
                s = core["current"]
            else:
                # still in practice setup
                return
        else:
            return await update.message.reply_text("Start a shot with /shot")

    text = update.message.text

    # Back
    if text == BACK:
        if pop_state(core):
            return await reask_step(update, core["current"])
        return await update.message.reply_text("Nothing to go back to.")

    # Cancel
    if text == CANCEL:
        core["current"] = None
        core["stack"] = []
        if core["mode"] == "practice":
            return await update.message.reply_text("Shot canceled.\nNew shot: choose Type", reply_markup=kb_type())
        return await update.message.reply_text("Shot canceled. Start new with /shot", reply_markup=ReplyKeyboardRemove())

    # Confirm
    if text == CONFIRM:
        core["shots"].append(core["current"])
        core["current"] = None
        core["stack"] = []
        if core["mode"] == "practice":
            start_new_shot(core)  # quick next
            return await update.message.reply_text("Saved ✅\nNew shot: choose Type", reply_markup=kb_type())
        return await update.message.reply_text("Saved ✅\nAdd next: /shot", reply_markup=ReplyKeyboardRemove())

    # --- Progress steps ---
    # Type
    if s.shot_type is None:
        if text in SHOT_TYPES:
            push_state(core)
            s.shot_type = text
            if s.shot_type == "putt":
                return await update.message.reply_text("Distance?", reply_markup=kb_putt_distance())
            # non-putt: ensure lie/club known
            if s.lie is None:
                return await update.message.reply_text("Lie?", reply_markup=kb_lie())
            if s.club is None:
                return await update.message.reply_text("Club?", reply_markup=kb_club())
            return await update.message.reply_text("Result?", reply_markup=kb_result(False))
        return await update.message.reply_text("Choose Type:", reply_markup=kb_type())

    # Non-putt branch
    if s.shot_type != "putt":
        if s.lie is None:
            if text in LIES:
                push_state(core); s.lie = text
                return await update.message.reply_text("Club?", reply_markup=kb_club())
            return await update.message.reply_text("Lie?", reply_markup=kb_lie())
        if s.club is None:
            if text in CLUBS:
                push_state(core); s.club = text
                return await update.message.reply_text("Result?", reply_markup=kb_result(False))
            return await update.message.reply_text("Club?", reply_markup=kb_club())
        if s.result is None:
            if text in RESULT_NON_PUTT:
                push_state(core); s.result = text
                return await update.message.reply_text("Contact?", reply_markup=kb_contact(False))
            return await update.message.reply_text("Result?", reply_markup=kb_result(False))
        if s.contact is None:
            if text in CONTACT_NON_PUTT:
                push_state(core); s.contact = text
                return await update.message.reply_text("Plan?", reply_markup=kb_plan())
            return await update.message.reply_text("Contact?", reply_markup=kb_contact(False))
        if s.plan is None:
            if text in PLAN_CHOICES:
                push_state(core); s.plan = text
                summary = summarize_shot(s)
                return await update.message.reply_text(f"Review:\n{summary}", reply_markup=kb_confirm())
            return await update.message.reply_text("Plan?", reply_markup=kb_plan())

    # Putt branch
    else:
        if s.putt_distance is None:
            if text in PUTT_DISTANCE:
                push_state(core); s.putt_distance = text
                if s.lie is None:
                    return await update.message.reply_text("Lie?", reply_markup=kb_lie())
                if s.club is None:
                    return await update.message.reply_text("Club?", reply_markup=kb_club())
                return await update.message.reply_text("Result?", reply_markup=kb_result(True))
            return await update.message.reply_text("Distance?", reply_markup=kb_putt_distance())
        if s.lie is None:
            if text in LIES:
                push_state(core); s.lie = text
                return await update.message.reply_text("Club?", reply_markup=kb_club())
            return await update.message.reply_text("Lie?", reply_markup=kb_lie())
        if s.club is None:
            if text in CLUBS:
                push_state(core); s.club = text
                return await update.message.reply_text("Result?", reply_markup=kb_result(True))
            return await update.message.reply_text("Club?", reply_markup=kb_club())
        if s.putt_result is None:
            if text in RESULT_PUTT:
                push_state(core); s.putt_result = text
                return await update.message.reply_text("Contact?", reply_markup=kb_contact(True))
            return await update.message.reply_text("Result?", reply_markup=kb_result(True))
        if s.putt_contact is None:
            if text in CONTACT_PUTT:
                push_state(core); s.putt_contact = text
                return await update.message.reply_text("Plan?", reply_markup=kb_plan())
            return await update.message.reply_text("Contact?", reply_markup=kb_contact(True))
        if s.putt_plan_1 is None:
            if text in PLAN_CHOICES:
                push_state(core); s.putt_plan_1 = text
                return await update.message.reply_text("Lag putt reading?", reply_markup=kb_lag())
            return await update.message.reply_text("Plan?", reply_markup=kb_plan())
        if s.lag_reading is None:
            if text in LAG_PUTT:
                push_state(core); s.lag_reading = text
                return await update.message.reply_text("Plan (after lag)?", reply_markup=kb_plan())
            return await update.message.reply_text("Lag putt reading?", reply_markup=kb_lag())
        if s.putt_plan_2 is None:
            if text in PLAN_CHOICES:
                push_state(core); s.putt_plan_2 = text
                summary = summarize_shot(s)
                return await update.message.reply_text(f"Review:\n{summary}", reply_markup=kb_confirm())
            return await update.message.reply_text("Plan (after lag)?", reply_markup=kb_plan())

async def reask_step(update: Update, s: Shot):
    if s.shot_type is None:
        return await update.message.reply_text("Choose Type:", reply_markup=kb_type())

    if s.shot_type != "putt":
        if s.lie is None:   return await update.message.reply_text("Lie?", reply_markup=kb_lie())
        if s.club is None:  return await update.message.reply_text("Club?", reply_markup=kb_club())
        if s.result is None:return await update.message.reply_text("Result?", reply_markup=kb_result(False))
        if s.contact is None:return await update.message.reply_text("Contact?", reply_markup=kb_contact(False))
        if s.plan is None: return await update.message.reply_text("Plan?", reply_markup=kb_plan())
        return await update.message.reply_text(f"Review:\n{summarize_shot(s)}", reply_markup=kb_confirm())
    else:
        if s.putt_distance is None: return await update.message.reply_text("Distance?", reply_markup=kb_putt_distance())
        if s.lie is None:           return await update.message.reply_text("Lie?", reply_markup=kb_lie())
        if s.club is None:          return await update.message.reply_text("Club?", reply_markup=kb_club())
        if s.putt_result is None:   return await update.message.reply_text("Result?", reply_markup=kb_result(True))
        if s.putt_contact is None:  return await update.message.reply_text("Contact?", reply_markup=kb_contact(True))
        if s.putt_plan_1 is None:   return await update.message.reply_text("Plan?", reply_markup=kb_plan())
        if s.lag_reading is None:   return await update.message.reply_text("Lag putt reading?", reply_markup=kb_lag())
        if s.putt_plan_2 is None:   return await update.message.reply_text("Plan (after lag)?", reply_markup=kb_plan())
        return await update.message.reply_text(f"Review:\n{summarize_shot(s)}", reply_markup=kb_confirm())

# ---- Router for plain text ----
async def any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] is None:
        return await handle_mode(update, context)

    if core["mode"] == "practice":
        if core["practice"]["lie"] is None or core["practice"]["club"] is None:
            return await handle_practice_setup(update, context)
        return await shot_flow(update, context)

    if core["mode"] == "on course":
        return await shot_flow(update, context)

# ======= MAIN =======
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("shot", cmd_shot))
    app.add_handler(CommandHandler("next_hole", cmd_next_hole))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("end_session", cmd_end_session))

    # any text (menus)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_text))

    try:
        print("Bot polling starting…", flush=True)
        app.run_polling()
    except Exception:
        print("FATAL: unhandled exception in run_polling()", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
