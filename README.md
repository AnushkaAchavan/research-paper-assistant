 HEAD
# PaperMind — AI Research Paper Explainer

PaperMind accepts one to three research-paper PDFs, saves each paper independently in SQL Server, and answers questions only from the selected papers. Select two or three papers to compare evidence across them.

The RAG pipeline uses PyMuPDF extraction, sentence-based chunking, Sentence Transformers, per-paper FAISS semantic retrieval, per-paper BM25 keyword retrieval, CrossEncoder reranking, and Groq for structured answers.

## Setup

1. Create a SQL Server database (the default is `PaperMind`).
2. Run [database/schema.sql](database/schema.sql). It is safe to run on an existing database: it creates missing tables/indexes and never deletes records.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Create `.env`:

   ```env
   GROQ_API_KEY=your_key
   SQL_SERVER=localhost
   SQL_DATABASE=PaperMind
   JWT_SECRET=replace_with_a_long_random_secret
   ```

5. Start the application from the project directory:

   ```bash
   uvicorn main:app --reload
   ```

## API

- Open http://127.0.0.1:8000/login to create an account or log in. The browser stores the returned JWT locally, verifies it through /auth/me on each application load, and clears it on logout or expiry.
- POST /auth/signup and POST /auth/login return a JWT and user profile.
- POST /upload, GET /papers, POST /load/{paper_id}, DELETE /papers/{paper_id}, and POST /ask require an Authorization Bearer token.
- Every protected paper operation is scoped to its owner. Existing pre-authentication papers are deliberately left unassigned by the migration and are not shown to any account; assign them explicitly in SQL if they must be retained.

Example question payload:

```json
{
  "question": "Compare the approaches used in these papers.",
  "paper_ids": [1, 2],
  "level": "undergraduate",
  "mode": "normal"
}
```

For each selected paper, PaperMind retrieves independently, combines candidates, reranks them, and labels excerpts with paper ID, filename, page, and section. Grounded model citations use `[Paper <id>, p. <page>]`.
c6c458d (Checkpoint before Azure deployment)
