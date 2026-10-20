"""Lecture-note search: chunks in ChromaDB, embeddings from Gemini.

Embeddings are ALWAYS computed here and passed in; Chroma's own default embedder is never used
(it would try to download a model). Postgres is the source of truth: search results are only returned for notes
that still exist there, so chunks left behind by a rolled-back transaction are ignored.
"""

from functools import lru_cache

import chromadb

from app.config import get_settings

CHUNK_WORDS = 180
OVERLAP_WORDS = 30


def embed(texts: list[str]) -> list[list[float]]:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    s = get_settings()
    return GoogleGenerativeAIEmbeddings(model=s.embedding_model, google_api_key=s.google_api_key).embed_documents(texts)


@lru_cache
def _collection(path: str):
    client = chromadb.PersistentClient(path=path)
    return client.get_or_create_collection("notes", embedding_function=None, metadata={"hnsw:space": "cosine"})


def collection():
    return _collection(get_settings().chroma_dir)


def chunk(text: str) -> list[str]:
    words = text.split()
    step = CHUNK_WORDS - OVERLAP_WORDS
    return [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, max(len(words) - OVERLAP_WORDS, 1), step)]


def index_note(note_id: int, class_id: int, title: str, text: str) -> int:
    """Store a note's chunks. Returns how many chunks were stored."""
    pieces = chunk(text)
    collection().upsert(
        ids=[f"note{note_id}-{i}" for i in range(len(pieces))],
        documents=pieces,
        embeddings=embed(pieces),
        metadatas=[{"class_id": class_id, "note_id": note_id, "title": title} for _ in pieces],
    )
    return len(pieces)


def search(class_ids: list[int], query: str, live_note_ids: set[int], k: int = 4) -> list[dict]:
    """Closest chunks from these classes' notes: [{title, text, note_id}], best first."""
    if not class_ids or not live_note_ids:
        return []
    found = collection().query(query_embeddings=embed([query]), n_results=k * 2,
                               where={"class_id": {"$in": class_ids}})
    hits = [{"title": m["title"], "text": d, "note_id": m["note_id"]}
            for d, m in zip(found["documents"][0], found["metadatas"][0]) if m["note_id"] in live_note_ids]
    return hits[:k]
