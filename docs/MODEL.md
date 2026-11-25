# ИИкузница — локальная модель (Q9)

**Модель:** Qwen3-8B-Instruct, GGUF Q4_K_M  
**Размер:** ~5 GB  
**Лицензия:** Apache-2.0  
**Источник:** https://huggingface.co/Qwen/Qwen3-8B-Instruct-GGUF (файл `Qwen3-8B-Instruct-Q4_K_M.gguf`)

## Требования к железу

- **GPU:** NVIDIA RTX 3050 (8 GiB VRAM) — минимальная конфигурация.
- **RAM:** 31.9 GiB (наша конфигурация i7-3770).
- **CPU:** x86_64, AVX2 поддержка желательна.
- **OS:** Linux (предпочтительно) / Windows (поддержка llama.cpp).

## Как поднять (profile `local`)

1. Скачать вес:
```bash
huggingface-cli download Qwen/Qwen3-8B-Instruct-GGUF Qwen3-8B-Instruct-Q4_K_M.gguf --local-dir ./models
```

2. Запустить llama.cpp server:
```bash
./llama-server -m models/Qwen3-8B-Instruct-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8080 \
  --ctx-size 8192 --threads 8 --n-gpu-layers 35 \
  --mlock --no-mmap
```

3. Проверить эндпоинт:
```bash
curl http://127.0.0.1:8080/v1/models
# {"object":"list","data":[{"id":"Qwen3-8B-Instruct-Q4_K_M","object":"model",...}]}
```

## Embedding-слой

**Модель:** bge-m3  
**Размер:** ~2 GB (FP16) / ~0.5 GB (ONNX int8)  
**Лицензия:** Apache-2.0  
**Как поднять:** FastEmbed / sentence-transformers локально на 8081 порту.

## Reranker

**Модель:** bge-reranker-base  
**Размер:** ~1 GB  
**Лицензия:** Apache-2.0  
**Как поднять:** локально на 8082 порту.

## Fallback (если 8B не влезает по скорости/VRAM)

**Модель:** Qwen3-4B-Instruct GGUF Q5_K_M  
**Размер:** ~3 GB  
**Лицензия:** Apache-2.0  
**Требования:** влезает в 6 GB VRAM с запасом.

---

**Важно (Q9):** в описании продукта фиксируется **одно** название — Qwen3-8B-Instruct GGUF Q4_K_M. Fallback не заявляется как вторая модель, держится в резерве.