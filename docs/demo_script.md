# Demo video outline (15–20 minutes)

Start the interface first: `python3 -m autograder serve --open` (inside WSL for full sanitizer support).

| time | what to show | where |
|---|---|---|
| 0:00–1:30 | Problem and contract (same submission + same rules → same grade) | README diagram |
| 1:30–4:00 | Input 1, the rules: start from the add-two-integers example, change a weight to break the sum and show the live error, fix it; point at the list of analyzers the rules will run | Grade page, step 1 |
| 4:00–7:00 | Input 2, the program: load the assignment's `add.c`, grade it, watch the analyzers tick off; walk through the circled grade, the margin notes, test 07 counterexample, suggestions | Grade page, steps 2–3, then the report |
| 7:00–8:30 | Custom rules: open your own `rules.json` with only three analyzers and grade pasted code against it | Grade page |
| 8:30–9:30 | Infinite loop: `add_infinite_loop.c` gives suspected_infinite_loop and a partial report | Grade page with that sample |
| 9:30–12:00 | Sorting: bubble sort growth chart (hover the points) and hot lines; the `return a - b` comparator underlined at line 7, counterexample shrunk to `2 / 0 -2147483648`; LSan leak | Class results → sort_integers |
| 12:00–13:30 | Copy detection: renamed copy flagged with line ranges; `probe-llm` and the LLM-copy flag | `sort_llm_copy.c` report, terminal |
| 13:30–14:30 | Reproducibility: `verify` (byte-identical reports), LLM cache, the reproducibility record in the report | terminal + report |
| 14:30–17:30 | Usefulness (4 cases) and limitations, shown live with `add_magic_value.c` (97.9) and `sort_memmove_insertion.c` (99.8) | Class results → limitations |
| 17:30–19:00 | Extending: write a plugin analyzer and add it to the rules without touching the agent | `docs/rules_reference.md` |
