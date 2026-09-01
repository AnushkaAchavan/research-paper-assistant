import os
import re
import fitz
import faiss
import json
import uuid
import time
import logging
import numpy as np
import pyodbc
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from typing import List, Dict
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer,CrossEncoder 
from rank_bm25 import BM25Okapi
from openai import OpenAI
load_dotenv()
# ==============================
# CONFIG
# ==============================

API_KEY = os.getenv("GROQ_API_KEY")
if not API_KEY:
    raise ValueError("GROQ_API_KEY not set in environment variables.")

client = OpenAI(
    api_key=API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_frontend():
    return FileResponse("templates/index.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
reranker_model = CrossEncoder(
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    device="cpu"
)
#==============================
# DATABASE CONNECTION
#==============================
def get_db_connection():
    server = os.getenv("SQL_SERVER", "localhost")
    database = os.getenv("SQL_DATABASE", "PaperMind")

    connection_string = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={database};"
        "Trusted_Connection=yes;"
    )

    return pyodbc.connect(connection_string)

def save_paper_to_db(filename, title):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO Papers (filename, title, upload_date)
        OUTPUT INSERTED.paper_id
        VALUES (?, ?, GETDATE())
        """,
        filename,
        title
    )

    paper_id = cursor.fetchone()[0]

    conn.commit()

    cursor.close()
    conn.close()

    return paper_id

def save_chunks_to_db(paper_id, chunks):
    conn = get_db_connection()
    cursor = conn.cursor()

    for chunk in chunks:

        text = chunk["text"]
        page = chunk["page"]
        section = chunk["section"]

        cursor.execute(
            """
            INSERT INTO Chunks
                (paper_id, chunk_text, page, section)
            VALUES (?, ?, ?, ?)
            """,
            paper_id,
            text,
            page,
            section
        )

    conn.commit()

    cursor.close()
    conn.close()
# ==============================
# # GORK FALLBACK CONFIG
# ==============================

GORK_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
]

MAX_RETRIES_PER_MODEL = 2
MODEL_TIMEOUT_SECONDS = 20

# ==============================
# GLOBAL STORES
# ==============================

faiss_index = None
bm25 = None
chunk_store = []

# ==============================
# PDF PROCESSING
# ==============================

def extract_pdf_with_metadata(file_path: str):
    doc = fitz.open(file_path)
    documents = []

    for page_num, page in enumerate(doc):
        text = page.get_text()
        sections = detect_sections(text)

        for section_title, section_text in sections:
            documents.append({
                "text": section_text,
                "page": page_num + 1,
                "section": section_title
            })

    doc.close()
    return documents


def detect_sections(text: str):
    section_patterns = r"\n([A-Z][A-Z\s]{3,})\n"
    splits = re.split(section_patterns, text)

    sections = []
    for i in range(1, len(splits), 2):
        title = splits[i].strip()
        content = splits[i + 1].strip()
        sections.append((title, content))

    if not sections:
        sections.append(("GENERAL", text))

    return sections

# ==============================
# SEMANTIC CHUNKING
# ==============================

def semantic_chunking(text: str, max_tokens=300):
    sentences = re.split(r'(?<=[.!?]) +', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if len(current_chunk) + len(sentence) < max_tokens:
            current_chunk += " " + sentence
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks

# ==============================
# INDEX BUILDING
# ==============================

def build_indexes(documents: List[Dict]):
    global faiss_index, bm25, chunk_store

    chunk_store = []

    for doc in documents:
        chunks = semantic_chunking(doc["text"])

        for chunk in chunks:
            chunk_store.append({
                "chunk_id": len(chunk_store),
                "text": chunk,
                "page": doc["page"],
                "section": doc["section"]
            })

    if not chunk_store:
        raise ValueError("No text extracted from PDF.")


    texts = [chunk["text"] for chunk in chunk_store]
    embeddings = embedding_model.encode(texts)
    dim = embeddings.shape[1]

    faiss_index = faiss.IndexFlatL2(dim)
    faiss_index.add(np.array(embeddings))


    tokenized = [
    chunk["text"].split()
    for chunk in chunk_store
]
    bm25 = BM25Okapi(tokenized)

#===========================================================
# LOAD PAPER FROM DATABASE
#===========================================================

def load_paper_from_db(paper_id):
    global faiss_index, bm25, chunk_store

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT chunk_id, chunk_text, page, section
        FROM Chunks
        WHERE paper_id = ?
        ORDER BY chunk_id
        """,
        paper_id
    )

    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    if not rows:
        raise ValueError(
            f"No chunks found for paper_id {paper_id}."
        )

    # Rebuild chunk_store from SQL Server
    chunk_store = []

    for row in rows:
        chunk_store.append({
            "chunk_id": row[0],
            "text": row[1],
            "page": row[2],
            "section": row[3]
        })

    # Rebuild FAISS
    texts = [chunk["text"] for chunk in chunk_store]

    embeddings = embedding_model.encode(texts)

    dim = embeddings.shape[1]

    faiss_index = faiss.IndexFlatL2(dim)

    faiss_index.add(
        np.array(embeddings)
    )

    # Rebuild BM25
    tokenized = [
        chunk["text"].split()
        for chunk in chunk_store
    ]

    bm25 = BM25Okapi(tokenized)

    return len(chunk_store)
