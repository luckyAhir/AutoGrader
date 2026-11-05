# Hardened grading image: gcc (with gcov, ASan/UBSan/LSan runtimes), cppcheck, valgrind.
#   docker build -t autograder .
#   docker run --rm --network none --cap-add SYS_PTRACE -v "$PWD":/work autograder \
#       grade submissions/add_two_integers --rules tests/fixtures/add_two_integers/rules.json
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev cppcheck valgrind \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/autograder
COPY pyproject.toml README.md ./
COPY autograder ./autograder
RUN pip install --no-cache-dir .

RUN useradd --create-home grader
USER grader
WORKDIR /work
ENTRYPOINT ["autograder"]
CMD ["--help"]
