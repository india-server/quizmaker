"""
SYNAPSE TELEGRAM BOT v4
Page range support added — "page 5 to 20" likhke specific pages process karo
"""

import os, io, json, logging, requests, tempfile
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Conversation states ──────────────────────
ASK_SUBJECT, ASK_TOPIC, ASK_PAGES, PROCESSING = range(4)

# ── Helpers ──────────────────────────────────
def progress_bar(pct: int, width: int = 18) -> str:
    f = int(width * pct / 100)
    return "█" * f + "░" * (width - f)

async def edit_prog(msg, text: str, pct: int):
    try:
        await msg.edit_text(
            f"{text}\n\n`[{progress_bar(pct)}]` *{pct}%*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

def parse_page_range(text: str, total_pages: int) -> tuple[int, int] | None:
    """
    Parse page range from user text.
    Supports:
      "5 to 20"  →  (5, 20)
      "5-20"     →  (5, 20)
      "5 se 20"  →  (5, 20)
      "all" / "poora" / "full" → (1, total_pages)
      "skip"     →  (1, total_pages)   same as all
    """
    text = text.strip().lower()

    # Full PDF keywords
    if any(w in text for w in ["all", "poora", "full", "puri", "skip", "sab"]):
        return (1, total_pages)

    # Patterns: "5 to 20", "5-20", "5 se 20", "page 5 to 20"
    import re
    m = re.search(r'(\d+)\s*(?:to|se|-|–)\s*(\d+)', text)
    if m:
        start = max(1, int(m.group(1)))
        end   = min(total_pages, int(m.group(2)))
        if start <= end:
            return (start, end)

    # Single page: "5"
    m = re.search(r'^\s*(\d+)\s*$', text)
    if m:
        p = int(m.group(1))
        if 1 <= p <= total_pages:
            return (p, p)

    return None  # Invalid


# ── Commands ─────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Synapse PDF Converter*\n\n"
        "PDF bhejo aur main questions extract kar dunga!\n\n"
        "🆕 *Page range support:*\n"
        "Ab specific pages bhi process kar sakte ho:\n"
        "`page 5 to 20` — sirf in pages ke questions\n"
        "`all` — poori PDF\n\n"
        "/help — Format guide\n"
        "/stats — Database stats",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help Guide*\n\n"
        "*Step 1:* PDF bhejo\n"
        "*Step 2:* Subject select karo\n"
        "*Step 3:* Topic type karo\n"
        "*Step 4:* Page range batao:\n\n"
        "```\n"
        "Examples:\n"
        "  5 to 20      → pages 5-20\n"
        "  10-30        → pages 10-30\n"
        "  10 se 30     → pages 10-30\n"
        "  all          → poori PDF\n"
        "  poora        → poori PDF\n"
        "```\n\n"
        "*PDF Format Expected:*\n"
        "```\n"
        "1. Question text (NDA-2021)\n"
        "(a) Option A  (b) Option B\n"
        "(c) Option C  (d) Option D\n"
        "Ans: (c) Explanation...\n"
        "```\n\n"
        "/cancel — Conversation cancel karo",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get(f"{BACKEND_URL}/stats", timeout=10)
        d = r.json()
        text = f"📊 *Database Stats*\n\n*Total:* {d['total']} questions\n\n"
        if d.get("by_subject"):
            text += "*By Subject:*\n" + "\n".join(f"  • {k}: {v}" for k,v in d["by_subject"].items())
        if d.get("by_exam"):
            text += "\n\n*By Exam:*\n" + "\n".join(f"  • {k}: {v}" for k,v in d["by_exam"].items() if k)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Backend connect nahi hua: {e}")


# ── PDF Flow ──────────────────────────────────

async def receive_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Sirf *PDF* files bhejo.", parse_mode="Markdown")
        return ConversationHandler.END

    ctx.user_data["file_id"]   = doc.file_id
    ctx.user_data["file_name"] = doc.file_name

    subjects = ["History", "Geography", "Polity", "Economics", "Science", "Maths", "English", "GK"]
    keyboard = [[InlineKeyboardButton(s, callback_data=f"subj:{s}")] for s in subjects]
    keyboard.append([InlineKeyboardButton("✏️ Khud type karo", callback_data="subj:__custom__")])

    await update.message.reply_text(
        f"📄 *{doc.file_name}* mila!\n\n*Subject kya hai?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASK_SUBJECT


async def got_subject_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.replace("subj:", "")
    if val == "__custom__":
        await q.edit_message_text("✏️ Subject ka naam likho:", parse_mode="Markdown")
        return ASK_SUBJECT
    ctx.user_data["subject"] = val
    await q.edit_message_text(
        f"✅ Subject: *{val}*\n\n*Topic kya hai?*\n_(e.g. Ancient India, Polity, Trigonometry)_",
        parse_mode="Markdown"
    )
    return ASK_TOPIC


async def got_subject_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["subject"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Subject: *{ctx.user_data['subject']}*\n\n*Topic likhein:*",
        parse_mode="Markdown"
    )
    return ASK_TOPIC


async def got_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["topic"] = update.message.text.strip()

    # Get total pages first — to show user how many pages PDF has
    prog = await update.message.reply_text("📄 PDF check ho raha hai...", parse_mode="Markdown")

    try:
        file = await ctx.bot.get_file(ctx.user_data["file_id"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            await file.download_to_drive(tmp.name)
            ctx.user_data["tmp_path"] = tmp.name

        # Get page count
        import fitz  # PyMuPDF
        _doc = fitz.open(tmp.name)
        total = _doc.page_count; _doc.close()
        ctx.user_data["total_pages"] = total

        await prog.delete()

        # Show page range options
        keyboard = [
            [InlineKeyboardButton(f"📄 Poori PDF ({total} pages)", callback_data="pages:all")],
            [
                InlineKeyboardButton("Pages 1-20",  callback_data="pages:1-20"),
                InlineKeyboardButton("Pages 1-50",  callback_data="pages:1-50"),
            ],
            [
                InlineKeyboardButton("Pages 21-40", callback_data="pages:21-40"),
                InlineKeyboardButton("Pages 51-71", callback_data=f"pages:51-{total}"),
            ],
            [InlineKeyboardButton("✏️ Custom range type karo", callback_data="pages:__custom__")],
        ]

        await update.message.reply_text(
            f"✅ Topic: *{ctx.user_data['topic']}*\n\n"
            f"📖 PDF mein *{total} pages* hain.\n\n"
            f"*Konse pages process karni hain?*\n"
            f"_(Specific range ke liye 'Custom' select karo)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ASK_PAGES

    except Exception as e:
        await prog.edit_text(f"❌ PDF read nahi hua: {e}")
        ctx.user_data.clear()
        return ConversationHandler.END


async def got_pages_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.replace("pages:", "")
    total = ctx.user_data.get("total_pages", 999)

    if val == "__custom__":
        await q.edit_message_text(
            f"✏️ *Page range type karo:*\n\n"
            f"PDF mein {total} pages hain.\n\n"
            f"Examples:\n"
            f"`5 to 20` — pages 5 to 20\n"
            f"`10-30` — pages 10 to 30\n"
            f"`all` — poori PDF",
            parse_mode="Markdown"
        )
        return ASK_PAGES

    if val == "all":
        ctx.user_data["page_start"] = 1
        ctx.user_data["page_end"]   = total
    else:
        import re
        m = re.search(r'(\d+)-(\d+)', val)
        if m:
            ctx.user_data["page_start"] = int(m.group(1))
            ctx.user_data["page_end"]   = min(int(m.group(2)), total)
        else:
            ctx.user_data["page_start"] = 1
            ctx.user_data["page_end"]   = total

    await q.edit_message_text(
        f"✅ Pages: *{ctx.user_data['page_start']} to {ctx.user_data['page_end']}*\n\n"
        f"🚀 Processing shuru...",
        parse_mode="Markdown"
    )
    await process_and_send(update, ctx, is_callback=True)
    return ConversationHandler.END


async def got_pages_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    total = ctx.user_data.get("total_pages", 999)

    result = parse_page_range(text, total)
    if not result:
        await update.message.reply_text(
            f"❌ Samajh nahi aaya. Try karo:\n"
            f"`5 to 20` ya `10-30` ya `all`\n\n"
            f"PDF mein {total} pages hain.",
            parse_mode="Markdown"
        )
        return ASK_PAGES  # Ask again

    start, end = result
    ctx.user_data["page_start"] = start
    ctx.user_data["page_end"]   = end

    await update.message.reply_text(
        f"✅ Pages: *{start} to {end}*\n\n🚀 Processing shuru...",
        parse_mode="Markdown"
    )
    await process_and_send(update, ctx, is_callback=False)
    return ConversationHandler.END


# ── Core Processing ───────────────────────────

async def process_and_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE, is_callback: bool):
    """Actual PDF processing + send result"""
    subject    = ctx.user_data["subject"]
    topic      = ctx.user_data["topic"]
    page_start = ctx.user_data.get("page_start", 1)
    page_end   = ctx.user_data.get("page_end", ctx.user_data.get("total_pages", 999))
    tmp_path   = ctx.user_data.get("tmp_path")
    file_name  = ctx.user_data["file_name"]

    chat_id = update.effective_chat.id

    prog_msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"⚡ *Processing...*\n\n"
            f"📚 Subject: *{subject}*\n"
            f"📝 Topic: *{topic}*\n"
            f"📄 Pages: *{page_start} → {page_end}*\n\n"
            f"`[░░░░░░░░░░░░░░░░░░]` *0%*"
        ),
        parse_mode="Markdown"
    )

    try:
        # If tmp_path not already downloaded (shouldn't happen but safety)
        if not tmp_path or not os.path.exists(tmp_path):
            await edit_prog(prog_msg, "📥 PDF download ho raha hai...", 10)
            file = await ctx.bot.get_file(ctx.user_data["file_id"])
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                await file.download_to_drive(tmp.name)
                tmp_path = tmp.name

        await edit_prog(prog_msg, f"🔍 Pages {page_start}-{page_end} se questions extract ho rahe hain...", 30)

        # Send to backend with page range params
        with open(tmp_path, "rb") as f:
            pages_msg = f"{page_start}-{page_end}" if not (page_start == 1 and page_end == ctx.user_data.get("total_pages")) else "all"
            timeout_secs = max(300, (page_end - page_start + 1) * 8)  # ~8s per page

            response = requests.post(
                f"{BACKEND_URL}/upload",
                files={"file": (file_name, f, "application/pdf")},
                params={
                    "subject":    subject,
                    "topic":      topic,
                    "use_llm":    True,
                    "page_start": page_start,
                    "page_end":   page_end,
                },
                timeout=timeout_secs
            )

        # Cleanup temp file
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        if response.status_code != 200:
            err = response.json().get("detail", f"HTTP {response.status_code}")
            raise ValueError(err)

        data      = response.json()
        questions = data["questions"]
        db_stats  = data["db"]
        total_q   = data["extracted"]

        await edit_prog(prog_msg, f"✅ {total_q} questions mili!", 90)

        # Build Synapse-format JSON
        synapse_json  = {subject: {topic: questions}}
        json_bytes    = json.dumps(synapse_json, ensure_ascii=False, indent=2).encode("utf-8")
        json_filename = f"{subject}_{topic}_p{page_start}-{page_end}.json".replace(" ", "_")

        await edit_prog(prog_msg, "📦 JSON file ready!", 100)
        await prog_msg.delete()

        caption = (
            f"✅ *Done!*\n\n"
            f"📚 *{subject}* — _{topic}_\n"
            f"📄 Pages: *{page_start} to {page_end}*\n"
            f"❓ *Questions: {total_q}*\n\n"
            f"💾 *MongoDB:*  +{db_stats['inserted']} new · "
            f"{db_stats['updated']} updated · {db_stats['skipped']} skipped\n\n"
            f"🌐 Website pe available hai!"
        )

        await ctx.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(json_bytes),
            filename=json_filename,
            caption=caption,
            parse_mode="Markdown"
        )

    except Exception as e:
        log.error(f"Processing error: {e}")
        try:
            await prog_msg.edit_text(
                f"❌ *Error:*\n```\n{str(e)[:300]}\n```\n\n"
                f"• PDF text-selectable hona chahiye\n"
                f"• Page range valid honi chahiye\n"
                f"/help se format dekho",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass

    ctx.user_data.clear()


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Cleanup temp file if exists
    tmp = ctx.user_data.get("tmp_path")
    if tmp and os.path.exists(tmp):
        try: os.unlink(tmp)
        except Exception: pass
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled. /start se dobara shuru karo.")
    return ConversationHandler.END


async def non_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 *PDF file bhejo* — main questions extract kar dunga!\n/help se guide dekho.",
        parse_mode="Markdown"
    )


# ── Main ─────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN .env mein set karo!")
        return

    # v22 compatible — timeouts in builder, not run_polling
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL, receive_pdf)],
        states={
            ASK_SUBJECT: [
                CallbackQueryHandler(got_subject_btn, pattern=r"^subj:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_subject_text),
            ],
            ASK_TOPIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_topic),
            ],
            ASK_PAGES: [
                CallbackQueryHandler(got_pages_btn, pattern=r"^pages:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_pages_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=600,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, non_pdf))

    print("=" * 45)
    print("⚡ Synapse Bot v4 running!")
    print(f"   Backend: {BACKEND_URL}")
    print("   Page range support: ON")
    print("=" * 45)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
