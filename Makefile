.PHONY: help sync test lint install uninstall build doctor replay match report clean sync-skills

help:
	@echo "make sync          install dev deps"
	@echo "make test          run pytest"
	@echo "make install       uv tool install + skill-advisor install"
	@echo "make uninstall     skill-advisor uninstall + uv tool uninstall"
	@echo "make sync-skills FROM=PATH  copy SKILL.md folders from PATH into ~/.claude/skills"
	@echo "make build         rebuild catalog + embeddings"
	@echo "make doctor        diagnose current install"
	@echo "make replay        run examples/prompts.jsonl through the pipeline"
	@echo "make match Q=...   evaluate the matcher against a single prompt"
	@echo "make report        summarize picks, dead skills, and latency from events.jsonl"
	@echo "make clean         remove build artefacts and caches"

sync:
	uv sync --extra dev

test:
	uv run pytest

install:
	uv tool install --reinstall .
	skill-advisor install

uninstall:
	-skill-advisor uninstall
	-uv tool uninstall skill-advisor

build:
	skill-advisor build

doctor:
	skill-advisor doctor

replay:
	skill-advisor replay examples/prompts.jsonl

match:
	@if [ -z "$(Q)" ]; then echo "usage: make match Q=\"your prompt\""; exit 2; fi
	skill-advisor match "$(Q)"

report:
	skill-advisor report

sync-skills:
	skill-advisor sync-skills $(if $(FROM),--from "$(FROM)") $(if $(FORCE),--force) $(if $(DRY_RUN),--dry-run)

clean:
	rm -rf build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
