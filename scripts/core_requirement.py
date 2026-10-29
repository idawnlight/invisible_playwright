"""Print the GitHub requirement for this checkout's exact core dependency."""
from pathlib import Path
import re
import tomllib


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*)$", re.DOTALL)
_PIN_RE = re.compile(r"^==([^\s,;()\[\]]+)$")


def normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_core_pin(value: str):
    match = _REQ_RE.match(value)
    if match is None or normalise_name(match.group(1)) != "invisible-core":
        return None
    specifier, _, marker = match.group(3).partition(";")
    specifier = re.sub(r"\s+", "", specifier)
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1]
    pin = _PIN_RE.fullmatch(specifier)
    if pin is None:
        return None
    extras = tuple(sorted(normalise_name(item.strip()) for item in (match.group(2) or "").split(",") if item.strip()))
    return {"version": pin.group(1), "extras": extras, "marker": " ".join(marker.split())}


def requirement() -> str:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [pin for value in data["project"]["dependencies"] if (pin := parse_core_pin(value))]
    if len(pins) != 1:
        raise ValueError("exactly one invisible-core version is required")
    pin = pins[0]
    extras = "[" + ",".join(pin["extras"]) + "]" if pin["extras"] else ""
    marker = " ; " + pin["marker"] if pin["marker"] else ""
    return (f"invisible-core{extras} @ git+https://github.com/idawnlight/invisible_core.git"
            f"@v{pin['version']}{marker}")


if __name__ == "__main__":
    print(requirement())
