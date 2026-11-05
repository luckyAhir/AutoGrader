# AutoGrader - one-command setup for a fresh Linux machine (no Docker needed).
#
#   make setup      install everything (system tools, Python env, Ollama, the model) and verify it
#   make run        start the browser interface on http://localhost:8000
#   make test       run the test-suite
#   make check      show which tools are available (autograder doctor)
#
# Needs: sudo (or root), internet access, apt-get / dnf / yum / pacman / zypper.
# The "sandbox" is not a separate download: it is a small C launcher compiled by gcc at run time.
# The Ollama model comes from OLLAMA_MODEL in .env (default qwen2.5-coder:3b).

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON   ?= python3
VENV     ?= .venv
PORT     ?= 8000
SUDO     := $(shell [ "$$(id -u)" = "0" ] && echo "" || echo sudo)
MODEL_ENV := $(shell grep -s '^OLLAMA_MODEL=' .env | head -n1 | cut -d= -f2 | cut -d' ' -f1 | tr -d '"')
MODEL    := $(if $(MODEL_ENV),$(MODEL_ENV),qwen2.5-coder:3b)
PKGMGR   := $(shell for m in apt-get dnf yum pacman zypper; do command -v $$m >/dev/null 2>&1 && { echo $$m; break; }; done)
VPY      := $(VENV)/bin/python

.PHONY: help setup system-deps python-env ollama model ollama-up check test run clean clean-data distclean \
	docker-build docker-run docker-check

help:
	@echo "make setup    - install system tools, Python environment, Ollama and the model ($(MODEL))"
	@echo "make run      - start the browser interface (PORT=$(PORT))"
	@echo "make test     - run the tests"
	@echo "make check    - show tool availability"
	@echo "make model    - (re)download the model named in .env"
	@echo "make clean    - remove Python caches;  make clean-data - also the LLM cache, uploads, problems/"
	@echo "make docker-build / docker-run / docker-check - the same grader in Docker (Ollama stays on the host)"
	@echo "Individual steps: system-deps python-env ollama model"

setup: system-deps python-env ollama model check
	@echo ""
	@echo "Setup finished. Start the interface with:  make run"

# --- 1. compiler, gcov, cppcheck, valgrind, python, curl --------------------------------------
system-deps:
	@echo ">> installing system packages with $(PKGMGR)"
ifeq ($(PKGMGR),apt-get)
	$(SUDO) apt-get update
	$(SUDO) apt-get install -y --no-install-recommends build-essential gcc libc6-dev cppcheck valgrind \
		python3 python3-venv python3-pip curl ca-certificates zstd
else ifeq ($(PKGMGR),dnf)
	$(SUDO) dnf install -y gcc glibc-devel libasan libubsan liblsan cppcheck valgrind python3 python3-pip curl zstd make
else ifeq ($(PKGMGR),yum)
	$(SUDO) yum install -y gcc glibc-devel libasan libubsan cppcheck valgrind python3 python3-pip curl zstd make
else ifeq ($(PKGMGR),pacman)
	$(SUDO) pacman -Sy --noconfirm --needed base-devel gcc cppcheck valgrind python python-pip curl zstd
else ifeq ($(PKGMGR),zypper)
	$(SUDO) zypper --non-interactive install gcc glibc-devel libasan8 cppcheck valgrind python3 python3-pip curl zstd make
else
	@echo "No supported package manager found. Install by hand: gcc, cppcheck, valgrind, python3 (>=3.9), curl."
	@exit 1
endif
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9 or newer is required"'

# --- 2. Python environment (the project itself needs no third-party packages) -------------------
python-env:
	@echo ">> creating $(VENV) and installing autograder"
	$(PYTHON) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -e . pyyaml

# --- 3. Ollama (skipped when already installed) --------------------------------------------------
ollama:
	@if command -v ollama >/dev/null 2>&1; then echo ">> ollama already installed: $$(ollama --version 2>&1 | head -n1)"; \
	else echo ">> installing Ollama"; curl -fsSL https://ollama.com/install.sh | sh; fi
	@$(MAKE) --no-print-directory ollama-up

# Make sure the Ollama server is running (systemd service if present, else a background process).
ollama-up:
	@if curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then echo ">> ollama server is running"; \
	else \
	  echo ">> starting the ollama server"; \
	  ( command -v systemctl >/dev/null 2>&1 && $(SUDO) systemctl start ollama 2>/dev/null ) || \
	    ( nohup ollama serve >/tmp/ollama.log 2>&1 & ); \
	  for i in $$(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done; \
	  curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && echo ">> ollama server is running" \
	    || { echo "!! could not start ollama; see /tmp/ollama.log"; exit 1; }; \
	fi

# --- 4. the model named in .env --------------------------------------------------------------------
model: ollama-up
	@echo ">> pulling model $(MODEL)  (set OLLAMA_MODEL in .env to change it)"
	ollama pull $(MODEL)

# --- verify / use ------------------------------------------------------------------------------------
check:
	@$(VPY) -m autograder doctor --model $(MODEL) || true
	@echo "model in .env: $(MODEL)"
	@ollama list 2>/dev/null || true

test:
	$(VPY) -m unittest discover -t . -s tests

run: ollama-up
	$(VPY) -m autograder serve --port $(PORT)

# --- Docker (optional; needs Docker on the machine and Ollama running on the HOST: make ollama model) ----
# --network host lets the container reach the host's Ollama at 127.0.0.1:11434 (Linux). OLLAMA_MODEL and
# OLLAMA_HOST come from .env. Files (problems/, reports/, submissions/, the LLM cache) stay in this folder.
IMAGE ?= autograder
DOCKER_RUN = docker run --rm --network host --user $$(id -u):$$(id -g) --env-file .env -v "$$PWD":/work $(IMAGE)

docker-build:
	docker build -t $(IMAGE) .

docker-run: docker-build ollama-up
	@echo ">> AutoGrader in Docker: http://localhost:$(PORT)"
	$(DOCKER_RUN) serve --host 127.0.0.1 --port $(PORT)

docker-check: docker-build
	$(DOCKER_RUN) doctor --model $(MODEL)

clean:
	find . -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf *.egg-info build

# Removes the LLM cache (it makes LLM-based scores reproducible), stored uploads and the working problems/ folder.
clean-data:
	rm -rf .autograder_cache reports/uploads problems

distclean: clean
	rm -rf $(VENV)
