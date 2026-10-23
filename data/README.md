# LogRefine dataset

`train.jsonl` contains the 3,000 instruction examples used in the paper.
Each of the five tasks contributes 600 examples.

| Task code | Task |
|---|---|
| Parsing | Log parsing |
| AD | Anomaly detection |
| Interpretation | Log interpretation |
| RCA | Root cause analysis |
| SR | Solution recommendation |

## Format

Each line is a JSON object with the following fields:

| Field | Meaning |
|---|---|
| `instruction` | The requested analysis task |
| `input` | Log input and relevant context |
| `output` | The response used for training |
| `reasoning` | Optional explanation |
| `sample_id` | Identifier of the constructed example |
| `task` | One of the five task codes above |
| `parent_seed_id` | Identifier linking the example to its source seed |

The data can be loaded with any JSONL reader. Use `instruction`, `input`, and
`output` as the supervised training fields.

## Sources

The examples were constructed from training seeds in the
[R-Log corpus](https://github.com/lunyiliu/R-Log), which builds on the
[LogLM benchmark](https://github.com/lunyiliu/LogLM). Source links and pinned
revisions are listed in [`NOTICE.md`](../NOTICE.md). The `parent_seed_id`
field retains the link to the corresponding training seed.

Dataset SHA256:
`67a59da3b63f37701960fe3c8e732bda94dc245879a0a4ba60d71d403a1c3e28`.

See [`NOTICE.md`](../NOTICE.md) for source attribution and dataset license information.