# ==============================
# HYBRID RETRIEVAL
# ==============================
def hybrid_search(query: str, k=5):

    if faiss_index is None or bm25 is None:
        raise HTTPException(
            status_code=400,
            detail="No document uploaded yet."
        )

    # --------------------------------------------------
    # 1. Convert the user's query into an embedding
    # --------------------------------------------------
    query_vec = embedding_model.encode([query])

    # --------------------------------------------------
    # 2. FAISS semantic search
    #
    # Search a larger candidate pool than k.
    # This gives us more candidates to rank later.
    # --------------------------------------------------
    candidate_k = min(k * 4, len(chunk_store))

    D, I = faiss_index.search(
        np.array(query_vec),
        candidate_k
    )

    semantic_distances = D[0]
    semantic_indices = I[0]

    # --------------------------------------------------
    # 3. Convert FAISS L2 distance into a similarity score
    #
    # Smaller L2 distance = better.
    # We convert it so that:
    #
    # higher score = better
    # --------------------------------------------------
    semantic_scores = {}

    for distance, idx in zip(
        semantic_distances,
        semantic_indices
    ):
        if idx == -1:
            continue

        semantic_scores[int(idx)] = 1 / (1 + float(distance))

    # --------------------------------------------------
    # 4. BM25 keyword search
    # --------------------------------------------------
    tokenized_query = query.split()

    bm25_scores = bm25.get_scores(
        tokenized_query
    )

    # Get candidate chunks from BM25
    keyword_candidate_indices = np.argsort(
        bm25_scores
    )[::-1][:candidate_k]

    # --------------------------------------------------
    # 5. Combine FAISS + BM25 candidates
    # --------------------------------------------------
    candidate_indices = set(
        semantic_scores.keys()
    ) | set(
        int(idx)
        for idx in keyword_candidate_indices
    )

    # --------------------------------------------------
    # 6. Normalize BM25 scores
    # --------------------------------------------------
    max_bm25 = max(
        [bm25_scores[idx] for idx in candidate_indices],
        default=0
    )

    min_bm25 = min(
        [bm25_scores[idx] for idx in candidate_indices],
        default=0
    )

    bm25_normalized = {}

    for idx in candidate_indices:

        score = float(bm25_scores[idx])

        if max_bm25 == min_bm25:
            normalized = 0.0
        else:
            normalized = (
                (score - min_bm25)
                / (max_bm25 - min_bm25)
            )

        bm25_normalized[idx] = normalized

    # --------------------------------------------------
    # 7. Calculate final hybrid score
    #
    # 70% semantic relevance
    # 30% keyword relevance
    # --------------------------------------------------
    hybrid_scores = {}

    for idx in candidate_indices:

        semantic_score = semantic_scores.get(
            idx,
            0.0
        )

        keyword_score = bm25_normalized.get(
            idx,
            0.0
        )

        final_score = (
            0.7 * semantic_score
            + 0.3 * keyword_score
        )

        hybrid_scores[idx] = final_score

    # --------------------------------------------------
    # 8. Rank candidates by final score
    # --------------------------------------------------
    ranked_indices = sorted(
        hybrid_scores,
        key=hybrid_scores.get,
        reverse=True
    )[:k]

    # --------------------------------------------------
    # 9. Build retrieval results
    # --------------------------------------------------
    results = []

    for idx in ranked_indices:

        chunk = chunk_store[idx]

        results.append({
            "text": chunk["text"],
            "metadata": {
                "chunk_id": chunk["chunk_id"],
                "page": chunk["page"],
                "section": chunk["section"],
                "retrieval_score": round(
                    hybrid_scores[idx],
                    4
                )
            }
        })

    return results

