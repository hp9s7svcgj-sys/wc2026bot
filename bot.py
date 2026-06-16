import os
import json
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from database import Database

# ─── تنظیمات ───────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
IRAN_TZ = pytz.timezone("Asia/Tehran")

db = Database()

# ─── ضرایب مراحل ───────────────────────────────────────────
STAGE_MULTIPLIERS = {
    "group_1": 1, "group_2": 2, "group_3": 3,
    "r16": 4, "qf": 5, "sf": 6,  # نیمه‌نهایی و رده‌بندی
    "3rd": 6, "final": 10,
    "r8": 5,  # یک هشتم
}

STAGE_NAMES = {
    "group_1": "مرحله اول گروهی",
    "group_2": "مرحله دوم گروهی",
    "group_3": "مرحله سوم گروهی",
    "r16": "یک شانزدهم نهایی",
    "r8": "یک هشتم نهایی",
    "qf": "یک چهارم نهایی",
    "sf": "نیمه‌نهایی",
    "3rd": "رده‌بندی سوم",
    "final": "فینال",
}

# ─── محاسبه امتیاز ──────────────────────────────────────────
def calculate_score(prediction: dict, result: dict, stage: str) -> int:
    multiplier = STAGE_MULTIPLIERS.get(stage, 1)
    ph, pa = prediction["home"], prediction["away"]
    rh, ra = result["home"], result["away"]

    is_knockout = stage in ("r16", "r8", "qf", "sf", "3rd", "final")

    # تعیین برنده پیش‌بینی و نتیجه واقعی
    def winner(h, a):
        if h > a: return "home"
        if a > h: return "away"
        return "draw"

    pred_winner = winner(ph, pa)
    real_winner = winner(rh, ra)

    score = 0

    # نتیجه دقیق
    if ph == rh and pa == ra:
        score = 20 * multiplier

    # تیم درست + تفاضل درست
    elif pred_winner == real_winner and pred_winner != "draw":
        if (ph - pa) == (rh - ra):
            score = 15 * multiplier
        else:
            score = 10 * multiplier

    # تساوی با تفاضل مختلف (تبصره ۱)
    elif pred_winner == "draw" and real_winner == "draw":
        score = 10 * multiplier

    # فقط تیم برنده درست
    elif pred_winner == real_winner and pred_winner != "draw":
        score = 10 * multiplier

    # امتیاز پیش‌بینی ادامه بازی در مراحل حذفی (تبصره ۲)
    if is_knockout and pred_winner == "draw" and score > 0:
        extra_pred = prediction.get("extra_time")
        extra_real = result.get("extra_time")
        if extra_pred and extra_real and extra_pred == extra_real:
            score += 5 * multiplier

    return score

# ─── دریافت بازی‌های امروز از API ───────────────────────────
async def fetch_today_matches():
    today = datetime.now(IRAN_TZ).strftime("%Y-%m-%d")
    url = f"https://api.football-data.org/v4/competitions/WC/matches?dateFrom={today}&dateTo={today}"
    headers = {"X-Auth-Token": os.getenv("FOOTBALL_API_KEY", "")}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("matches", [])
    except Exception as e:
        print(f"API Error: {e}")
    return []

def format_match_time(utc_time_str: str) -> str:
    try:
        utc_dt = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        iran_dt = utc_dt.astimezone(IRAN_TZ)
        return iran_dt.strftime("%H:%M")
    except:
        return "؟؟:؟؟"

# ─── ساخت کیبورد پیش‌بینی ────────────────────────────────────
def build_score_keyboard(match_id: int, home: str, away: str, step: str = "home_score"):
    """
    مرحله اول: انتخاب گل تیم میزبان
    مرحله دوم: انتخاب گل تیم مهمان
    """
    buttons = []
    row = []
    for i in range(10):
        row.append(InlineKeyboardButton(
            str(i),
            callback_data=f"pred|{match_id}|{step}|{i}"
        ))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    label = f"⚽ گل‌های {home}" if step == "home_score" else f"⚽ گل‌های {away}"
    buttons.insert(0, [InlineKeyboardButton(label, callback_data="noop")])

    return InlineKeyboardMarkup(buttons)

