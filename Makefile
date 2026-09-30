.PHONY: setup k8s-apply run test demo clean

SHELL := /bin/bash
PYTHON ?= python3

setup:
	$(PYTHON) -m pip install -r requirements.txt
	docker image inspect python:3.12-slim >/dev/null 2>&1 || docker pull python:3.12-slim

k8s-apply:
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/networkpolicy.yaml

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	set -o pipefail; $(PYTHON) -m pytest tests/ -v 2>&1 | tee artifacts/test_output.txt

demo:
	bash scripts/demo.sh

clean:
	rm -f code_exec.db code_exec.db-wal code_exec.db-shm
	kubectl delete namespace sandbox --ignore-not-found
