# Rules reference

A rules file is JSON (or YAML if PyYAML is installed) and is kept next to the problem's assets. All
relative paths are resolved against the directory that contains the rules file. Run
`python -m autograder validate-rules <file>` after every edit. It checks the schema and the weights and
generates the test cases if there are none.

## Top-level keys

| key | default | meaning |
|---|---|---|
| `problem` | *required* | problem identifier (also the default report folder) |
| `statement` / `statement_file` | – | problem text; given to the LLM reviewer and to `probe-llm` |
| `time_limit_s` | 1 | **CPU-time** limit per run; the wall-clock safety net is max(3×limit, limit+2 s) |
| `memory_limit_mb` | 64 | address-space limit (Linux) / committed-memory limit (Windows) |
| `output_limit_kb` | 8192 | larger output ⇒ OLE (the program is killed) |
| `compile_flags` | `-O2 -Wall -Wextra -std=c11` | flags for the main build; `-std=` is reused for every other build |
| `link_flags` | `-lm` | |
| `reference_solution` | – | optional (command line only; the browser ignores it): an instructor's C solution used as the oracle for generated edge cases and the complexity comparison. Not needed to generate tests |
| `sample` | – | `{"input": ..., "output": ...}` or a list of them; the first is the first-pass smoke run, all of them become the first tests and are shown to the model as worked examples (`io_tests` / `llm_tests`); at least one needs an `output` |
| `compare` | `{"mode": "lines"}` | `exact`, `lines` (ignore trailing spaces/blank lines), `tokens`, `float` (+ `abs_eps`, `rel_eps`) |
| `nonzero_exit_is_error` | true | treat `return 1` from main as a runtime error |
| `input_spec` | – | declarative input format (below); enables edge cases, minimization and complexity |
| `generator` | – | Python file with `generate(category, rng, limit) -> [str]` and optional `for_size(n, rng) -> str`, for formats `input_spec` cannot express |
| `seed` | derived | override the seed of generated inputs (default: hash of problem + input_spec) |
| `plugins` | [] | Python files that register extra analyzers |
| `feedback.max_counterexamples` | 8 | how many counterexamples the report shows |
| `usefulness_limitations_file` | built-in | replace the usefulness/limitations section |
| `metrics` | *required* | list of `{name, analyzer, weight, params}`; **weights must sum to 1.00** |

## `input_spec`

```json
"input_spec": {
  "variables": [
    {"name": "n", "type": "int", "min": 0, "max": 100000, "role": "size"},
    {"name": "values", "type": "int[]", "length": "n", "min": -2147483648, "max": 2147483647}
  ],
  "layout": [["n"], ["values"]]
}
```

* types (what the input holds, and how it is read back):

  | type | purpose | example | notes |
  |---|---|---|---|
  | `int` | integer | `25` | `min`, `max` |
  | `float` | decimal number | `3.14` | `min`, `max` (default ±1e6) |
  | `str` | a word without spaces | `hello` | `min`/`max` length (or `min_length`/`max_length`), `charset`; `length` fixes it to a number or to an earlier variable |
  | `char` | one character | `A` | `charset` (default: letters) |
  | `char[]` | a text of characters, same as `str` | `hello` | as `str` |
  | `int[]` | list of integers | `1 2 3 4` | `length`; `sorted`, `distinct` |
  | `float[]` | list of decimals | `1.2 3.4 5.6` | `length` |
  | `int[][]` | integer matrix, one row per line | `1 2` / `3 4` | `rows`, `cols` |
  | `float[][]` | decimal matrix | `1.5 2.5` / `3.5 4.5` | `rows`, `cols` |
  | `char[][]` | grid of characters, one row per line, no spaces | `#.#` / `..#` | `rows`, `cols`, `charset` |

  Other spellings are accepted: `integer`, `long`, `double`, `string`, `character`, `array`, `list`,
  `matrix`, `grid`. `length`, `rows`, `cols` are numbers or the names of variables declared **earlier**.
  `min`/`max` bound the values. `input_spec` is used to check the shape of generated tests and to build inputs of a given size for
  `complexity_estimator`.
* an `input_spec` that cannot be used (unknown type, missing `rows`, ...) **never stops grading**: it is
  ignored with a warning and a report flag. Tests are still generated from the statement and the samples;
  only `complexity_estimator` (which needs generated inputs) is skipped, and their
  weight is spread over the other metrics. For formats the spec cannot express, use a `generator` file (see the top-level keys).
