# agentic01 — AutoGrader: implementation plan, work distribution and progress

*Group: [Member 1], [Member 2], [Member 3], [Member 4]. Export this file to `agentic01.pdf`.*

## 1. Plan for the implementation

AutoGrader is a coordinator agent with one expert analyzer per metric. It is written in Python (standard
library only) and uses gcc, gcov, ASan/UBSan, cppcheck and a local LLM (Ollama).

| component | approach |
|---|---|
| Sandbox | Linux: a C launcher applies `setrlimit` (CPU, memory, file size) and measures the student process; Windows: Job Objects. TLE is judged on CPU time so verdicts are reproducible. |
| Rules | JSON file per problem (metrics, analyzers, weights summing to 1.00, thresholds, limits, tests, reference solution, input specification). |
| Coordinator | first pass (compile → static sanity → smoke run; infinite loop → partial report), parallel dispatch, weighted collation, JSON + text reports. |
| Analyzers | hidden I/O tests; compiler warnings; static structure (own tokenizer: McCabe complexity, function length, nesting); modularity; generated edge cases checked against the reference, with counterexample minimization; complexity from gcov operation counts; ASan/UBSan/LSan; LLM readability review (temperature 0, seed, cache) blended with style metrics; MOSS-style similarity against peers and LLM-generated solutions. |
| Reproducibility | seeded generators, operation counts instead of timings, LLM response cache, hashes of rules/assets/tool versions in each report, `verify` command. |

## 2. Distribution of work

| member | responsibility |
|---|---|
| [Member 1] | sandbox (POSIX launcher, Windows Job Objects), coordinator, first pass, report writer, CLI |
| [Member 2] | input generator, reference oracle, edge-case runner, counterexample minimization and explanations |
| [Member 3] | static analysis (tokenizer, metrics), modularity, complexity estimator (gcov fitting), sanitizer analyzer |
| [Member 4] | LLM review and cache, similarity / LLM-copy detection, sample problems and submissions, final report and video |

Everyone writes unit tests for their own modules and reviews one other member's pull requests.

## 3. Progress so far

* All 10 analyzers, the coordinator, the sandbox (Linux + Windows) and the CLI work end to end.
* Two rule sets (`add_two_integers` from the assignment and `sort_integers`) with reference solutions,
  10 hidden tests each and 11 sample submissions. The agent's reports are in `reports/`.
* The assignment's `add.c` is graded 77.7/100, with the overflow counterexample `2147483647 1 →
  2147483648`, the UBSan finding at `add.c:5` and the `-Wunused-result` warning.
* Reproducibility checked: three runs give byte-identical JSON reports.
* A browser interface: give the agent the rules (rules.json) and a C program, watch the analyzers run, and read the marked report.
* 45 automated tests pass on Linux and Windows.

## 4. Timeline

| week | goal | owner(s) |
|---|---|---|
| 1 | sandbox, rules loader, first pass, io_tests, compiler warnings; this report | M1, M3 |
| 2 | edge cases + minimization, complexity estimator, sanitizers, static analysis | M2, M3 |
| 3 | LLM review + cache, similarity / LLM-copy detection, reports, reproducibility checks | M4, M1 |
| 4 | more problems and submissions, limitation experiments, final report, 15–20 min video, GitHub release | all |
