import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List

import faiss
import fitz
import numpy as np
import pyodbc
import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException,Security, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("GROQ_API_KEY")
client = OpenAI(api_key=API_KEY, base_url="https://api.groq.com/openai/v1") if API_KEY else None
JWT_SECRET = os.getenv("JWT_SECRET")

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not configured.")

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24
bearer_scheme = HTTPBearer(auto_error=False)
app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def serve_frontend():
    return FileResponse(BASE_DIR / "templates" / "index.html")

@app.get("/login")
async def serve_login():
    return FileResponse(BASE_DIR / "templates" / "login.html")


@app.get("/signup")
async def serve_signup():
    return FileResponse(BASE_DIR / "templates" / "signup.html")
# Existing models and per-paper RAG architecture are intentionally retained.
# Loading is deferred so configuration and FastAPI startup can be diagnosed without
# immediately downloading/initialising model weights.
embedding_model = None
reranker_model = None
GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
MAX_RETRIES_PER_MODEL, MODEL_TIMEOUT_SECONDS = 2, 20
paper_indexes: Dict[int, Dict] = {}

def get_embedding_model():
    global embedding_model
    if embedding_model is None:
        embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    return embedding_model

def get_reranker_model():
    global reranker_model
    if reranker_model is None:
        reranker_model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", device="cpu")
    return reranker_model

def get_db_connection():
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={os.getenv('SQL_SERVER', 'localhost')};DATABASE={os.getenv('SQL_DATABASE', 'PaperMind')};"
        "Trusted_Connection=yes;"
    )

def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "iat": int(time.time()),
        "exp": int(time.time() + (JWT_EXPIRATION_HOURS * 60 * 60)),
    }

    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )


def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None
) -> int:

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required."
        )

    token = credentials.credentials

    if not token:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token."
        )

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM]
        )

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.isdigit():
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token."
            )

        user_id = int(subject)
        try:
            with get_db_connection() as conn:
                user_exists = conn.cursor().execute(
                    "SELECT 1 FROM Users WHERE user_id = ?", user_id
                ).fetchone()
        except pyodbc.Error as exc:
            logger.exception("Session validation failed")
            raise HTTPException(
                status_code=503,
                detail="Could not validate the current session."
            ) from exc

        if not user_exists:
            raise HTTPException(
                status_code=401,
                detail="User account no longer exists."
            )
        return user_id

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Authentication token has expired."
        )

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token."
        )

def get_paper_metadata(paper_id: int) -> Dict:
    try:
        with get_db_connection() as conn:
            row = conn.cursor().execute(
                "SELECT paper_id, filename, title, upload_date FROM Papers WHERE paper_id = ?", paper_id
            ).fetchone()
    except pyodbc.Error as exc:
        logger.exception("Database lookup failed for paper %s", paper_id)
        raise HTTPException(503, "Database is unavailable.") from exc
    if not row:
        raise HTTPException(404, f"Paper {paper_id} does not exist.")
    return {"paper_id": row[0], "filename": row[1], "title": row[2], "upload_date": str(row[3])}

def get_owned_paper_metadata(
    paper_id: int,
    user_id: int
) -> Dict:
    try:
        with get_db_connection() as conn:
            row = conn.cursor().execute(
                """
                SELECT
                    paper_id,
                    filename,
                    title,
                    upload_date
                FROM Papers
                WHERE paper_id = ?
                  AND user_id = ?
                """,
                paper_id,
                user_id
            ).fetchone()

    except pyodbc.Error as exc:
        logger.exception(
            "Owned paper lookup failed for paper %s and user %s",
            paper_id,
            user_id
        )
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable."
        ) from exc

    if not row:
        raise HTTPException(
            status_code=404,
            detail="Paper not found."
        )

    return {
        "paper_id": row[0],
        "filename": row[1],
        "title": row[2],
        "upload_date": str(row[3])
    }
