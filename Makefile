PYTHON ?= python3

.PHONY: payload install-plugin plugin-zip test

payload:
	$(MAKE) -C payload

install-plugin:
	$(PYTHON) scripts/install_plugin.py

plugin-zip:
	$(PYTHON) scripts/build_plugin_zip.py

test:
	$(PYTHON) -m unittest discover -s tests