def rerank_results(
        query:str,
        results:list,
        top_k:int=5
):
    """
    Rerank the retrieved results using a CrossEncoder model.
    The cross-encoder receives the query and each candidate chunk 
    together and produces a relevance score.
    """
 
    if not results:
        return []

    # Prepare pairs for the cross-encoder
    pairs = []

    #Create query-document pairs for reranking
    for result in results:
        pairs.append([
            query, 
            result["text"]
        ])

    # Get relevance scores from the cross-encoder
    scores = reranker_model.predict(pairs)

    # Attach scores to results
    scored_results = []

    for result, score in zip(results, scores):
        result_copy = result.copy()
        metadata = result_copy.get(
            "metadata",
            {}
        ).copy()

        metadata["rerank_score"] = round(float(score), 4)
        result_copy["metadata"] = metadata
        scored_results.append(result_copy)

        # Sort by rerank score
        scored_results.sort(
            key=lambda x: x["metadata"]["rerank_score"],
            reverse=True
        )


    return scored_results[:top_k]

# ==============================
# PROMPT BUILDER (GUARDRAILS)
# ==============================
def build_prompt(context_chunks, question, mode, level):

    context_parts = []

    for i, result in enumerate(context_chunks):

        metadata = result.get("metadata", {})

        page = metadata.get("page", "Unknown")
        section = metadata.get("section", "GENERAL")

        context_parts.append(
            f"""
    [Source {i + 1}]
    Page: {page}
    Section: {section}

    Text:
    {result["text"]}
    """
        )

    context = "\n".join(context_parts)

    base_instruction = """
You are PaperMind, a research-paper explanation assistant.

Your job is to answer the user's question using ONLY the
provided excerpts from the uploaded research paper.

STRICT GROUNDING RULES:

1. Use only information present in the provided context.

2. Do not use outside knowledge to fill missing information.

3. Do not invent facts, equations, results, datasets, citations,
   page numbers, or section names.

4. If the answer cannot be determined from the provided context,
   say:
   "Not available in document."

5. You may explain or simplify information from the context,
   but do not introduce new factual claims.

6. Every important factual claim should be supported by the
   provided source context.

7. Use the page and section metadata supplied with each source.
   Never guess a page number.

8. When multiple sources support an answer, use the relevant
   sources together.

9. For equations, reproduce only equations that actually appear
   in the provided context.

10. Clearly distinguish the paper's claims from your own
    explanation or interpretation.

Return valid JSON only.
"""



    # LEVEL LOGIC
    level_lower = level.lower()
    if level_lower in ["10 year old", "child", "beginner"]:
        level_instruction = """
Explain in very simple words.
Avoid technical terms.
Use short sentences.
Use analogies.
Make it easy enough for a 10 year old.
For key concepts, provide a simple explanation for each term in full sentences.
"""
    elif level_lower in ["college student", "undergraduate", "student"]:
        level_instruction = """
Explain clearly with moderate technical depth.
Define important terms.
Keep it academically accurate.
Assume basic background knowledge.
For key concepts, list each term with a clear explanation in complete sentences.
Do not leave any explanations blank.
"""
    elif level_lower in ["researcher", "expert", "phd"]:
        level_instruction = """
Explain with full technical depth.
Use formal academic language.
Include equations if present.
Discuss assumptions and limitations.
Be precise and rigorous.
For key concepts, provide detailed explanations, examples, and references to pages.
"""
    else:
        level_instruction = "Explain clearly and appropriately. Provide explanations for all key concepts."

    # Mode logic
    if mode == "equation":
        task = """
        Focus specifically on the mathematical equations relevant to the question.

        For every relevant equation:
        - Give the equation name.
        - Reproduce the mathematical expression using LaTeX.
        - Explain every important variable.
        - Explain the equation step by step.
        - Explain the intuition behind the equation.
        - Cite the page where the equation appears.
        - Do not invent equations or information not present in the context.
        """
    elif mode == "analysis":
        task = "Provide detailed paper analysis including strengths and weaknesses."
    else:
        task = "Answer normally with structured explanation."

    # NOTE: the JSON schema example below is kept as a PLAIN (non f-string)
    # string, because it contains literal `{` / `}` characters. Mixing those
    # into an f-string causes Python to try to parse them as expressions
    # (and the embedded colons get misread as format-specs), which raises
    # a SyntaxError / breaks the prompt. Keeping this block as a normal
    # string and concatenating it avoids that entirely.
    json_schema_block = """
Return JSON with exactly this structure:

{
  "main_idea": "",

  "key_concepts": [
    {
      "concept": "",
      "explanation": ""
    }
  ],

  "equations": [
    {
      "name": "",
      "formula": "",
      "explanation": [],
      "page": null
    }
  ],

  "real_world_example": "",

  "simple_summary": ""
}

IMPORTANT EQUATION RULES:

1. If the context contains equations relevant to the question, include them in the "equations" array.

2. Each equation must be a separate object.

3. Put ONLY the mathematical expression in "formula".

4. Write formulas using LaTeX notation.

5. Example:
   "formula": "\\\\mathrm{Attention}(Q,K,V)=\\\\mathrm{softmax}\\\\left(\\\\frac{QK^T}{\\\\sqrt{d_k}}\\\\right)V"

6. Put the explanation of the equation as separate steps inside the "explanation" array.

7. "page" must contain the page number from the provided context.

8. Do NOT invent equations.

9. If no relevant equation exists in the context, return:
   "equations": []

10. Never put a long paragraph containing equations inside "formula".

11. Preserve the mathematical meaning of equations from the paper.

12. If the paper gives multiple equations, return each equation as a separate object.
"""

    prompt_header = f"""
{base_instruction}

LEVEL INSTRUCTION:
{level_instruction}

TASK:
{task}

CONTEXT:
{context}

QUESTION:
{question}
"""

    return prompt_header + json_schema_block

