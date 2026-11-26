"""Скачивание моделей LEX в D:\\models (разовый скрипт, не часть серверов)."""
import sys

from huggingface_hub import snapshot_download

ROOT = r"D:\models"

EMBED_ALLOW = [
    "config.json",
    "modules.json",
    "sentence_bert_config.json",
    "config_sentence_transformers.json",
    "1_Pooling/*",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "pytorch_model.bin",
]

RERANK_ALLOW = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "model.safetensors",
]


def get(repo, dest, allow):
    print(f"[download] {repo} -> {dest}", flush=True)
    p = snapshot_download(
        repo_id=repo,
        local_dir=dest,
        allow_patterns=allow,
        max_workers=4,
    )
    print(f"[ok] {repo}: {p}", flush=True)
    return p


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "embed"):
        get("BAAI/bge-m3", ROOT + r"\bge-m3", EMBED_ALLOW)
    if which in ("all", "rerank"):
        get("BAAI/bge-reranker-base", ROOT + r"\bge-reranker-base", RERANK_ALLOW)
    print("[done]", flush=True)
