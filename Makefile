.PHONY: setup k8s-apply run test demo clean

setup:
	pip install -r requirements.txt

k8s-apply:
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/networkpolicy.yaml

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	pytest tests/ -v 2>&1 | tee artifacts/test_output.txt

demo:
	bash scripts/demo.sh

clean:
	rm -f code_exec.db
	kubectl delete namespace sandbox --ignore-not-found
