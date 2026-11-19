# CodeProof verification environment.
# Main comparison environment: Linux / CPython 3.12 (python:3.12-slim).
# Dependencies are pre-installed so candidate runs reuse the same snapshot
# offline (no network at run time). Pinned base digest recorded in evidence.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

RUN pip install --no-cache-dir coverage ruff flit_core

WORKDIR /work
# Non-root execution; candidate code never gets host mounts or network.
RUN useradd -m verifier
USER verifier