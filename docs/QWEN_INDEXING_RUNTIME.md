# Qwen indexing runtime

Shock's Art keeps multimodal indexing in an isolated runtime under `data/qwen_indexing` so the FastAPI/web environment does not need to import or share the GPU stack.

The repository source of truth is `config/qwen-indexing-runtime.json`. `tools/setup_qwen_indexing.ps1` reads that file and creates or repairs the isolated environment without modifying the normal app environment, system Python, NVIDIA driver, or system CUDA installation.

## Validated host

Validation date: 2026-08-18

- Windows 10 AMD64
- Python 3.13.5 in `data/qwen_indexing/.venv`
- NVIDIA GeForce RTX 3060, 12 GB
- Torch 2.8.0+cu128
- Torch CUDA runtime 12.8
- Qwen3-VL-Embedding source pinned to `393e2978d27852b0d0230d6994f37f9c15bed73c`
- `Qwen/Qwen3-VL-Embedding-2B` model snapshot pinned to `9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda`
- inference dtype: bfloat16
- native embedding dimension: 2048

The setup script records the resolved Python package set to `data/qwen_indexing/environment.freeze.txt`. That file is host evidence and is intentionally kept under ignored runtime data rather than committed as application dependencies.

IDX-017 validation reran the pinned setup successfully, confirmed the Qwen source HEAD exactly matched the pinned commit, confirmed the isolated Torch/CUDA runtime, produced `environment.freeze.txt`, left the app/system environments untouched, and reported 75 passing application tests before and after setup.

## Benchmark result

IDX-016 was validated against the existing 36 visual artifacts. Baseline and final application tests both reported 72 passing tests.

Model load:

- 4.3571 seconds
- 4058.96 MiB allocated after load
- 4064.0 MiB reserved after load

Text query `man playing guitar`:

- first encode: 0.5499 seconds
- repeat encode: 0.0723 seconds
- repeat cosine: 1.00000012
- output shape: `[1, 2048]`
- peak allocated VRAM: 4072.1 MiB

Image batches:

| Batch | Seconds | Images/sec | Peak allocated MiB |
| ---: | ---: | ---: | ---: |
| 1 | 1.4799 | 0.6757 | 4888.40 |
| 2 | 2.3341 | 0.8568 | 5695.68 |
| 4 | 4.5663 | 0.8760 | 7302.03 |
| 8 | 6.3903 | 1.2519 | 6941.33 |
| 12 | 9.4976 | 1.2635 | 8373.67 |
| 16 | 13.0390 | 1.2271 | 9870.25 |

All tested batches through 16 succeeded; no OOM was encountered. Batch 12 is the current throughput-oriented default because it was the fastest tested rate while retaining substantially more VRAM headroom than batch 16. Batch 16 remains a validated upper point, not an assumed hard limit.

## Embedding dimensions

The benchmark verified normalized Matryoshka truncations at 2048, 1024, 512, and 256 dimensions for both text and image vectors. This only proves the vectors can be produced and normalized; it does **not** prove equivalent retrieval quality.

Until IDX-021 measures retrieval quality with filename leakage disabled, 2048 remains the safe native representation. Smaller dimensions are evaluation candidates, not production defaults.

## Runtime availability

`app.indexing.qwen_runtime` reads the pinned runtime contract without importing Torch, Transformers, or Qwen. It reports whether the isolated interpreter, pinned Qwen source, and model snapshot are present. `require_qwen_runtime()` raises an actionable `EmbeddingBackendError` that points to `tools/setup_qwen_indexing.ps1` instead of leaking an opaque import/file error.

The configuration also derives a deterministic embedding generation ID from the pinned model revision, Qwen source commit, dtype, native dimension, and retrieval instruction. Throughput-only settings such as batch size are excluded from that identity. If a semantic-producing setting changes, a new generation ID is produced rather than silently reusing an older vector generation.

## Visual embedding persistence

IDX-018 keeps the heavyweight model isolated:

1. The normal offline indexer selects persisted visual Traces and owns SQLite writes.
2. `QwenSubprocessEmbeddingBackend` invokes `tools/qwen_embedding_worker.py` using `data/qwen_indexing/.venv/Scripts/python.exe`.
3. The worker loads Qwen in the isolated process, internally batches images using the validated batch policy, and returns vectors through a temporary response file.
4. The normal indexer L2-normalizes vectors again defensively and stores float32 bytes in `Embedding` rows.
5. Existing rows for the exact generation ID + dimension are reused. Different generations coexist instead of replacing one another.

Commands:

```powershell
python -m app.indexing qwen-status
python -m app.indexing embed-visual
python -m app.indexing status
```

`embed-visual` is intentionally an offline command, not a public GET/POST inference endpoint. This avoids exposing an unauthenticated expensive GPU operation and keeps Torch/Qwen out of FastAPI.