def build_extra_time_keyboard(match_id: int):
    """کیبورد پیش‌بینی ادامه در وقت اضافه/پنالتی"""
    buttons = [
        [InlineKeyboardButton("⏱ وقت اضافه - تیم میزبان پیروز", callback_data=f"extra|{match_id}|home_et")],
        [InlineKeyboardButton("⏱ وقت اضافه - تیم مهمان پیروز", callback_data=f"extra|{match_id}|away_et")],
        [InlineKeyboardButton("🥅 پنالتی - تیم میزبان پیروز", callback_data=f"extra|{match_id}|home_pen")],
        [InlineKeyboardButton("🥅 پنالتی - تیم مهمان پیروز", callback_data=f"extra|{match_id}|away_pen")],
    ]
    return InlineKeyboardMarkup(buttons)

# ─── دستور /start ──────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.first_name, user.username)
    await update.message.reply_text(
        f"سلام {user.first_name}! 👋⚽\n\n"
        "🏆 به بات پیش‌بینی جام جهانی ۲۰۲۶ خوش اومدی!\n\n"
        "دستورات:\n"
        "📅 /today — بازی‌های امروز\n"
        "🔮 /predict — پیش‌بینی بازی‌های امروز\n"
        "🏅 /leaderboard — جدول امتیازات\n"
        "🏟 /semifinal — پیش‌بینی ۴ تیم نیمه‌نهایی\n"
        "🏆 /champion — پیش‌بینی قهرمان\n"
        "📊 /mystats — آمار من\n"
    )

# ─── بازی‌های امروز ─────────────────────────────────────────
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    matches = await fetch_today_matches()
    if not matches:
        # اگر API نداد، از دیتابیس بخوان
        matches_db = db.get_today_matches()
        if not matches_db:
            await update.message.reply_text("📅 امروز بازی‌ای ثبت نشده.")
            return
        text = "📅 *بازی‌های امروز:*\n\n"
        for m in matches_db:
            text += f"🕐 {m['time']} | {m['home']} vs {m['away']}\n"
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    text = "📅 *بازی‌های امروز:*\n\n"
    for m in matches:
        home = m["homeTeam"]["shortName"] or m["homeTeam"]["name"]
        away = m["awayTeam"]["shortName"] or m["awayTeam"]["name"]
        time_str = format_match_time(m["utcDate"])
        stage = m.get("stage", "GROUP_STAGE")
        text += f"🕐 {time_str} | {home} 🆚 {away}\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── پیش‌بینی ────────────────────────────────────────────────
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    matches = await fetch_today_matches()

    if not matches:
        matches = db.get_today_matches()
        if not matches:
            await update.message.reply_text("⚠️ امروز بازی‌ای برای پیش‌بینی وجود نداره.")
            return

    now = datetime.now(timezone.utc)
    upcoming = []

    for m in matches:
        try:
            match_time = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            if match_time > now:
                upcoming.append(m)
        except:
            pass

    if not upcoming:
        await update.message.reply_text("⏰ زمان پیش‌بینی برای بازی‌های امروز تموم شده!")
        return

    for m in upcoming:
        match_id = m["id"]
        home = m["homeTeam"]["shortName"] or m["homeTeam"]["name"]
        away = m["awayTeam"]["shortName"] or m["awayTeam"]["name"]
        time_str = format_match_time(m["utcDate"])

        existing = db.get_prediction(user_id, match_id)
        if existing:
            await ctx.bot.send_message(
                update.effective_chat.id,
                f"✅ قبلاً پیش‌بینی کردی:\n{home} {existing['home']} - {existing['away']} {away}"
            )
            continue

        # ذخیره بازی در دیتابیس
        stage = map_stage(m.get("stage", "GROUP_STAGE"), m.get("matchday", 1))
        db.save_match(match_id, home, away, m["utcDate"], stage)

        kb = build_score_keyboard(match_id, home, away, "home_score")
        await ctx.bot.send_message(
            update.effective_chat.id,
            f"🔮 *پیش‌بینی:*\n⚽ {home} 🆚 {away}\n🕐 {time_str}",
            parse_mode="Markdown",
            reply_markup=kb
        )

