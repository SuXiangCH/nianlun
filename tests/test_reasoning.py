import pytest

from nianlun.models.reasoning import resolve_reasoning_effort


@pytest.mark.parametrize("model_default", [None, "low", "medium", "high"])
@pytest.mark.parametrize("override", ["inherit", "default", "low", "medium", "high"])
def test_reasoning_effort_resolution(override, model_default) -> None:
    expected = (
        model_default
        if override == "inherit"
        else None
        if override == "default"
        else override
    )
    assert resolve_reasoning_effort(override, model_default) == expected


@pytest.mark.parametrize("override", [None, "none", "HIGH", "", False])
def test_invalid_reasoning_override_is_rejected(override) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        resolve_reasoning_effort(override, "low")


@pytest.mark.parametrize("model_default", ["none", "default", "inherit", False])
def test_invalid_model_default_is_rejected(model_default) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        resolve_reasoning_effort("inherit", model_default)
