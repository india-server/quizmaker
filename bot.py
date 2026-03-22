"""
SYNAPSE TELEGRAM BOT v4.1 — Optimized Polling Mode
Works reliably on free tier with proper error handling
"""

import os
import io
import json
import logging
import requests
import tempfile
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "https://quizmaker-kci2.onrender.com")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# Conversation states
ASK_SUBJECT, ASK_TOPIC, ASK_PAGES, PROCESSING = range(4)

# ─────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────

def progress_bar(pct: int, width: int = 18) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)

async def edit_prog(msg, text: str, pct: int):
    try:
        await msg.edit_text(
            f"{text}\n\n`[{progress_bar(pct)}]` *{pct}%*",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Progress edit error: {e}")

def parse_page_range(text: str, total_pages: int):
    """Parse page range from user text"""
    text = text.strip().lower()
    
    # Full PDF keywords
    if any(w in text for w in ["all", "poora", "full", "puri", "sab", "complete"]):
        return (1, total_pages)
    
    # Range patterns
    import re
    m = re.search(r'(\d+)\s*(?:to|se|-|–)\s*(\d+)', text)
    if m:
        start = max(1, int(m.group(1)))
        end = min(total_pages, int(m.group(2)))
        if start <= end:
            return (start, end)
    
    # Single page
    m = re.search(r'^\s*(\d+)\s*$', text)
    if m:
        p = int(m.group(1))
        if 1 <= p <= total_pages:
            return (p, p)
    
    return None

# ─────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Synapse PDF Converter v4*\n\n"
        "📄 PDF bhejo aur main questions extract kar dunga!\n\n"
        "✨ *Features:*\n"
        "• 2-column PDF support\n"
        "• Page range selection (5 to 20, 10-30, etc.)\n"
        "• Groq AI extraction\n"
        "• JSON export\n\n"
        "/help — Format guide\n"
        "/stats — Database stats",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help Guide*\n\n"
        "*Step 1:* PDF file bhejo\n"
        "*Step 2:* Subject select karo\n"
        "*Step 3:* Topic type karo\n"
        "*Step 4:* Page range batao\n\n"
        "*Page Range Examples:*\n"
        "```\n"
        "5 to 20      → pages 5-20\n"
        "10-30        → pages 10-30\n"
        "all          → poori PDF\n"
        "skip         → poori PDF\n"
        "```\n\n"
        "*PDF Format Expected:*\n"
        "```\n"
        "1. Question text (NDA-2021)\n"
        "(a) Option A  (b) Option B\n"
        "(c) Option C  (d) Option D\n"
        "Ans: (c) Explanation...\n"
        "```\n\n"
        "/cancel — Cancel conversation",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prog = await update.message.reply_text("📊 Fetching stats...")
    try:
        r = requests.get(f"{BACKEND_URL}/stats", timeout=15)
        if r.status_code == 200:
            d = r.json()
            text = f"📊 *Database Stats*\n\n*Total:* {d.get('total', 0)} questions\n"
            if d.get("by_subject"):
                text += "\n*By Subject:*\n" + "\n".join(f"  • {k}: {v}" for k, v in d["by_subject"].items())
            if d.get("by_exam"):
                text += "\n\n*By Exam:*\n" + "\n".join(f"  • {k}: {v}" for k, v in d["by_exam"].items() if k)
            await prog.edit_text(text, parse_mode="Markdown")
        else:
            await prog.edit_text(f"❌ Backend error: {r.status_code}")
    except requests.exceptions.Timeout:
        await prog.edit_text("⏰ Backend timeout — maybe waking up. Try again in 10 seconds.")
    except Exception as e:
        await prog.edit_text(f"❌ Error: {str(e)[:100]}")
        log.error(f"Stats error: {e}")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    tmp = ctx.user_data.get("tmp_path")
    if tmp and os.path.exists(tmp):
        try:
            os.unlink(tmp)
        except Exception:
            pass
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled. /start se dobara shuru karo.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────
# PDF Processing Flow
# ─────────────────────────────────────────────────────────────────

async def receive_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Sirf *PDF* files bhejo.", parse_mode="Markdown")
        return ConversationHandler.END

    ctx.user_data["file_id"] = doc.file_id
    ctx.user_data["file_name"] = doc.file_name

    subjects = ["History", "Geography", "Polity", "Economy", "Science", "Maths", "English", "GK"]
    keyboard = [[InlineKeyboardButton(s, callback_data=f"subj:{s}")] for s in subjects]
    keyboard.append([InlineKeyboardButton("✏️ Custom subject", callback_data="subj:__custom__")])

    await update.message.reply_text(
        f"📄 *{doc.file_name}* received!\n\n*Select subject:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASK_SUBJECT

async def got_subject_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.replace("subj:", "")
    if val == "__custom__":
        await q.edit_message_text("✏️ Enter subject name:", parse_mode="Markdown")
        return ASK_SUBJECT
    ctx.user_data["subject"] = val
    await q.edit_message_text(
        f"✅ Subject: *{val}*\n\n*Enter topic:*\n_(e.g., Ancient India, Trigonometry)_",
        parse_mode="Markdown"
    )
    return ASK_TOPIC

async def got_subject_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["subject"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Subject: *{ctx.user_data['subject']}*\n\n*Enter topic:*",
        parse_mode="Markdown"
    )
    return ASK_TOPIC

async def got_topic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["topic"] = update.message.text.strip()
    
    prog = await update.message.reply_text("📄 Checking PDF...")
    
    try:
        file = await ctx.bot.get_file(ctx.user_data["file_id"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            await file.download_to_drive(tmp.name)
            ctx.user_data["tmp_path"] = tmp.name
        
        # Get page count
        import fitz
        doc = fitz.open(tmp.name)
        total_pages = doc.page_count
        doc.close()
        ctx.user_data["total_pages"] = total_pages
        
        await prog.delete()
        
        keyboard = [
            [InlineKeyboardButton(f"📄 All Pages ({total_pages})", callback_data="pages:all")],
            [
                InlineKeyboardButton("Pages 1-20", callback_data="pages:1-20"),
                InlineKeyboardButton("Pages 1-50", callback_data="pages:1-50"),
            ],
            [
                InlineKeyboardButton(f"Pages 51-{total_pages}", callback_data=f"pages:51-{total_pages}"),
            ],
            [InlineKeyboardButton("✏️ Custom range", callback_data="pages:__custom__")],
        ]
        
        await update.message.reply_text(
            f"✅ Topic: *{ctx.user_data['topic']}*\n\n"
            f"📖 PDF has *{total_pages} pages*\n\n"
            f"*Select page range:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ASK_PAGES
        
    except Exception as e:
        await prog.edit_text(f"❌ Error reading PDF: {e}")
        return ConversationHandler.END

async def got_pages_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.replace("pages:", "")
    total = ctx.user_data.get("total_pages", 999)
    
    if val == "__custom__":
        await q.edit_message_text(
            f"✏️ *Enter page range:*\n\n"
            f"Total pages: {total}\n\n"
            f"Examples:\n"
            f"`5 to 20` — pages 5-20\n"
            f"`10-30` — pages 10-30\n"
            f"`all` — all pages",
            parse_mode="Markdown"
        )
        return ASK_PAGES
    
    if val == "all":
        ctx.user_data["page_start"] = 1
        ctx.user_data["page_end"] = total
    else:
        import re
        m = re.search(r'(\d+)-(\d+)', val)
        if m:
            ctx.user_data["page_start"] = int(m.group(1))
            ctx.user_data["page_end"] = min(int(m.group(2)), total)
        else:
            ctx.user_data["page_start"] = 1
            ctx.user_data["page_end"] = total
    
    await q.edit_message_text(
        f"✅ Pages: *{ctx.user_data['page_start']} to {ctx.user_data['page_end']}*\n\n"
        f"🚀 Processing started...",
        parse_mode="Markdown"
    )
    await process_and_send(update, ctx, is_callback=True)
    return ConversationHandler.END

async def got_pages_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    total = ctx.user_data.get("total_pages", 999)
    
    result = parse_page_range(text, total)
    if not result:
        await update.message.reply_text(
            f"❌ Invalid format. Try:\n"
            f"`5 to 20` or `10-30` or `all`\n\n"
            f"Total pages: {total}",
            parse_mode="Markdown"
        )
        return ASK_PAGES
    
    start, end = result
    ctx.user_data["page_start"] = start
    ctx.user_data["page_end"] = end
    
    await update.message.reply_text(
        f"✅ Pages: *{start} to {end}*\n\n🚀 Processing...",
        parse_mode="Markdown"
    )
    await process_and_send(update, ctx, is_callback=False)
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────
# Main Processing
# ─────────────────────────────────────────────────────────────────

async def process_and_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE, is_callback: bool):
    subject = ctx.user_data["subject"]
    topic = ctx.user_data["topic"]
    page_start = ctx.user_data.get("page_start", 1)
    page_end = ctx.user_data.get("page_end", ctx.user_data.get("total_pages", 999))
    tmp_path = ctx.user_data.get("tmp_path")
    file_name = ctx.user_data["file_name"]
    
    chat_id = update.effective_chat.id
    
    prog_msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=f"⚡ Processing...\n\nSubject: *{subject}*\nTopic: *{topic}*\nPages: *{page_start} → {page_end}*\n\n`[░░░░░░░░░░░░░░░░░░] 0%`",
        parse_mode="Markdown"
    )
    
    try:
        await edit_prog(prog_msg, "📤 Sending to backend...", 20)
        
        with open(tmp_path, "rb") as f:
            timeout_secs = max(120, (page_end - page_start + 1) * 5)
            
            response = requests.post(
                f"{BACKEND_URL}/upload",
                files={"file": (file_name, f, "application/pdf")},
                params={
                    "subject": subject,
                    "topic": topic,
                    "use_llm": True,
                    "page_start": page_start,
                    "page_end": page_end,
                },
                timeout=timeout_secs
            )
        
        # Cleanup
        try:
            os.unlink(tmp_path)
        except:
            pass
        
        if response.status_code != 200:
            error = response.json().get("detail", f"HTTP {response.status_code}")
            raise Exception(error)
        
        data = response.json()
        questions = data.get("questions", [])
        db_stats = data.get("db", {})
        total_q = data.get("extracted", 0)
        
        await edit_prog(prog_msg, f"✅ Found {total_q} questions!", 90)
        
        # Create JSON
        synapse_json = {subject: {topic: questions}}
        json_bytes = json.dumps(synapse_json, ensure_ascii=False, indent=2).encode("utf-8")
        json_filename = f"{subject}_{topic}_p{page_start}-{page_end}.json".replace(" ", "_")
        
        await edit_prog(prog_msg, "📦 Creating JSON...", 100)
        await prog_msg.delete()
        
        caption = (
            f"✅ *Done!*\n\n"
            f"📚 *{subject}* — _{topic}_\n"
            f"📄 Pages: *{page_start} to {page_end}*\n"
            f"❓ Questions: *{total_q}*\n\n"
            f"💾 Database: +{db_stats.get('inserted', 0)} new, "
            f"{db_stats.get('updated', 0)} updated\n\n"
            f"🌐 Available in Aerivue web app!"
        )
        
        await ctx.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(json_bytes),
            filename=json_filename,
            caption=caption,
            parse_mode="Markdown"
        )
        
    except requests.exceptions.Timeout:
        await prog_msg.edit_text("⏰ Backend timeout — server might be waking up. Try again in 30 seconds.")
    except Exception as e:
        log.error(f"Processing error: {e}")
        await prog_msg.edit_text(f"❌ Error: {str(e)[:300]}")
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except:
            pass
    
    ctx.user_data.clear()

async def non_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 Please send a *PDF file* for processing.\n/help for guide.",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────────────────────────
# Main — Optimized Polling
# ─────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set in .env file!")
        return
    
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler
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
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=900,  # 15 minutes timeout
    )
    
    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, non_pdf))
    
    # Run polling with error handling
    print("=" * 50)
    print("⚡ Synapse Bot v4.1 Running (Polling Mode)")
    print(f"   Backend: {BACKEND_URL}")
    print("   Status: Waiting for messages...")
    print("=" * 50)
    
    # Run with proper error handling
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        timeout=60,
        read_timeout=60
    )

if __name__ == "__main__":
    main()
