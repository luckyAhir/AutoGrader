"""Instructor tool: regenerate the hidden tests for sort_integers (deterministic)."""

import random
from pathlib import Path

INT_MIN, INT_MAX = -2 ** 31, 2 ** 31 - 1
HERE = Path(__file__).resolve().parent / "tests"


def write(name, values):
    HERE.mkdir(exist_ok=True)
    (HERE / f"{name}.in").write_bytes(f"{len(values)}\n{' '.join(map(str, values))}\n".encode())
    (HERE / f"{name}.out").write_bytes((" ".join(map(str, sorted(values))) + "\n").encode())


def main():
    rng = random.Random(2026)
    write("01", [3, 1, 4, 1, 5])                                   # the sample
    write("02", [42])                                              # single element
    write("03", [])                                                # empty input
    write("04", [-5, 3, -1, 0, -10, 7])                            # negatives
    write("05", [2, 2, 1, 1, 3, 3, 2, 1])                          # duplicates
    write("06", list(range(-4, 6)))                                # already sorted
    write("07", list(range(10, 0, -1)))                            # reverse sorted
    write("08", [INT_MAX, INT_MIN, 0, -1, 1, INT_MAX])             # extreme values
    write("09", [rng.randint(-10 ** 6, 10 ** 6) for _ in range(1000)])
    write("10", [rng.randint(INT_MIN, INT_MAX) for _ in range(100000)])  # needs O(n log n)


if __name__ == "__main__":
    main()
