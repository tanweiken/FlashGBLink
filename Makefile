PYTHON ?= python3

.PHONY: payload install-plugin test

payload:
	$(MAKE) -C payload

install-plugin:
	$(PYTHON) scripts/install_plugin.py

test:
	$(PYTHON) -m unittest discover -s tests
