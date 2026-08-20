.DEFAULT_GOAL := report

IMAGE := lake-level-analysis
DATA_DIR := $(CURDIR)/data
OUTPUT_DIR := $(CURDIR)/output

.PHONY: build fetch report clean

build:
	docker build --tag $(IMAGE) .

fetch: build
	docker run --rm \
		-v "$(DATA_DIR):/app/data" \
		-v "$(OUTPUT_DIR):/app/output" \
		$(IMAGE) --refresh --fetch-only

report: build
	docker run --rm \
		-v "$(DATA_DIR):/app/data" \
		-v "$(OUTPUT_DIR):/app/output" \
		$(IMAGE)

clean:
	rm -rf "$(DATA_DIR)" "$(OUTPUT_DIR)"
