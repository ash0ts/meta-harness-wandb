# Dataset provenance and publication boundary

The showcase uses the dataset files already present in the pinned upstream example. It adds no dataset copies. Upstream's `reference_examples/text_classification/data/README.md` identifies their source as `metaevo-ai/mce-artifact/env/<directory>/data`.

| Manifest dataset | Upstream directory | Train / validation / test |
| --- | --- | --- |
| USPTO | `data/uspto` | 50 / 30 / 100 |
| Symptom2Disease | `data/symptom_diagnosis` | 200 / 50 / 212 |
| LawBench | `data/crime_prediction` | 200 / 50 / 100 |

These counts are the configured experiment slices. The LawBench name is upstream's loader alias for the vendored crime-prediction data; it is not a claim to evaluate the full LawBench benchmark. The source-tree digest includes the pinned task files, and each job bundle records its input hashes.

Upstream's software license is not evidence of independent dataset redistribution permission. This checkpoint does not certify the datasets' licensing or publication clearance. The draft keeps dataset-text publication unset and USPTO text clearance false. The exporter applies those decisions, and publication excludes raw dataset files, sidecars, solver prompts, memory and transcripts. Complete the source-specific notices and clearance decision in the research protocol before any public release of task text.
