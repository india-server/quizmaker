"""
SYNAPSE BACKEND v4 — FastAPI
PDF (2-column) → Groq FREE LLM → MongoDB → REST API

FREE LLM : Groq (llama-3.3-70b) — https://console.groq.com
PDF LIB  : PyMuPDF — 10x faster, better 2-column detection

Install:
  pip install fastapi uvicorn pymupdf pymongo groq python-multipart python-dotenv

Run:
  uvicorn main:app --reload --port 8000
"""

import os, re, json, tempfile, logging, time
from typing import Optional
from datetime import datetime

import fitz                                        # PyMuPDF — pip install pymupdf
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient, ASCENDING
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MONGO_URI  = os.getenv("MONGO_URI",    "mongodb://localhost:27017")
GROQ_KEY   = os.getenv("GROQ_API_KEY", "")
DB_NAME    = os.getenv("DB_NAME",      "synapse")
COLLECTION = os.getenv("COLLECTION",   "questions")

app = FastAPI(title="Synapse API", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

mongo_client = MongoClient(MONGO_URI)
col = mongo_client[DB_NAME][COLLECTION]
col.create_index([("uniqueId", ASCENDING)], unique=True, sparse=True)
col.create_index([("subject",  ASCENDING), ("topic", ASCENDING)])
col.create_index([("exam",     ASCENDING)])

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None
GROQ_MODEL  = "llama-3.3-70b-versatile"


# ═══════════════════════════════════════════════════════════════════
# STEP 1 — PyMuPDF 2-COLUMN EXTRACTION
#
# Why PyMuPDF > pdfplumber:
#   - Reads text blocks with (x, y, width, height) bounding boxes
#   - We sort blocks by column (left/right) then by y position
#   - Result: perfect top→bottom reading order per column
# ═══════════════════════════════════════════════════════════════════

def extract_columns_pymupdf(path: str, page_start: int = 1, page_end: int = None) -> str:
    """
    PyMuPDF-based 2-column extraction.
    For each page:
      1. Get all text blocks with their bounding boxes
      2. Split blocks into LEFT col (x < midpoint) and RIGHT col (x >= midpoint)
      3. Sort each column top→bottom by y coordinate
      4. Join: left_col_text + right_col_text
    """
    doc    = fitz.open(path)
    total  = doc.page_count
    p_end  = min((page_end or total), total)
    p_start = max(1, page_start)

    log.info(f"PDF: {total} pages — processing {p_start} to {p_end}")

    all_text = []

    for pnum in range(p_start - 1, p_end):   # fitz is 0-indexed
        page  = doc[pnum]
        pw    = page.rect.width               # page width
        mid   = pw * 0.50                     # column split point

        # Get text blocks: each is (x0, y0, x1, y1, text, block_no, block_type)
        blocks = page.get_text("blocks", sort=True)  # sort=True → reading order hint

        left_blocks  = []
        right_blocks = []

        for b in blocks:
            x0, y0, x1, y1, text, *_ = b
            if not text.strip():
                continue
            cx = (x0 + x1) / 2           # center x of block
            if cx < mid:
                left_blocks.append((y0, text))
            else:
                right_blocks.append((y0, text))

        # Sort each column top→bottom
        left_blocks.sort(key=lambda x: x[0])
        right_blocks.sort(key=lambda x: x[0])

        left_text  = "\n".join(t for _, t in left_blocks).strip()
        right_text = "\n".join(t for _, t in right_blocks).strip()

        # Detect if truly 2-column (both sides have numbered questions)
        has_qs_l = bool(re.search(r'\d{1,3}[\.\)]\s+\w', left_text))
        has_qs_r = bool(re.search(r'\d{1,3}[\.\)]\s+\w', right_text))

        if has_qs_l and has_qs_r:
            all_text.append(left_text)
            all_text.append(right_text)
        elif has_qs_l:
            all_text.append(left_text)
        elif has_qs_r:
            all_text.append(right_text)
        else:
            # Single column page (title page, index, etc.)
            all_text.append(page.get_text("text").strip())

    doc.close()
    result = "\n\n".join(all_text)
    log.info(f"Extracted: {len(result)} chars from {p_end - p_start + 1} pages")
    return result


# ═══════════════════════════════════════════════════════════════════
# STEP 2 — PREPROCESS
# ═══════════════════════════════════════════════════════════════════

def clean_option_text(t: str) -> str:
    """Clean a single option string — remove mid-word line breaks"""
    t = re.sub(r'([a-z])-\n([a-z])', r'\1\2', t)   # hyphen break
    t = re.sub(r'([a-z])\n([a-z])',   r'\1\2', t)   # soft break
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def preprocess(text: str) -> str:
    # Remove noise
    text = re.sub(r'PWOnlyIAS Extra Edge.*?\n', '', text, flags=re.I)
    text = re.sub(r'\d+\s*PYQs\s*\+.*?\n',    '', text)

    # Fix hard hyphen line-breaks: "pre-his-\ntoric" → "prehistoric"
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)

    # Fix soft mid-word breaks: "Da\nce" → "Dance"
    # Only when lowercase char ends line AND lowercase starts next line
    text = re.sub(r'([a-z])\n([a-z])', r'\1\2', text)

    # Normalize whitespace per line
    lines = [re.sub(r'[ \t]+', ' ', l).strip() for l in text.split('\n')]
    text  = '\n'.join(lines)
    text  = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# STEP 3 — GROQ LLM EXTRACTION
