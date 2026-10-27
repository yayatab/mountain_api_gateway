from __future__ import annotations

import os
from pathlib import Path

import yaml

from tests.system_test.models import SystemTestScenario

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_all_scenarios() -> list[tuple[str, SystemTestScenario]]:
    scenario_files: list[Path] = []

    # Check for specific override via environment variable
    custom_file = os.getenv("SYSTEM_TEST_YAML")
    if custom_file:
        custom_path = Path(custom_file)
        if custom_path.exists():
            scenario_files.append(custom_path)
    elif SCENARIOS_DIR.exists():
        scenario_files.extend(sorted(SCENARIOS_DIR.glob("*.yaml")))
        scenario_files.extend(sorted(SCENARIOS_DIR.glob("*.yml")))

    loaded_scenarios: list[tuple[str, SystemTestScenario]] = []
    for f in scenario_files:
        content = yaml.safe_load(f.read_text(encoding="utf-8"))
        if isinstance(content, list):
            for idx, item in enumerate(content, 1):
                sc = SystemTestScenario.model_validate(item)
                test_id = f"{f.stem}_{idx}_{sc.name.lower().replace(' ', '_')}"
                loaded_scenarios.append((test_id, sc))
        elif isinstance(content, dict) and "scenarios" in content:
            for idx, item in enumerate(content["scenarios"], 1):
                sc = SystemTestScenario.model_validate(item)
                test_id = f"{f.stem}_{idx}_{sc.name.lower().replace(' ', '_')}"
                loaded_scenarios.append((test_id, sc))
        elif isinstance(content, dict):
            sc = SystemTestScenario.model_validate(content)
            test_id = f"{f.stem}_{sc.name.lower().replace(' ', '_')}"
            loaded_scenarios.append((test_id, sc))

    return loaded_scenarios


SCENARIO_LIST = load_all_scenarios()
