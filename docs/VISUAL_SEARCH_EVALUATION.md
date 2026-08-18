# Visual search evaluation

IDX-021 evaluates whether persisted visual embeddings actually retrieve the right footage, not merely whether the retrieval plumbing works.

## Run

```powershell
python -m app.indexing evaluate-visual --output data/idx021_evaluation.json
```

The default spec is `config/visual-search-evaluation.json`.

The harness:

- embeds all configured text queries in one Qwen subprocess invocation;
- reuses the exact persisted native 2048-d visual embedding generation;
- compares 2048, 1024, 512, and 256 dimensions by first-N Matryoshka truncation followed by L2 normalization;
- never regenerates image embeddings;
- filters to the exact embedding generation;
- intentionally omits Media title, filename, and source-path metadata from the evaluation bundle;
- uses stable score-descending / Trace-ID tie ordering.

The default positive queries are three semantic phrasings of the known-present guitar footage. The default controls are candidate absent concepts/scenes/actions. Controls must be visually verified against the current corpus before treating them as valid negatives.

## Human relevance review

Ranking quality must be judged from image pixels, not from filenames, titles, paths, Media IDs, or query notes. For each query and dimension, inspect at least the top five matches and record:

- whether rank 1 is relevant;
- relevant results in top 5;
- first relevant rank;
- distinct relevant Media in top 5;
- obvious near-duplicate concentration;
- top score and score of the lowest relevant top-5 match.

For control queries, verify the concept is genuinely absent from the corpus and record the top score. Because brute-force search always returns a nearest neighbor, a control result is not automatically a semantic false positive merely because it is returned; score separation from positive/relevant matches is the useful observation until a threshold is calibrated later.

## Dimension choice

Do not select a smaller vector solely because it uses less storage. Prefer the smallest tested dimension whose visually judged ranking quality is materially indistinguishable from the native 2048-d result across the positive queries and does not materially worsen control separation. If the provisional corpus is too small or ambiguous to establish that, retain 2048 and defer reduction rather than guessing.

The output and any review artifacts belong under ignored `data/` runtime storage. They are host evidence, not repository fixtures.
