# I_suck_at_golf — Telegram bot (Background Worker, long-polling)
# Requirements: python-telegram-bot==21.3  (или 22.x)
# Render: Build -> pip install -r requirements.txt
#         Start -> python -u golf_bot.py
# Env var: BOT_TOKEN=<ваш токен от BotFather>

import os, sys, traceback, platform, io, csv, uuid
from dataclasses import dataclass, asdict
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

# ======= CONSTANTS =======
ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT = "⬆️", "⬇️", "➡️", "⬅️"

# ⛳️ — маркер «удачно / как планировал» (вместо ✅)
CHECK, CROSS = "⛳️", "❌"

# Кнопка подтверждения оставляем с ✅
BACK, CANCEL, CONFIRM = "⬅ Back", "✖ Cancel", "✅ Confirm"

# Новые универсальные кнопки
MAIN_MENU = "🏠 Main menu"
END_SESSION_BTN = "🛑 End session"

# Lie / Club
LIES = ["tee", "fairway", "rough", "deep rough", "fringe", "green", "sand", "mat", "bare lie", "divot"]
CLUBS = ["Dr", "3w", "5w", "7w", "3h", "3", "4", "5", "6", "7", "8", "9",
         "GW", "PW", "SW", "LW", "54", "56", "58", "60", "Putter"]

SHOT_TYPES = [
    "full swing", "3/4", "half swing",
    "pitch shot", "bunker shot", "chip shot",
    "bump and run", "flop shot", "putt"
]

# Non-putt
RESULT_NON_PUTT = [ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT, "⛳️"]
CONTACT_NON_PUTT = ["thin", "fat", "toe", "heel", "shank", "high on face", "low on face", "good ⛳️"]
PLAN_CHOICES = ["shot as planned ⛳️", "not as planned ❌"]

# Putt
PUTT_DISTANCE = ["Long putt", "Short putt"]
RESULT_PUTT = [ARW_UP, ARW_DOWN, ARW_RIGHT, ARW_LEFT, "⛳️"]
CONTACT_PUTT = ["toe", "heel", "good ⛳️"]
LAG_PUTT = ["good reading", "poor reading"]

# ======= DATA =======
@dataclass
class Shot:
    timestamp: str
    mode: str            # "practice" | "oncourse"
    session_id: str
    hole: int | None = None

    # sticky для practice; явные для oncourse
    lie: str | None = None
    club: str | None = None

    shot_type: str | None = None

    # non-putt
    result: str | None = None
    contact: str | None = None
    plan: str | None = None

    # putt
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
def club_name(c: str | None) -> str: return c or "—"

def ensure_session(ctx: ContextTypes.DEFAULT_TYPE):
    if "core" not in ctx.user_data:
        ctx.user_data["core"] = {}
    core = ctx.user_data["core"]
    core.setdefault("session_id", str(uuid.uuid4()))
    core.setdefault("mode", None)              # "practice" / "oncourse"
    core.setdefault("shots", [])               # list[Shot]
    core.setdefault("current", None)           # building Shot
    core.setdefault("stack", [])               # back snapshots
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
    core["stack"].append(asdict(core["current"]))

def pop_state(core):
    if core["stack"]:
        prev = core["stack"].pop()
        core["current"] = Shot(**prev)
        return True
    return False

def summarize(s: Shot) -> str:
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

