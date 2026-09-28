.PHONY: addon benchmark check deb format fuzz-smoke lint messages package-smoke release test test-live test-live-greaseweazle typecheck vendor-check

PYTHON ?= python3

check: vendor-check lint typecheck test

vendor-check:
	$(PYTHON) tools/vendor_amiganut.py --check

addon:
	$(PYTHON) tools/build_addon.py --output dist

deb:
	$(PYTHON) tools/debian_package.py --output build/debian

benchmark:
	python -m amigafs.benchmark --output build/performance/amd64.json --check-budgets

format:
	python -m ruff format .

fuzz-smoke:
	mkdir -p build/fuzz/inf build/fuzz/uri build/fuzz/volume
	python fuzz/fuzz_inf.py build/fuzz/inf fuzz/corpus/inf -atheris_runs=1000 -max_len=4096
	python fuzz/fuzz_uri.py build/fuzz/uri fuzz/corpus/uri -atheris_runs=1000 -max_len=4096
	python fuzz/fuzz_volume.py build/fuzz/volume -atheris_runs=250 -max_len=16384

lint:
	python -m ruff check .
	python -m ruff format --check .

messages:
	xgettext --language=Python --from-code=UTF-8 --sort-output --no-wrap \
		--keyword=_ --keyword=N_ --keyword=ngettext:1,2 --output=po/amigafs.pot \
		src/amigafs/core/blockio.py src/amigafs/core/containers.py \
		src/amigafs/core/create.py src/amigafs/core/devices.py \
		src/amigafs/core/disc_transfer.py src/amigafs/core/formats.py \
		src/amigafs/core/image.py src/amigafs/core/media.py \
		src/amigafs/core/properties.py src/amigafs/core/repair.py \
		src/amigafs/core/transfer.py src/amigafs/core/validation.py \
		src/amigafs/desktop.py src/amigafs/file_forge.py src/amigafs/greaseweazle.py \
		src/amigafs/fuse_adapter/operations.py src/amigafs/fuse_adapter/runner.py \
		src/amigafs/mounts.py src/amigafs/operations.py src/amigafs/preferences.py \
		src/amigafs/recovery.py src/amigafs/safe_paths.py src/amigafs_nautilus/extension.py \
		src/amigafs_nautilus/logic.py

package-smoke:
	python tools/package_smoke.py --build

release:
	python tools/release_artifacts.py --output build/release

typecheck:
	python -m mypy src

test:
	python -m pytest

test-live:
	AMIGAFS_RUN_LIVE_FUSE=1 python -m pytest tests/test_live_fuse.py

test-live-greaseweazle:
	AMIGAFS_RUN_LIVE_GREASEWEAZLE=1 python -m pytest tests/test_containers.py