# ═══════════════════════════════════════════════════════════════════

def llm_extract_chunk(chunk: str, subject: str, topic: str, chunk_num: int, total: int) -> list[dict]:
    system = """You are a precise MCQ extractor for Indian competitive exam books.
OUTPUT: Pure JSON array only. No markdown, no explanation, no extra text.

RULES:
1. question = actual exam question ending with ?
2. options  = exactly 4 SHORT answer choices (2-20 words each)
3. answer   = single letter: a, b, c, or d
4. explanation = 1-2 sentences from "Ans:" section only
5. exam     = detect from patterns like (CDS-1, 2020) (CAPF- 2022) [NDA Exam, 2023]
6. year     = 4-digit year string

OPTIONS MUST BE SHORT:
  GOOD: ["Babur", "Humayun", "Akbar", "Aurangzeb"]
  GOOD: ["Both 1 and 2", "1 only", "2 only", "Neither 1 nor 2"]
  BAD : ["Option (a) is correct: The battle..."]   ← SKIP entire question
  BAD : ["Statement 1 is correct: Under Mughals"]  ← SKIP entire question

Skip any question where options contain explanation text."""

    user = f"""Extract MCQ questions. Subject: {subject} | Topic: {topic} | Chunk {chunk_num}/{total}

TEXT:
{chunk}

Return: [{{"id":int,"question":"...?","options":["a","b","c","d"],"answer":"x","explanation":"...","exam":"...","year":"..."}}]"""

    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role":"system","content":system},{"role":"user","content":user}],
                temperature=0.1,
                max_tokens=4096,
                response_format={"type":"json_object"},
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)

            # Unwrap if dict wrapper
            if isinstance(parsed, dict):
                for key in ["questions","data","results","items"]:
                    if key in parsed and isinstance(parsed[key], list):
                        return parsed[key]
                for v in parsed.values():
                    if isinstance(v, list):
                        return v
            if isinstance(parsed, list):
                return parsed

        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e).lower():
                wait = 15 * (attempt + 1)
                log.warning(f"Rate limit chunk {chunk_num}, waiting {wait}s")
                time.sleep(wait)
            else:
                log.error(f"Groq error chunk {chunk_num}: {e}")
                return []

    return []


def validate_question(q: dict) -> bool:
    question = str(q.get("question","")).strip()
    options  = q.get("options", [])
    answer   = str(q.get("answer","")).strip().lower()

    if len(question) < 15 or len(question) > 600: return False
    if answer not in ['a','b','c','d']:            return False
    if re.search(r'\([a-d]\)\s+\w{3,}', question): return False

    valid_opts = [o for o in options if isinstance(o,str) and len(o.strip()) > 1]
    if len(valid_opts) < 2: return False

    bad = [r'is correct:',r'are incorrect:',r'Statement \d',r'Option \(',r'PWOnlyIAS',r'\d{4}\s+CE']
    for opt in valid_opts:
        if len(opt.strip()) > 200: return False
        for p in bad:
            if re.search(p, opt, re.I): return False
    return True


