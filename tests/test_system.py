from __future__ import annotations

import pytest

from tests.system_test.models import SystemTestScenario
from tests.system_test.runner import SystemTestRunner
from tests.system_test.scenario_loader import SCENARIO_LIST


@pytest.mark.system_test
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_id", "scenario"),
    SCENARIO_LIST,
    ids=[s[0] for s in SCENARIO_LIST],
)
async def test_system_scenario(scenario_id: str, scenario: SystemTestScenario) -> None:
    """Executes a system test scenario defined in a YAML file."""
    runner = SystemTestRunner(scenario)
    await runner.run()
