# Source attribution and licensing

LogRefine includes a four-agent data-construction implementation, role prompts,
and a dataset of log-analysis instructions. Code and prompt licensing is
described in [`LICENSE`](LICENSE). Dataset content retains the terms of its
underlying sources and is not covered by the code license.

## Referenced resources

| Resource | Use | Revision | License information at the revision |
|---|---|---|---|
| [R-Log](https://github.com/lunyiliu/R-Log) | Training seed corpus | `51d58fa60b39f38bcd8d497a6ddaa899c151127c` | No license file specified |
| [LogLM](https://github.com/lunyiliu/LogLM) | Benchmark and corpus lineage | `6b563becae259cc7bc1fd3dfaccfe517ae2ca2b1` | Apache-2.0 |
| [RationAnomaly](https://github.com/Gravityless/RationAnomaly) | Referenced anomaly-label resource | `ca217708f6d85ba0f7e4574cb9776dc3d2d483f6` | No license file specified |

The dataset contains instruction/input/response examples constructed from
training seeds. Each `parent_seed_id` identifies its source seed. The upstream
resources and their authors retain their respective rights. Consult the source
repositories for any additional data terms.
