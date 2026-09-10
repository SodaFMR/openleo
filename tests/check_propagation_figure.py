"""Compare the portable SVG fixture, allowing only subpixel marker roundoff."""

import argparse
from math import isclose, isfinite
from xml.etree import ElementTree


def compare_svg(reference, actual):
    expected_nodes, actual_nodes = list(reference.iter()), list(actual.iter())
    assert len(expected_nodes) == len(actual_nodes), "SVG element counts differ"
    for index, (expected, observed) in enumerate(zip(expected_nodes, actual_nodes, strict=True)):
        context = f"SVG element {index} ({expected.tag})"
        assert expected.tag == observed.tag, f"{context}: tags differ"
        assert len(expected) == len(observed), f"{context}: child counts differ"
        assert (expected.text, expected.tail) == (observed.text, observed.tail), (
            f"{context}: text differs"
        )
        assert expected.attrib.keys() == observed.attrib.keys(), f"{context}: attributes differ"
        for key, value in expected.attrib.items():
            other = observed.attrib[key]
            if expected.tag == "{http://www.w3.org/2000/svg}use" and key in ("x", "y"):
                message = f"{context}: coordinate {key}: {value!r} vs {other!r}; finite values within 0.001 SVG point required"
                try:
                    first, second = float(value), float(other)
                except ValueError as exc:
                    raise AssertionError(message) from exc
                assert isfinite(first) and isfinite(second), message
                assert isclose(first, second, rel_tol=0.0, abs_tol=0.001), message
            else:
                assert value == other, f"{context}: attribute {key} differs"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    parser.add_argument("actual")
    args = parser.parse_args()
    compare_svg(
        ElementTree.parse(args.reference).getroot(), ElementTree.parse(args.actual).getroot()
    )
    print("SVG matches: exact structure/text/attributes; marker coordinates within 0.001 point")