# ==============================
#  GORK MULTI-MODEL FALLBACK
# ==============================

def ask_gork_with_fallback(prompt: str):

    last_error = None

    for model_name in GORK_MODELS:

        for attempt in range(MAX_RETRIES_PER_MODEL):

            try:
                logging.info(f"Trying {model_name} | Attempt {attempt+1}")

                # NOTE: Groq's OpenAI-compatible endpoint supports
                # chat.completions, not the newer `responses` API.
                # `client.responses.create(...)` was raising errors
                # (or 404s) against Groq's base_url on every call.
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=MODEL_TIMEOUT_SECONDS,
                )

                if response and response.choices and response.choices[0].message.content:
                    logging.info(f"Success with {model_name}")
                    return response.choices[0].message.content

                raise Exception("Empty response")

            except Exception as e:
                last_error = str(e)
                logging.warning(f"{model_name} failed: {e}")
                time.sleep(1.5)

        logging.info(f"Switching model from {model_name}")

    logging.error("All models failed.")

    return json.dumps({
        "error": "All models unavailable",
        "details": last_error
    })

# ==============================
# REQUEST MODEL
# ==============================

class Query(BaseModel):
    question: str
    level: str = "undergraduate"
    mode: str = "normal"

# ==============================
# API ROUTES
# ==============================

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):

    file_id = str(uuid.uuid4())
    file_path = f"temp_{file_id}.pdf"

    with open(file_path, "wb") as f:
        f.write(await file.read())

    # Extract sections from PDF
    documents = extract_pdf_with_metadata(file_path)

    # Build FAISS + BM25 indexes
    build_indexes(documents)

    # Save paper information in SQL Server
    filename = file.filename
    title = os.path.splitext(filename)[0]

    paper_id = save_paper_to_db(
        filename,
        title
    )

    # Save the same chunks used by FAISS/BM25
    save_chunks_to_db(
        paper_id,
        chunk_store
    )

    return {
        "message": "PDF processed with hybrid index and saved to SQL Server",
        "paper_id": paper_id
    }

@app.post("/load/{paper_id}")
async def load_paper(paper_id: int):

    try:
        total_chunks = load_paper_from_db(paper_id)

        return {
            "message": "Paper loaded successfully",
            "paper_id": paper_id,
            "chunks_loaded": total_chunks
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/papers")
async def get_papers():

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT paper_id, filename, title, upload_date
            FROM Papers
            ORDER BY upload_date DESC
            """
        )

        rows = cursor.fetchall()

        cursor.close()
        conn.close()

        papers = []

        for row in rows:
            papers.append({
                "paper_id": row[0],
                "filename": row[1],
                "title": row[2],
                "upload_date": str(row[3])
            })

        return {
            "papers": papers
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
@app.post("/ask")
async def ask_question(query: Query):
    # Retrieve context and build prompt
    results = hybrid_search(
        query.question,
        k=20
    )
    results = rerank_results(
        query.question,
        results,
        top_k=5
    )
    prompt = build_prompt(results, query.question, query.mode, query.level)

    # Ask Gork
    answer = ask_gork_with_fallback(prompt)

    # Clean the answer string
    cleaned = answer.strip()

    # Remove triple backticks and optional 'json' label
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = re.sub(r"^```json\s*|```$", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # If parsing fails, return as raw_response
        parsed = {"raw_response": cleaned}

    # Return as 'answer' key for frontend consistency
    return {"answer": parsed}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