def save_paper_and_chunks(filename: str, title: str, chunks: List[Dict], user_id: int) -> int:
    """One transaction per uploaded paper: no orphan paper on chunk failure."""
    conn = cursor = None
    try:
        conn, cursor = get_db_connection(), None
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO Papers (filename, title, upload_date, user_id)
            OUTPUT INSERTED.paper_id
            VALUES (?, ?, GETDATE(), ?)
            """,
            filename,
            title,
            user_id
    )
        paper_id = cursor.fetchone()[0]
        cursor.executemany("""INSERT INTO Chunks (paper_id, chunk_text, page, section)
                              VALUES (?, ?, ?, ?)""",
                           [(paper_id, c["text"], c["page"], c["section"]) for c in chunks])
        conn.commit()
        return paper_id
    except pyodbc.Error as exc:
        if conn: conn.rollback()
        logger.exception("Database save failed for %s", filename)
        raise RuntimeError("Could not save this paper to the database.") from exc
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def detect_sections(text: str):
    splits = re.split(r"\n([A-Z][A-Z\s]{3,})\n", text)
    sections = [(splits[i].strip(), splits[i + 1].strip()) for i in range(1, len(splits) - 1, 2) if splits[i + 1].strip()]
    return sections or [("GENERAL", text)]

def extract_pdf_with_metadata(file_path: str) -> List[Dict]:
    try:
        doc = fitz.open(file_path)
    except (fitz.FileDataError, RuntimeError) as exc:
        raise ValueError("The uploaded file is not a readable PDF.") from exc
    documents = []
    try:
        for page_number, page in enumerate(doc, 1):
            text = page.get_text("text").strip()
            if text:
                documents.extend({"text": body, "page": page_number, "section": heading}
                                 for heading, body in detect_sections(text) if body.strip())
    finally:
        doc.close()
    return documents

def semantic_chunking(text: str, max_chars: int = 1200) -> List[str]:
    sentences, chunks, current = re.split(r"(?<=[.!?])\s+", text.strip()), [], ""
    for sentence in sentences:
        if not sentence: continue
        proposed = f"{current} {sentence}".strip()
        if current and len(proposed) > max_chars:
            chunks.append(current); current = sentence
        else: current = proposed
    return chunks + ([current] if current else [])

def build_chunk_store(documents: List[Dict], filename: str = "") -> List[Dict]:
    chunks = []
    for document in documents:
        for text in semantic_chunking(document["text"]):
            if len(re.sub(r"\W", "", text)) >= 20:
                chunks.append({"chunk_id": len(chunks), "text": text, "page": document["page"],
                               "section": document["section"], "filename": filename})
    if not chunks: raise ValueError("No meaningful text could be extracted from this PDF.")
    return chunks

def build_index_bundle(chunk_store: List[Dict]) -> Dict:
    texts = [chunk["text"] for chunk in chunk_store]
    embeddings = np.asarray(get_embedding_model().encode(texts), dtype="float32")
    index = faiss.IndexFlatL2(embeddings.shape[1]); index.add(embeddings)
    return {"faiss_index": index, "bm25": BM25Okapi([text.lower().split() for text in texts]), "chunk_store": chunk_store}

def build_indexes(documents: List[Dict], paper_id: int, filename: str = "") -> int:
    chunks = build_chunk_store(documents, filename)
    paper_indexes[paper_id] = build_index_bundle(chunks)
    return len(chunks)

def load_paper_from_db(paper_id: int, user_id: int) -> int:
    metadata = get_owned_paper_metadata(paper_id, user_id)
    try:
        with get_db_connection() as conn:
            rows = conn.cursor().execute("""SELECT chunk_id, chunk_text, page, section FROM Chunks
                                            WHERE paper_id = ? ORDER BY chunk_id""", paper_id).fetchall()
    except pyodbc.Error as exc:
        logger.exception("Chunk load failed for %s", paper_id)
        raise HTTPException(503, "Could not load paper chunks from the database.") from exc
    if not rows: raise HTTPException(409, f"Paper {paper_id} has no stored chunks.")
    chunks = [{"chunk_id": r[0], "text": r[1], "page": r[2], "section": r[3], "filename": metadata["filename"]} for r in rows]
    paper_indexes[paper_id] = build_index_bundle(chunks)
    return len(chunks)

def hybrid_search(query: str, paper_id: int, k: int = 20) -> List[Dict]:
    if paper_id not in paper_indexes: raise HTTPException(400, f"Paper {paper_id} is not loaded.")
    data, chunks = paper_indexes[paper_id], paper_indexes[paper_id]["chunk_store"]
    candidate_k = min(k * 4, len(chunks))
    distances, indexes = data["faiss_index"].search(np.asarray(get_embedding_model().encode([query]), dtype="float32"), candidate_k)
    semantic = {int(i): 1 / (1 + float(d)) for d, i in zip(distances[0], indexes[0]) if i != -1}
    bm25 = data["bm25"].get_scores(query.lower().split())
    candidates = set(semantic) | {int(i) for i in np.argsort(bm25)[::-1][:candidate_k]}
    values = [float(bm25[i]) for i in candidates]
    low, high = (min(values), max(values)) if values else (0.0, 0.0)
    results = []
    for i in candidates:
        keyword = 0.0 if low == high else (float(bm25[i]) - low) / (high - low)
        chunk, score = chunks[i], .7 * semantic.get(i, 0.0) + .3 * keyword
        results.append({"text": chunk["text"], "metadata": {"paper_id": paper_id, "chunk_id": chunk["chunk_id"],
                        "filename": chunk.get("filename", "Unknown paper"), "page": chunk["page"], "section": chunk["section"],
                        "retrieval_score": round(score, 4)}})
    return sorted(results, key=lambda r: r["metadata"]["retrieval_score"], reverse=True)[:k]

def rerank_results(query: str, results: List[Dict], top_k: int = 5) -> List[Dict]:
    if not results: return []
    scored = []
    for result, score in zip(results, get_reranker_model().predict([[query, r["text"]] for r in results])):
        meta = result["metadata"].copy(); meta["rerank_score"] = round(float(score), 4)
        scored.append({"text": result["text"], "metadata": meta})
    return sorted(scored, key=lambda r: r["metadata"]["rerank_score"], reverse=True)[:top_k]

def build_prompt(context_chunks: List[Dict], question: str, mode: str, level: str, selected_papers: Dict[int, Dict]) -> str:
    sources = []
    for number, result in enumerate(context_chunks, 1):
        m = result["metadata"]
        paper = selected_papers[m["paper_id"]]
        sources.append(f"[SOURCE {number}]\nPaper ID: {m['paper_id']}\nPaper title: {paper['title']}\nPaper filename: {paper['filename']}\n"
                       f"Page: {m['page']}\nSection: {m['section']}\nText: {result['text']}")
    levels = {"10 year old": "Use simple words and analogies.", "college student": "Explain with moderate technical depth.",
              "undergraduate": "Explain with moderate technical depth.", "researcher": "Use rigorous academic language."}
    task = "Focus on relevant equations; reproduce only supplied equations and explain variables and steps." if mode == "equation" else ("Discuss strengths or limitations only when supported by sources." if mode == "analysis" else "Give a structured answer.")
    selected_paper_list = "\n".join(
        f"- Paper {paper_id}: {paper['title']} ({paper['filename']})"
        for paper_id, paper in selected_papers.items()
    )
    return f'''You are PaperMind, a research-paper explanation assistant.

SOURCE GROUNDING:
Use ONLY the supplied excerpts as factual evidence.
Do not invent facts, equations, results, datasets, citations, pages, sections,
methods, terminology, or conclusions that are not supported by the supplied sources.

You may synthesize information from multiple supplied excerpts when the
relationship between them is supported by the sources. Explain the information
in your own words rather than simply copying or compressing the excerpts.

If the supplied context does not contain enough information to answer an important
part of the question, say "Not available in document." Do not use outside knowledge
to fill the missing information.

PAPER ATTRIBUTION:
Clearly identify the supporting paper naturally when it matters
(for example, "Paper 15 finds...").
When several papers are supplied, clearly distinguish their findings and compare
only information explicitly supported by the supplied sources.

Do not put page citations such as "[Paper 15, p. 3]" into normal prose, bullets,
or table cells. Pages remain backend evidence metadata and must never be invented.

ANSWER DEPTH:
Provide a detailed and informative answer rather than an unnecessarily short summary.

For normal questions:
- Aim for approximately 500-800 words when sufficient information is available.
- Do not compress multiple important findings into a single paragraph.
- Explain important concepts, findings, methods, results, and implications separately
  when they are supported by the sources.
- Include relevant examples or explanations from the supplied excerpts when useful.
- Explain WHY a finding or result is important when that explanation is supported
  by the supplied sources.
- Do not repeat the same information simply to increase the word count.

For simple factual questions, be concise and answer only what is needed.
For broad research questions, provide substantially more detail.

ANSWER STRUCTURE:
Use a structure that matches the question.

When appropriate, organize the answer as:
1. Brief overview
2. Main explanation
3. Key findings or observations
4. Detailed explanation of important findings
5. Implications or significance
6. Conclusion

Do not force all of these sections if they are not relevant.

Level:
{levels.get(level.lower(), "Explain clearly.")}

Task:
{task}

SELECTED PAPERS:
{selected_paper_list}

SOURCES:
{chr(10).join(sources)}

QUESTION:
{question}

Return valid JSON only with exactly this structure:
{{"blocks":[{{"type":"paragraph", "text":""}}]}}

CONTENT BLOCKS:
Choose the blocks that best answer the question.

Allowed block types:
- heading: {{"type":"heading", "text":""}}
- paragraph: {{"type":"paragraph", "text":""}}
- bullets: {{"type":"bullets", "items":[""]}}
- numbered_list: {{"type":"numbered_list", "items":[""]}}
- table: {{"type":"table", "headers":[""], "rows":[[""]]}}
- equation: {{"type":"equation", "name":"", "formula":"", "explanation":[""], "page":null}}

Formatting rules:
- Use headings to separate major concepts or findings.
- Use multiple paragraphs for detailed explanations.
- Use bullets for multiple related points.
- Use numbered lists for procedures or ordered steps.
- Use tables for meaningful comparisons.
- Use equation blocks only when relevant equations are explicitly supported
  by the supplied sources.
- In equation blocks, formula must contain only LaTeX.
- The explanation field must contain a list of explanatory steps.
- The page field must contain only a page number from a supplied source.
- Do not create placeholder blocks.
- Do not add generic filler merely to make the answer longer.
- Make the answer detailed because the user needs understanding, not just a summary.
'''
def ask_groq_with_fallback(prompt: str) -> str:
    if not client: raise HTTPException(503, "The language-model service is not configured.")
    for model in GROQ_MODELS:
        for attempt in range(MAX_RETRIES_PER_MODEL):
            try:
                response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],max_tokens=1200,temperature=0.3, timeout=MODEL_TIMEOUT_SECONDS)
                if response.choices and response.choices[0].message.content: return response.choices[0].message.content
            except Exception:
                logger.warning("Groq model %s attempt %s failed", model, attempt + 1)
                time.sleep(1.5)
    raise HTTPException(503, "The language-model service is temporarily unavailable.")

def parse_llm_answer(answer: str) -> Dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip(), flags=re.I)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Models sometimes add prose before an otherwise valid JSON object.
        parsed = None
        decoder = json.JSONDecoder()
        for position, char in enumerate(cleaned):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(cleaned[position:])
                if isinstance(candidate, dict):
                    parsed = candidate
                    break
            except json.JSONDecodeError:
                continue
        if parsed is None:
            logger.warning("Model returned non-JSON content; rendering it as plain text")
            return {"raw_response": cleaned or "The model returned an empty response.", "format_error": True}

    # Support both the original response shape and the current blocks contract.
    if isinstance(parsed, dict) and not parsed.get("blocks") and parsed.get("main_idea"):
        blocks = [{"type": "paragraph", "text": str(parsed["main_idea"])}]
        concepts = parsed.get("key_concepts")
        if isinstance(concepts, list):
            items = [
                f"{item.get('concept', 'Concept')}: {item.get('explanation', '')}"
                for item in concepts if isinstance(item, dict)
            ]
            if items:
                blocks.append({"type": "bullets", "items": items})
        if parsed.get("simple_summary"):
            blocks.append({"type": "paragraph", "text": str(parsed["simple_summary"])})
        parsed = {"blocks": blocks}

    allowed_types = {"heading", "paragraph", "bullets", "numbered_list", "table", "equation"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("blocks"), list) or not parsed["blocks"]:
        logger.warning("LLM JSON did not match answer schema; rendering it as text")
        return {"raw_response": json.dumps(parsed, ensure_ascii=False), "format_error": True}

    valid_blocks = [block for block in parsed["blocks"]
                    if isinstance(block, dict) and block.get("type") in allowed_types]
    if not valid_blocks:
        logger.warning("LLM JSON had no supported content blocks; rendering it as text")
        return {"raw_response": json.dumps(parsed, ensure_ascii=False), "format_error": True}
    return {"blocks": valid_blocks}
class SignupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)

@app.post("/auth/signup")
async def signup(request: SignupRequest):
    name = request.name.strip()
    email = request.email.strip().lower()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name cannot be empty."
        )
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Email cannot be empty."
        )

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            existing = cursor.execute(
                "SELECT user_id FROM Users WHERE email = ?",
                email
            ).fetchone()

            if existing:
                raise HTTPException(
                    status_code=409,
                    detail="An account with this email already exists."
                )

            password_hash = bcrypt.hashpw(
                request.password.encode("utf-8"),
                bcrypt.gensalt()
            ).decode("utf-8")

            user_id = cursor.execute(
                """
                INSERT INTO Users (name, email, password_hash)
                OUTPUT INSERTED.user_id
                VALUES (?, ?, ?)
                """,
                name,
                email,
                password_hash
            ).fetchone()[0]

            conn.commit()

    except HTTPException:
        raise

    except pyodbc.Error as exc:
        logger.exception("Signup database operation failed")
        raise HTTPException(
            status_code=503,
            detail="Could not create the account."
        ) from exc

    token = create_access_token(user_id)

    return {
        "message": "Account created successfully.",
        "token": token,
        "user": {
            "user_id": user_id,
            "name": name,
            "email": email
        }
    }


@app.post("/auth/login")
async def login(request: LoginRequest):
    email = request.email.strip().lower()
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Email cannot be empty."
        )

    try:
        with get_db_connection() as conn:
            row = conn.cursor().execute(
                """
                SELECT user_id, name, email, password_hash
                FROM Users
                WHERE email = ?
                """,
                email
            ).fetchone()

    except pyodbc.Error as exc:
        logger.exception("Login database operation failed")
        raise HTTPException(
            status_code=503,
            detail="Could not process login."
        ) from exc

    if not row:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password."
        )

    try:
        stored_hash = row[3] if isinstance(row[3], bytes) else str(row[3]).encode("utf-8")
        password_matches = bcrypt.checkpw(
            request.password.encode("utf-8"),
            stored_hash
        )
    except (AttributeError, TypeError, ValueError):
        logger.warning("Invalid password hash for user %s", row[0])
        password_matches = False

    if not password_matches:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password."
        )

    token = create_access_token(row[0])

    return {
        "message": "Login successful.",
        "token": token,
        "user": {
            "user_id": row[0],
            "name": row[1],
            "email": row[2]
        }
    }


@app.get("/auth/me")
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)
):
    user_id = get_current_user_id(credentials)

    try:
        with get_db_connection() as conn:
            row = conn.cursor().execute(
                """
                SELECT user_id, name, email, created_at
                FROM Users
                WHERE user_id = ?
                """,
                user_id
            ).fetchone()

    except pyodbc.Error as exc:
        logger.exception("User lookup failed")
        raise HTTPException(
            status_code=503,
            detail="Could not retrieve user information."
        ) from exc

    if not row:
        raise HTTPException(
            status_code=401,
            detail="User account no longer exists."
        )

    return {
        "user": {
            "user_id": row[0],
            "name": row[1],
            "email": row[2],
            "created_at": str(row[3])
        }
    }

class Query(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    paper_ids: List[int] = Field(...)
    level: str = "undergraduate"
    mode: str = "normal"

@app.post("/upload")
async def upload_pdf(files: List[UploadFile] = File(...),
                     credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)):
    user_id = get_current_user_id(credentials)
    if not files: raise HTTPException(400, "Upload at least one PDF.")
    if len(files) > 3: raise HTTPException(400, "A maximum of 3 PDFs may be uploaded at once.")
    papers, errors = [], []
    for uploaded in files:
        filename, temp_path = uploaded.filename or "unnamed.pdf", None
        if not filename.lower().endswith(".pdf"):
            errors.append({"filename": filename, "error": "Only PDF files are accepted."}); continue
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=BASE_DIR, delete=False) as temp:
                temp_path = temp.name; temp.write(await uploaded.read())
            chunks = build_chunk_store(extract_pdf_with_metadata(temp_path), filename)
            paper_id = save_paper_and_chunks(filename, Path(filename).stem, chunks, user_id)
            paper_indexes[paper_id] = build_index_bundle(chunks)
            papers.append({"paper_id": paper_id, "filename": filename, "title": Path(filename).stem, "chunk_count": len(chunks)})
        except (ValueError, RuntimeError) as exc:
            logger.info("Rejected upload %s: %s", filename, exc); errors.append({"filename": filename, "error": str(exc)})
        except Exception:
            logger.exception("Unexpected upload failure for %s", filename); errors.append({"filename": filename, "error": "Could not process this PDF."})
        finally:
            if temp_path:
                try: Path(temp_path).unlink(missing_ok=True)
                except OSError: logger.warning("Could not remove temp file %s", temp_path)
    if not papers: raise HTTPException(400, {"message": "No papers were uploaded.", "errors": errors})
    return {"message": "Papers uploaded successfully" if not errors else "Some papers uploaded successfully", "papers": papers, "errors": errors}

@app.post("/load/{paper_id}")
async def load_paper(
    paper_id: int,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)
):
    user_id = get_current_user_id(credentials)

    chunks_loaded = load_paper_from_db(paper_id, user_id)

    return {
        "message": "Paper loaded successfully",
        "paper_id": paper_id,
        "chunks_loaded": chunks_loaded
    }
@app.get("/papers")
async def get_papers(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)
):
    user_id = get_current_user_id(credentials)

    try:
        with get_db_connection() as conn:
            rows = conn.cursor().execute(
                """
                SELECT
                    p.paper_id,
                    p.filename,
                    p.title,
                    p.upload_date,
                    COUNT(c.chunk_id)
                FROM Papers p
                LEFT JOIN Chunks c
                    ON c.paper_id = p.paper_id
                WHERE p.user_id = ?
                GROUP BY
                    p.paper_id,
                    p.filename,
                    p.title,
                    p.upload_date
                ORDER BY p.upload_date DESC
                """,
                user_id
            ).fetchall()

    except pyodbc.Error as exc:
        logger.exception("Paper listing failed for user %s", user_id)
        raise HTTPException(
            status_code=503,
            detail="Could not retrieve saved papers."
        ) from exc

    return {
        "papers": [
            {
                "paper_id": row[0],
                "filename": row[1],
                "title": row[2],
                "upload_date": str(row[3]),
                "chunk_count": row[4]
            }
            for row in rows
        ]
    }

@app.delete("/papers/{paper_id}")
async def delete_paper(
    paper_id: int,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)
):
    user_id = get_current_user_id(credentials)

    # Make sure this paper belongs to the authenticated user.
    get_owned_paper_metadata(paper_id, user_id)

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM Papers WHERE paper_id = ? AND user_id = ?",
                paper_id,
                user_id
            )

            if cursor.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="Paper not found."
                )

            conn.commit()

    except HTTPException:
        raise

    except pyodbc.Error as exc:
        logger.exception(
            "Failed to delete paper %s for user %s",
            paper_id,
            user_id
        )
        raise HTTPException(
            status_code=503,
            detail="Could not delete the paper."
        ) from exc

    paper_indexes.pop(paper_id, None)

    return {
        "message": "Paper deleted successfully",
        "paper_id": paper_id
    }

@app.post("/ask")
async def ask_question(
    query: Query,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme)
):
    user_id = get_current_user_id(credentials)
    paper_ids = list(dict.fromkeys(query.paper_ids))
    if not paper_ids: raise HTTPException(400, "Select at least one paper before asking a question.")
    if len(paper_ids) > 3: raise HTTPException(400, "A maximum of 3 papers may be queried at once.")
    selected_papers = {}
    for paper_id in paper_ids:
        selected_papers[paper_id] = get_owned_paper_metadata(paper_id, user_id)
        if paper_id not in paper_indexes:
            load_paper_from_db(paper_id, user_id)
    candidates = [result for paper_id in paper_ids for result in hybrid_search(query.question, paper_id, 20)]
    if not candidates: raise HTTPException(404, "No relevant content was found in the selected papers.")
    reranked = rerank_results(query.question, candidates, len(candidates))
    # A comparison needs evidence from every selected paper. Reserve each
    # paper's best reranked excerpt, then fill the remaining context by score.
    results, included_indexes = [], set()
    if len(paper_ids) > 1:
        for paper_id in paper_ids:
            for index, result in enumerate(reranked):
                if result["metadata"]["paper_id"] == paper_id:
                    results.append(result)
                    included_indexes.add(index)
                    break
    for index, result in enumerate(reranked):
        if len(results) >= 5:
            break
        if index not in included_indexes:
            results.append(result)
    answer = parse_llm_answer(ask_groq_with_fallback(build_prompt(results, query.question, query.mode, query.level, selected_papers)))
    # Keep the full retrieved result (including text and both scores) available
    # to API consumers; the current frontend renders the structured answer.
    return {"answer": answer, "paper_ids": paper_ids, "sources": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