* `role: "size"` marks *n* (also inferred from an array's `length`)
* `layout`: one list per input line; arrays are space-separated

Edge-case categories: `zero`, `negatives`, `int_overflow`, `boundaries`, `empty`, `single`, `max_size`,
`duplicates`, `sorted`, `reverse_sorted`, `whitespace` (tabs, several spaces, one value per line, CRLF, no
trailing newline, leading blank line), `random`, `large_values`. A category that does not apply to the
input format (e.g. `negatives` when `min` ≥ 0) is listed as *not applicable* and is not scored.

## Analyzers and parameters

| analyzer | kind | parameters (defaults) | score |
|---|---|---|---|
| `io_tests` | execution | `testset_dir` (`tests/`): `NN.in` + `NN.out`/`.ans`; if the folder has no tests they are generated (see below). `max_reported` (5) | passed / total |
| `llm_tests` | execution | `testset_dir` (`test_cases.json`), `provider`, `model` (overridden by `OLLAMA_MODEL` in `.env`), `temperature` (0), `seed` (42), `num_tests` (8), `max_attempts` (3), `max_input_chars` (200), `max_reported` (5) | passed / total on LLM-designed tests (see below) |
| `compiler_warnings` | source | `flags` (`-Wall -Wextra -std=c11`; `-Werror` is noted but stripped so warnings can be counted), `optimize` (`-O2`), `penalty_per_warning` (0.1) | 1 − 0.1·warnings; 0 if it does not compile |
| `static_analysis` | source | `max_cyclomatic` (10), `max_function_length` (50 NLOC), `max_nesting` (4), `cppcheck` (`auto`) | mean of: complexity / length / nesting / header-comment shares, hygiene (unsafe calls, unchecked scanf, globals, goto, cppcheck findings) |
| `function_split` | source | `min_functions` (2), `expect_helpers` (true), `max_main_share` (0.6), `helper_hint` | 0.4·count + 0.3·helper used + 0.3·main share − 0.1·dead function |
| `edge_case_runner` | execution | `edge_file` (`edge_case.json`), `cases` (hints for the model, e.g. `["zero", "negatives"]`), `num_edge_cases` (5), `max_reported` (3), plus the model settings of `llm_tests` | passed / total on LLM-written edge cases (see below) |
| `complexity_estimator` | execution | `required` (`O(1)`, `O(log n)`, `O(n)`, `O(n log n)`, `O(n^2)`, `O(n^3)`, `O(2^n)`), `sizes`, `penalty_per_class` (0.5), `time_limit_s` (10×limit), `compare_reference` (true) | 1 if the measured class ≤ required, else 1 − 0.5 per class |
| `sanitizer_run` | execution | `flags` (`-fsanitize=address,undefined`), `backend` (`auto`/`asan`/`valgrind`/`ubsan-trap`), `inputs` (`["sample","tests"]`, may add `"edge_cases"`), `edge_categories`, `max_runs` (20), `max_input_bytes` (200000), `penalties` (`memory_error` 0.5, `leak` 0.25, `undefined_behavior` 0.2) | 1 − Σ penalties of unique findings |
| `llm_review` | source | `provider` (`ollama`/`groq`/`openai`), `model`, `temperature` (0), `seed` (42), `mode` (`hybrid`/`llm`/`heuristic`), `llm_weight` (0.5), `host`, `timeout_s` | hybrid: 0.5·LLM rubric + 0.5·style metrics |
| `similarity_flag` | source | `corpus_dir` (default: `submissions/<problem>/` when the grader keeps submissions), `known_sources_dir`, `flag_threshold` (0.85), `k` (5), `window` (4), `min_shared` (8), `min_tokens_identical` (50), `common_fraction` (0.5), `ignore_reference` (true), `flagged_score` (0), `llm_style_threshold` (0.6) | 1, or `flagged_score` when flagged |
| `forbidden_constructs` | source | `forbidden_calls`, `forbidden_headers`, `forbidden_keywords`, `required_calls`, `penalty_per_violation` (0.5) | 1 − 0.5·violations |

*Execution* analyzers are skipped (score 0) when the submission does not compile or the smoke run
times out. A metric that cannot be evaluated because of the **environment** (for example no sanitizer
backend, or no tests found) is flagged and its weight is spread over the remaining metrics.

## Tests generated at runtime (`llm_tests`)

Instead of a hand-written `tests/` folder, the correctness metric can let a local LLM build the test set
from the statement and the samples:

There is **no reference solution**. The statement is the `problem` field of `rules.json` (a short name such as
`prime_number` is treated as a name, and the statement is then read from `statement`/`statement_file`), the
samples come from `sample`. The model (a ~3B model is enough: the prompt is short and the reply is capped)
returns a few short, general test cases, always in this shape:

```json
[{"name": "positive_numbers", "input": "5 7\n", "expected": "12\n"}]
```

* The samples are the first tests (`sample_1`, ...; their outputs come from your rules).
* **Expected outputs:** a small model is poor at computing outputs in its head, so it also writes a short C
  solution. If that program reproduces every sample output it computes the expected output of every model case
  (a case it cannot run is dropped); otherwise the model's own expected outputs are kept.
* A model case is dropped when it is malformed, empty, a duplicate, longer than `max_input_chars`, or breaks
  `input_spec` (the spec is used only when it describes the samples). If none survives, only the samples are
  graded and the report carries a `samples_only_tests` flag.

The result is written next to the rules as `test_cases.json` and is reused by every later run, so the grade
stays reproducible and the file is part of `assets_hash`. Delete it to regenerate. **Review the generated
tests before grading for real**: a small model can write a wrong expected output, so write samples that
include the tricky cases.

### Edge cases

`edge_case_runner` works the same way, with its own file. The model writes a few simple edge cases (smallest
and largest allowed values, zero, negatives, a single element ...) as `[{"name", "input", "expected"}]` into
`edge_case.json` next to the rules, with the same checks and the same solution-based expected outputs as above
(the samples are not part of this file). `cases` in the metric's params is only a hint to the model. The file
is reused by every later run; delete it to regenerate. If the model gives no usable edge case the metric is
skipped and its weight redistributed. `input_spec` is no longer needed for edge cases (only for `complexity_estimator`).

## Originality corpus: who copied from whom

The browser interface keeps every graded program in `submissions/<problem>/<file name>.c`, after grading it, so a
program is compared only with the ones graded before. When the rules give no `corpus_dir`, that folder is the
corpus for `similarity_flag`. The **file name identifies the student**: grading the same name again replaces the
stored copy and is never compared with itself, so use a different file name per student (e.g. the roll number).

A matching program is *earlier* or *later* than the one being graded (file modification time, 1 s tolerance);
the later one is named as the likely copy in the flag and in the feedback. On the command line, add
`--submissions-dir DIR` to `grade` to keep programs the same way, and run
`python -m autograder originality submissions/<problem> [--rules rules.json]` to list every flagged pair in the
folder with its direction. Without these options nothing changes.

## Writing a plugin analyzer

```python
# problems/my_problem/line_count.py
from autograder.analyzers.base import Analyzer, AnalyzerResult, register

@register
class LineCount(Analyzer):
    name = "line_count"
    description = "Penalise very long programs"

    def run(self, ctx, params):
        lines = len(ctx.csource.code_lines())
        limit = int(params.get("max_lines", 80))
        score = 1.0 if lines <= limit else max(0.0, 1 - (lines - limit) / limit)
        return AnalyzerResult(score, f"{lines} code lines", f"{lines} lines of code (limit {limit}).",
                              {"lines": lines, "limit": limit})
```

```json
"plugins": ["line_count.py"],
"metrics": [ ..., {"name": "brevity", "analyzer": "line_count", "weight": 0.05, "params": {"max_lines": 60}} ]
```

The context `ctx` provides `source_text`, `csource` (tokens, functions, comments, declarations),
`rules`, `limits`, `run(input)`, `judge(input, expected)`, `build(tag, flags)`, `testset(dir)`,
`oracle` (reference outputs; `None` unless the rules name a `reference_solution`) and `inputgen` (generated inputs).

An analyzer may also define `prepare(self, agent, params)`, which runs once before any submission is
graded (and before `assets_hash` is computed). Its return value reaches `run` as `params["_prepared"]`;
`io_tests` / `llm_tests` use it to generate their tests.