def map_stage(api_stage: str, matchday: int) -> str:
    if "GROUP" in api_stage:
        if matchday == 1: return "group_1"
        if matchday == 2: return "group_2"
        return "group_3"
    mapping = {
        "LAST_16": "r16",
        "QUARTER_FINALS": "qf",
        "SEMI_FINALS": "sf",
        "THIRD_PLACE": "3rd",
        "FINAL": "final",
    }
    return mapping.get(api_stage, "group_1")

# ─── هندل کیبورد پیش‌بینی ───────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "noop":
        return

    # پیش‌بینی گل
    if data.startswith("pred|"):
        _, match_id, step, value = data.split("|")
        match_id = int(match_id)
        value = int(value)
        match = db.get_match(match_id)

        if step == "home_score":
            # ذخیره موقت گل میزبان
            ctx.user_data[f"home_{match_id}"] = value
            kb = build_score_keyboard(match_id, match["home"], match["away"], "away_score")
            await query.edit_message_text(
                f"🔮 *پیش‌بینی:*\n⚽ {match['home']} {value} 🆚 {match['away']}\n\nحالا گل‌های {match['away']} رو انتخاب کن:",
                parse_mode="Markdown",
                reply_markup=kb
            )

        elif step == "away_score":
            home_goals = ctx.user_data.get(f"home_{match_id}", 0)
            away_goals = value
            match_info = db.get_match(match_id)
            stage = match_info["stage"]
            is_knockout = stage in ("r16", "r8", "qf", "sf", "3rd", "final")

            if is_knockout and home_goals == away_goals:
                # پیش‌بینی وقت اضافه/پنالتی
                ctx.user_data[f"away_{match_id}"] = away_goals
                kb = build_extra_time_keyboard(match_id)
                await query.edit_message_text(
                    f"🔮 {match['home']} {home_goals} - {away_goals} {match['away']}\n\n"
                    "⚡ مساوی در مرحله حذفی! ادامه بازی چطور میشه؟",
                    reply_markup=kb
                )
            else:
                db.save_prediction(user_id, match_id, home_goals, away_goals)
                await query.edit_message_text(
                    f"✅ پیش‌بینی ثبت شد!\n"
                    f"⚽ {match['home']} {home_goals} - {away_goals} {match['away']}"
                )

    # پیش‌بینی وقت اضافه
    elif data.startswith("extra|"):
        _, match_id, extra_type = data.split("|")
        match_id = int(match_id)
        home_goals = ctx.user_data.get(f"home_{match_id}", 0)
        away_goals = ctx.user_data.get(f"away_{match_id}", 0)
        match = db.get_match(match_id)

        db.save_prediction(user_id, match_id, home_goals, away_goals, extra_time=extra_type)
        extra_labels = {
            "home_et": "تیم میزبان در وقت اضافه",
            "away_et": "تیم مهمان در وقت اضافه",
            "home_pen": "تیم میزبان در پنالتی",
            "away_pen": "تیم مهمان در پنالتی",
        }
        await query.edit_message_text(
            f"✅ پیش‌بینی ثبت شد!\n"
            f"⚽ {match['home']} {home_goals} - {away_goals} {match['away']}\n"
            f"⏱ {extra_labels.get(extra_type, extra_type)}"
        )

    # پیش‌بینی نیمه‌نهایی
    elif data.startswith("semi|"):
        _, team, slot = data.split("|")
        db.save_semifinal_pick(user_id, int(slot), team)
        await query.answer(f"✅ {team} انتخاب شد!", show_alert=True)

    # پیش‌بینی قهرمان
    elif data.startswith("champ|"):
        _, team = data.split("|")
        db.save_champion_pick(user_id, team)
        await query.edit_message_text(f"🏆 پیش‌بینی قهرمان ثبت شد: {team}")

# ─── جدول امتیازات ──────────────────────────────────────────
async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    standings = db.get_leaderboard()
    if not standings:
        await update.message.reply_text("هنوز امتیازی ثبت نشده!")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    text = "🏆 *جدول امتیازات*\n\n"
    for i, row in enumerate(standings):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        text += f"{medal} {row['name']}: *{row['score']}* امتیاز\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ─── پیش‌بینی نیمه‌نهایی‌ها ─────────────────────────────────
