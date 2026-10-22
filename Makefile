.PHONY: test e2e

test:
	python3 -m unittest discover -s tests -q

e2e:
	python3 -m trader e2e
