#!/usr/bin/env bash
# Regenerate every sample report (run from the repository root on Linux/WSL).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m autograder grade submissions/add_two_integers --rules tests/fixtures/add_two_integers/rules.json --quiet
python3 -m autograder grade submissions/sort_integers   --rules tests/fixtures/sort_integers/rules.json   --quiet
python3 -m autograder grade docs/limitation_cases/add_magic_value.c \
    --rules tests/fixtures/add_two_integers/rules.json --out reports/limitations --quiet
python3 -m autograder grade docs/limitation_cases/sort_memmove_insertion.c \
    --rules tests/fixtures/sort_integers/rules.json --out reports/limitations --quiet

cat reports/add_two_integers/summary.md reports/sort_integers/summary.md
python3 -m autograder verify submissions/add_two_integers/add.c --rules tests/fixtures/add_two_integers/rules.json --runs 3