SEMIFINAL_TEAMS = [
    ["Brazil", "France", "England", "Germany", "Argentina", "Spain"],
    ["Portugal", "Netherlands", "Belgium", "Croatia", "Uruguay", "USA"],
    ["Morocco", "Japan", "Senegal", "Mexico", "Colombia", "Denmark"],
    ["Switzerland", "Poland", "Serbia", "Canada", "Ecuador", "Australia"],
]

async def cmd_semifinal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    existing = db.get_semifinal_picks(user_id)
    if existing and len(existing) == 4:
        teams = [e["team"] for e in existing]
        await update.message.reply_text(
            f"✅ قبلاً ۴ تیم نیمه‌نهایی رو انتخاب کردی:\n" +
            "\n".join(f"  {t}" for t in teams)
        )
        return

    for slot, group in enumerate(SEMIFINAL_TEAMS, 1):
        buttons = [[InlineKeyboardButton(t, callback_data=f"semi|{t}|{slot}")] for t in group]
        await ctx.bot.send_message(
            update.effective_chat.id,
            f"🏟 *تیم {slot} نیمه‌نهایی رو انتخاب کن:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# ─── پیش‌بینی قهرمان ────────────────────────────────────────
CHAMPION_TEAMS = [
    ["Brazil", "France", "England", "Germany"],
    ["Argentina", "Spain", "Portugal", "Netherlands"],
]

async def cmd_champion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    existing = db.get_champion_pick(user_id)
    if existing:
        await update.message.reply_text(f"✅ قبلاً قهرمان رو انتخاب کردی: {existing['team']}")
        return

    buttons = [[InlineKeyboardButton(t, callback_data=f"champ|{t}")] for row in CHAMPION_TEAMS for t in row]
    await update.message.reply_text(
        "🏆 *پیش‌بینی قهرمان جام جهانی ۲۰۲۶:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ─── آمار شخصی ──────────────────────────────────────────────
async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = db.get_user_stats(user_id)
    if not stats:
        await update.message.reply_text("هنوز پیش‌بینی‌ای نداری!")
        return

    text = (
        f"📊 *آمار من*\n\n"
        f"🎯 نتیجه دقیق: {stats['exact']} بار\n"
        f"✅ تیم + تفاضل: {stats['diff']} بار\n"
        f"🙂 فقط تیم درست: {stats['winner']} بار\n"
        f"❌ اشتباه: {stats['wrong']} بار\n\n"
        f"⭐ کل امتیاز: *{stats['total']}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─── اعلان روزانه ───────────────────────────────────────────
async def daily_announcement(ctx: ContextTypes.DEFAULT_TYPE):
    matches = await fetch_today_matches()
    if not matches:
        return

    chat_id = os.getenv("GROUP_CHAT_ID", "")
    if not chat_id:
        return

    text = "🌅 *بازی‌های امروز جام جهانی ۲۰۲۶*\n\n"
    for m in matches:
        home = m["homeTeam"]["shortName"] or m["homeTeam"]["name"]
        away = m["awayTeam"]["shortName"] or m["awayTeam"]["name"]
        time_str = format_match_time(m["utcDate"])
        text += f"⚽ {time_str} | {home} 🆚 {away}\n"

    text += "\n🔮 برای پیش‌بینی /predict رو بزن!"
    await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")

# ─── اجرا ───────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("semifinal", cmd_semifinal))
    app.add_handler(CommandHandler("champion", cmd_champion))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # اعلان روزانه ساعت ۸ صبح ایران
    iran_8am = datetime.now(IRAN_TZ).replace(hour=8, minute=0, second=0, microsecond=0)
    if iran_8am < datetime.now(IRAN_TZ):
        iran_8am += timedelta(days=1)
    app.job_queue.run_repeating(daily_announcement, interval=86400, first=iran_8am.astimezone(timezone.utc))

    print("🤖 بات شروع به کار کرد...")
    app.run_polling()

if __name__ == "__main__":
    main()