# ======= GLOBAL CONTROLS =======
async def go_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сброс и возврат на выбор режима."""
    core = ensure_session(context)
    core["mode"] = None
    core["shots"] = []
    core["current"] = None
    core["stack"] = []
    core["practice"] = {"lie": None, "club": None}
    core["round"] = {"hole": 1}
    await update.message.reply_text(
        f"Hi! This is {BOT_NAME}.\nChoose mode:",
        reply_markup=kb_mode()
    )

async def end_session_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение сессии (и в practice, и в on course)."""
    core = ensure_session(context)
    core["session_id"] = str(uuid.uuid4())
    core["shots"] = []
    core["current"] = None
    core["stack"] = []
    if core["mode"] == "practice":
        core["practice"] = {"lie": None, "club": None}
        await update.message.reply_text("Session ended. Practice setup: pick Lie.", reply_markup=kb_lie())
    elif core["mode"] == "oncourse":
        core["round"] = {"hole": 1}
        await update.message.reply_text("Session ended. On-course: Hole = 1. Use /shot.", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("Session ended. Use /start to choose mode.", reply_markup=kb_mode())

async def handle_controls(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обработка управляющих кнопок. True если обработано."""
    if text == MAIN_MENU:
        await go_main_menu(update, context)
        return True
    if text == END_SESSION_BTN:
        await end_session_action(update, context)
        return True
    return False

# ======= KEYBOARDS =======
def kb_mode(): return kb([["practice", "on course"]])

def kb_with_controls(rows: list[list[str]]):
    # Добавляем общий ряд управления в любой экран шага
    rows = list(rows)
    rows += [[BACK, MAIN_MENU], [END_SESSION_BTN]]
    return kb(rows)

def kb_lie():
    rows = [LIES[i:i+3] for i in range(0, len(LIES), 3)]
    return kb_with_controls(rows)

def kb_club():
    rows = [CLUBS[i:i+5] for i in range(0, len(CLUBS), 5)]
    return kb_with_controls(rows)

def kb_type():
    rows = [SHOT_TYPES[i:i+3] for i in range(0, len(SHOT_TYPES), 3)]
    return kb_with_controls(rows)

def kb_result(is_putt=False):
    src = RESULT_PUTT if is_putt else RESULT_NON_PUTT
    rows = [src[i:i+3] for i in range(0, len(src), 3)]
    return kb_with_controls(rows)

def kb_contact(is_putt=False):
    src = CONTACT_PUTT if is_putt else CONTACT_NON_PUTT
    rows = [src[i:i+3] for i in range(0, len(src), 3)]
    return kb_with_controls(rows)

def kb_plan():
    rows = [PLAN_CHOICES]
    return kb_with_controls(rows)

def kb_putt_distance():
    rows = [PUTT_DISTANCE]
    return kb_with_controls(rows)

def kb_lag():
    rows = [LAG_PUTT]
    return kb_with_controls(rows)

def kb_confirm():
    # Confirm оставляем с ✅, но управление тоже доступно
    rows = [[CONFIRM, CANCEL]]
    return kb_with_controls(rows)

# ======= COMMANDS =======
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I_suck_at_golf — commands:\n"
        "/start — choose mode\n"
        "/shot — (on course) start a shot\n"
        "/next_hole — go to next hole\n"
        "/stats — CSV stats (percent per club, current session)\n"
        "/end_session — end current session\n"
        "/help — this help"
    )

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

    core["mode"] = text if text != "on course" else "oncourse"
    core["session_id"] = str(uuid.uuid4())
    core["shots"] = []
    core["current"] = None
    core["stack"] = []

    if core["mode"] == "practice":
        core["practice"] = {"lie": None, "club": None}
        return await update.message.reply_text("Practice selected.\nPick Lie:", reply_markup=kb_lie())
    else:
        core["round"] = {"hole": 1}
        return await update.message.reply_text(
            "On-course selected.\nHole = 1.\nStart a shot with /shot\nUse /next_hole to advance hole.",
            reply_markup=ReplyKeyboardRemove()
        )

# ---- Practice sticky setup ----
async def handle_practice_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "practice": return
    text = update.message.text

    # Управляющие кнопки
    if await handle_controls(text, update, context):
        return

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
            start_new_shot(core)  # prefill sticky
            return await update.message.reply_text(
                f"Sticky set ⛳️\nLie: {core['practice']['lie']} | Club: {core['practice']['club']}\nStart a shot: choose Type",
                reply_markup=kb_type()
            )
        return await update.message.reply_text("Pick Club:", reply_markup=kb_club())

    # обе заданы → входим в шаги удара
    await shot_flow(update, context)

# ---- On-course helpers ----
async def cmd_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "oncourse":
        return await update.message.reply_text("You are not in on-course mode. Use /start.")
    start_new_shot(core)
    await update.message.reply_text(f"Hole {core['round']['hole']}: choose Type", reply_markup=kb_type())

async def cmd_next_hole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    if core["mode"] != "oncourse":
        return await update.message.reply_text("You are not in on-course mode.")
    core["round"]["hole"] += 1
    await update.message.reply_text(f"Moved to hole {core['round']['hole']}. Add a shot: /shot")

# ---- Stats / CSV ----
def compute_stats_by_club(shots: list[Shot]):
    by_club = defaultdict(list)
    for s in shots:
        by_club[club_name(s.club)].append(s)

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
    raw_file = raw_csv_bytes(core["shots"]);  raw_file.name = "raw_shots.csv"

    await update.message.reply_text(
        "Statistics are percentages per club within the current session.\n"
        "Sending two CSVs for Google Sheets:"
    )
    await update.message.reply_document(InputFile(stats_file))
    await update.message.reply_document(InputFile(raw_file))

async def cmd_end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await end_session_action(update, context)

# ---- Common shot flow ----
async def shot_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    s: Shot | None = core["current"]

    # Подготовка для practice (быстрые подряд удары)
    if s is None:
        if core["mode"] == "practice":
            if core["practice"]["lie"] and core["practice"]["club"]:
                start_new_shot(core); s = core["current"]
            else:
                return  # ещё выбираем sticky lie/club
        elif core["mode"] == "oncourse":
            return await update.message.reply_text("Start a shot with /shot")
        else:
            return await update.message.reply_text("Use /start to choose mode.")

    text = update.message.text

    # Управляющие кнопки
    if await handle_controls(text, update, context):
        return

    # Назад
    if text == BACK:
        if pop_state(core):
            return await reask_step(update, core["current"])
        return await update.message.reply_text("Nothing to go back to.")

    # Отмена
    if text == CANCEL:
        core["current"] = None
        core["stack"] = []
        if core["mode"] == "practice":
            return await update.message.reply_text("Shot canceled.\nNew shot: choose Type", reply_markup=kb_type())
        return await update.message.reply_text("Shot canceled. Start new with /shot", reply_markup=ReplyKeyboardRemove())

    # Подтверждение
    if text == CONFIRM:
        core["shots"].append(core["current"])
        core["current"] = None
        core["stack"] = []
        if core["mode"] == "practice":
            start_new_shot(core)
            return await update.message.reply_text("Saved ✅\nNew shot: choose Type", reply_markup=kb_type())
        return await update.message.reply_text("Saved ✅\nAdd next: /shot", reply_markup=ReplyKeyboardRemove())

    # Progression
    # Type
    if s.shot_type is None:
        if text in SHOT_TYPES:
            push_state(core); s.shot_type = text
            if s.shot_type == "putt":
                return await update.message.reply_text("Distance?", reply_markup=kb_putt_distance())
            # non-putt: ensure lie/club
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
                return await update.message.reply_text(f"Review:\n{summarize(s)}", reply_markup=kb_confirm())
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
                return await update.message.reply_text(f"Review:\n{summarize(s)}", reply_markup=kb_confirm())
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
        return await update.message.reply_text(f"Review:\n{summarize(s)}", reply_markup=kb_confirm())
    else:
        if s.putt_distance is None: return await update.message.reply_text("Distance?", reply_markup=kb_putt_distance())
        if s.lie is None:           return await update.message.reply_text("Lie?", reply_markup=kb_lie())
        if s.club is None:          return await update.message.reply_text("Club?", reply_markup=kb_club())
        if s.putt_result is None:   return await update.message.reply_text("Result?", reply_markup=kb_result(True))
        if s.putt_contact is None:  return await update.message.reply_text("Contact?", reply_markup=kb_contact(True))
        if s.putt_plan_1 is None:   return await update.message.reply_text("Plan?", reply_markup=kb_plan())
        if s.lag_reading is None:   return await update.message.reply_text("Lag putt reading?", reply_markup=kb_lag())
        if s.putt_plan_2 is None:   return await update.message.reply_text("Plan (after lag)?", reply_markup=kb_plan())
        return await update.message.reply_text(f"Review:\n{summarize(s)}", reply_markup=kb_confirm())

# ---- Router ----
async def any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    core = ensure_session(context)
    text = update.message.text

    # Управляющие кнопки доступны всегда
    if await handle_controls(text, update, context):
        return

    if core["mode"] is None:
        return await handle_mode(update, context)

    if core["mode"] == "practice":
        if core["practice"]["lie"] is None or core["practice"]["club"] is None:
            return await handle_practice_setup(update, context)
        return await shot_flow(update, context)

    if core["mode"] == "oncourse":
        return await shot_flow(update, context)

    return await update.message.reply_text("Use /start to choose mode.")

# ======= MAIN =======
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("shot", cmd_shot))
    app.add_handler(CommandHandler("next_hole", cmd_next_hole))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("end_session", cmd_end_session))
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
