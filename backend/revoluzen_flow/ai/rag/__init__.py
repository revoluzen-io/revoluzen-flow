"""
RAG (Retrieval-Augmented Generation) layer for the Revoluzen Flow Copilot.

Augments the agent's answers with workspace-scoped retrieval over:
  - execution logs (failures in last 30d)
  - pipeline definitions (workflow IR)
  - catalog entries (connectors, step-types)
  - docs/*.md files

All retrieval is workspace-scoped via tenant isolation.
Embeddings run locally via Ollama nomic-embed-text; retrieved chunks
are sent in-prompt to whichever LLM provider is configured.

Disable entirely via REVOLUZEN_FLOW_DISABLE_RAG=1.
"""

from revoluzen_flow.ai.rag.embedder import Embedder
from revoluzen_flow.ai.rag.store import VectorStore
from revoluzen_flow.ai.rag.indexer import RAGIndexer
from revoluzen_flow.ai.rag.retrieve import retrieve

__all__ = ["Embedder", "VectorStore", "RAGIndexer", "retrieve"]