def llm_extract(text: str, subject: str, topic: str) -> list[dict]:
    if not groq_client:
        log.warning("No GROQ_API_KEY")
        return []

    CHUNK_SIZE = 15000
    DELAY      = 3.0

    chunks     = [text[i:i+CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
    total      = len(chunks)
    all_qs     = []
    seen       = set()

    log.info(f"LLM: {len(text)} chars → {total} chunks, ~{int(total*(DELAY+2))}s estimated")

    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(DELAY)

        log.info(f"[{idx+1}/{total}] chunk {len(chunk)} chars | so far: {len(all_qs)} questions")
        raw = llm_extract_chunk(chunk, subject, topic, idx+1, total)

        added = 0
        for q in raw:
            if not validate_question(q): continue
            fp = re.sub(r'\W','',str(q.get("question",""))[:40]).lower()
            if fp in seen: continue
            seen.add(fp)
            all_qs.append(q)
            added += 1

        log.info(f"[{idx+1}/{total}] {len(raw)} raw → {added} added | total: {len(all_qs)}")

    log.info(f"LLM done: {len(all_qs)} valid questions")
    return all_qs


# ═══════════════════════════════════════════════════════════════════
# STEP 4 — REGEX FALLBACK
# ═══════════════════════════════════════════════════════════════════

def regex_parse(text: str) -> list[dict]:
    questions = []
    blocks    = re.split(r'\n(?=\d{1,3}[\.\)]\s+[A-Z])', text)

    for block in blocks:
        block = block.strip()
        if len(block) < 60: continue

        q = {}
        num_m = re.match(r'^(\d{1,3})[\.\)]\s+', block)
        if not num_m: continue
        q["id"] = int(num_m.group(1))
        rest = block[num_m.end():]

        for pat in [r'\[([^\]]+?)(20\d{2})\]', r'\(([A-Z]{2,6})[^\)]*?(20\d{2})\)']:
            em = re.search(pat, rest)
            if em:
                raw = em.group(0)
                for name in ['CAPF','NDA','CDS','AFCAT','UPSC','EPFO','CGS']:
                    if name in raw: q["exam"] = name; break
                yr = re.search(r'20\d{2}', raw)
                if yr: q["year"] = yr.group(0)
                break

        opt_m = re.compile(r'\(([a-d])\)\s*(.*?)(?=\([a-d]\)|\nAns\b|$)', re.DOTALL|re.I)
        opts  = {}
        for letter, txt in opt_m.findall(rest):
            txt = re.sub(r'\s+',' ',txt).strip()
            txt = re.sub(r'(Ans:|Explanation:).*','',txt,flags=re.I).strip()
            if 2 < len(txt) < 150: opts[letter.lower()] = txt
        q["options"] = [opts[l] for l in "abcd" if l in opts]
        if len(q["options"]) < 2: continue

        ans_m = re.search(r'Ans:\s*\(?([a-d])\)?', rest, re.I)
        if not ans_m: continue
        q["answer"] = ans_m.group(1).lower()

        exp_m = re.search(r'Ans:.*?\)\s*(.+?)(?:\n\n|\n\d{1,3}[\.\)]|$)', rest, re.DOTALL|re.I)
        if exp_m:
            exp = re.sub(r'\s+',' ',exp_m.group(1)).strip()
            q["explanation"] = '. '.join(re.split(r'(?<=[.!?]) ',exp)[:2])

        main = re.split(r'\([a-d]\)', rest, flags=re.I)[0]
        main = re.sub(r'\[.*?\]|\(.*?20\d{2}.*?\)','',main)
        main = re.sub(r'\s+',' ',main).strip()
        q["question"] = main

        if validate_question(q): questions.append(q)

    log.info(f"Regex: {len(questions)} questions")
    return questions


# ═══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════

def process_pdf(path: str, subject: str, topic: str,
                use_llm: bool = True,
                page_start: int = 1, page_end: int = None) -> list[dict]:

    page_info = f"pages {page_start}–{page_end}" if page_end else f"page {page_start} onwards (all)"
    log.info(f"=== {subject}/{topic} | {page_info} ===")

    raw  = extract_columns_pymupdf(path, page_start, page_end)
    if not raw.strip():
        raise ValueError("PDF se text nahi mila. Scanned image PDF hai kya?")

    text = preprocess(raw)
    log.info(f"Text ready: {len(text)} chars")

    questions = []
    if use_llm and groq_client:
        questions = llm_extract(text, subject, topic)
    if not questions:
        log.info("Regex fallback...")
        questions = regex_parse(text)

    clean = []
    seen  = set()
    for q in questions:
        if not q.get("question") or not q.get("options") or not q.get("answer"): continue
        snip = re.sub(r'\W','',str(q["question"]))[:28]
        uid  = f"{subject}|{topic}|{q.get('id','x')}|{snip}"
        if uid in seen: continue
        seen.add(uid)
        q.update({"subject":subject,"topic":topic,"uniqueId":uid,"createdAt":datetime.utcnow().isoformat()})
        clean.append(q)

    log.info(f"=== Done: {len(clean)} questions ===")
    return clean


# ═══════════════════════════════════════════════════════════════════
# MONGODB
# ═══════════════════════════════════════════════════════════════════

def save_to_mongo(questions: list[dict]) -> dict:
    inserted = updated = skipped = 0
    for q in questions:
        try:
            r = col.update_one({"uniqueId":q["uniqueId"]},{"$set":q},upsert=True)
            if   r.upserted_id:    inserted += 1
            elif r.modified_count: updated  += 1
            else:                  skipped  += 1
        except Exception as e:
            log.error(f"Mongo: {e}"); skipped += 1
    return {"inserted":inserted,"updated":updated,"skipped":skipped}


# ═══════════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
def root():
    return {"status":"ok","service":"Synapse API v4","pdf_lib":"PyMuPDF","llm":"Groq free"}

@app.get("/health")
def health():
    try:
        mongo_client.admin.command("ping")
        return {"status":"ok","db":"connected","groq":"ready" if groq_client else "no key"}
    except Exception as e:
        return {"status":"error","db":str(e)}


@app.post("/upload")
async def upload_pdf(
    file:       UploadFile = File(...),
    subject:    str  = Query(...,    description="e.g. History"),
    topic:      str  = Query(...,    description="e.g. Ancient India"),
    use_llm:    bool = Query(True,   description="Use Groq LLM"),
    page_start: int  = Query(1,      description="Start page number (1-indexed)"),
    page_end:   Optional[int] = Query(None, description="End page (leave empty = last page)"),
):
    """
    PDF upload → extract → save → return

    Page range examples (Telegram bot mein bhi kaam karta hai):
      page_start=5&page_end=20   → sirf pages 5 to 20
      page_start=10              → page 10 se end tak
      (default)                  → poori PDF
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Sirf .pdf files")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        questions = process_pdf(tmp_path, subject, topic, use_llm, page_start, page_end)
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        os.unlink(tmp_path)

    if not questions:
        raise HTTPException(422, "Koi questions nahi mili. /debug/extract-text se check karein.")

    stats = save_to_mongo(questions)
    return {"success":True,"extracted":len(questions),"db":stats,"questions":questions}


@app.get("/questions")
def get_questions(
    subject: Optional[str] = None, topic: Optional[str] = None,
    exam:    Optional[str] = None,  year:  Optional[str] = None,
    limit: int = Query(500, le=2000), skip: int = 0
):
    query = {}
    if subject: query["subject"] = subject
    if topic:   query["topic"]   = topic
    if exam:    query["exam"]    = exam
    if year:    query["year"]    = year
    return {"count": len(r := list(col.find(query,{"_id":0}).skip(skip).limit(limit))), "questions": r}


@app.get("/subjects")
def get_subjects():
    pipeline = [{"$group":{"_id":"$subject","topics":{"$addToSet":"$topic"},"count":{"$sum":1}}},{"$sort":{"_id":1}}]
    return {"subjects":[{"name":r["_id"],"topics":sorted(r["topics"]),"count":r["count"]} for r in col.aggregate(pipeline)]}


@app.get("/stats")
def get_stats():
    return {
        "total":      col.count_documents({}),
        "by_exam":    {r["_id"] or "Unknown":r["count"] for r in col.aggregate([{"$group":{"_id":"$exam","count":{"$sum":1}}}])},
        "by_subject": {r["_id"] or "Unknown":r["count"] for r in col.aggregate([{"$group":{"_id":"$subject","count":{"$sum":1}}}])}
    }


@app.delete("/questions")
def delete_questions(subject: Optional[str]=None, topic: Optional[str]=None):
    q = {}
    if subject: q["subject"]=subject
    if topic:   q["topic"]=topic
    return {"deleted": col.delete_many(q).deleted_count}


@app.get("/pdf/info")
async def pdf_info(file: UploadFile = File(...)):
    """PDF ki total pages aur size dekho — page range decide karne se pehle"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    doc = fitz.open(tmp_path)
    info = {"total_pages": doc.page_count, "filename": file.filename,
            "size_kb": round(os.path.getsize(tmp_path)/1024, 1)}
    doc.close(); os.unlink(tmp_path)
    return info


@app.post("/debug/extract-text")
async def debug_extract(
    file:       UploadFile = File(...),
    page_start: int = Query(1),
    page_end:   Optional[int] = Query(None)
):
    """Raw extracted text dekho — quality check ke liye"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        raw  = extract_columns_pymupdf(tmp_path, page_start, page_end)
        text = preprocess(raw)
    finally:
        os.unlink(tmp_path)
    return {
        "total_chars":      len(text),
        "first_2000":       text[:2000],
        "last_500":         text[-500:],
        "questions_found":  len(re.findall(r'\d{1,3}[\.\)]\s+[A-Z]', text))
    }
