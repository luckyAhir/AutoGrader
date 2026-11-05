# AutoGrader

AutoGrader grades C programs automatically. You give it a **rules file** (the problem, a few sample
tests and the metrics to measure) and a student's **C file**. It runs the program safely and returns a
**grade out of 100** with the reasons: the smallest failing inputs, explanations and suggestions.

The same program and rules always give the same grade.

## What it checks

| Metric | What is measured |
|---|---|
| Correctness | Tests written by a local AI model from the problem statement and samples |
| Edge cases | Extreme inputs (smallest, largest, zero, negative) written the same way |
| Syntax | Compiler warnings |
| Structure | Function size, nesting, comments, risky constructs |
| Modularity | Helper functions that are actually used |
| Complexity | Growth of work as the input grows (compared with the required bound) |
| Memory safety | Memory errors, leaks, undefined behaviour |
| Readability | AI review plus style measurements |
| Originality | Copying between students, with who copied from whom |

No reference solution is needed. The sample tests in the rules are the ground truth.

## Install

You need **Python 3.9+** and a **C compiler** (gcc or clang). There are no Python packages to install.

Optional: `cppcheck`, `valgrind`, and [Ollama](https://ollama.com) for the AI parts.

```bash
ollama pull qwen2.5-coder:3b        # the AI model used for tests and review
python3 -m autograder doctor        # shows which tools were found
```

The model is set in the `.env` file (`OLLAMA_MODEL=qwen2.5-coder:3b`). Change that one line to use another
model everywhere, for example `qwen2.5-coder:7b`.

## Use it in the browser

```bash
python3 -m autograder serve --open        # opens http://localhost:8000
```

1. Paste the **rules.json** (or open the file). The page checks it and lists the analyzers.
2. Paste the student's **C program** and give it a file name (use the student's name or roll number).
3. Click **Grade program**.

The report shows the grade, the marked program, the score of every metric, failing cases with
explanations and ranked suggestions. **Class results** shows every graded program in one table.

## Use it from the command line

```bash
# grade one program, or a whole folder
python3 -m autograder grade student.c --rules rules.json
python3 -m autograder grade submissions/ --rules rules.json

# check that grading is reproducible
python3 -m autograder verify student.c --rules rules.json

# find who copied from whom in a folder of programs
python3 -m autograder originality submissions/problem_name
```

Reports are written to `reports/<problem>/` (a `.json` for programs, a `.txt` for students).

## A minimal rules file

```json
{
  "problem": "Add two integers.\nInput: two integers a and b.\nOutput: their sum.",
  "sample": [{"input": "7 5\n", "output": "12\n"}],
  "metrics": [
    {"name": "correctness", "analyzer": "llm_tests",       "weight": 0.6},
    {"name": "logical",     "analyzer": "edge_case_runner", "weight": 0.4}
  ]
}
```

The `problem` field holds the problem statement. Metric weights must add up to 1.
All options and every analyzer's settings are in [docs/rules_reference.md](docs/rules_reference.md).

## Project layout

```
autograder/     the program (coordinator, sandbox, analyzers, web interface)
tests/          automated tests
docs/           rules reference and demo notes
problems/       created while grading (the current problem)
submissions/    graded programs, kept per problem for the originality check
reports/        saved reports
```

## Run the tests

```bash
python3 -m unittest discover -s tests -t .
```

## Good to know

- The web server runs student code, so it listens on this computer only.
- A flag such as "possible copying" is a hint for the instructor, not a verdict.
- Tests written by an AI model can still contain mistakes, so review the generated test files
  (`test_cases.json`, `edge_case.json`) before relying on a grade.
