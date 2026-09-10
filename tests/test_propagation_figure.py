"""The portable figure check permits only bounded SVG marker roundoff."""

from xml.etree import ElementTree

import pytest

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><g><use x="0" y="2" fill="blue"/></g><text>Loss (dB)</text></svg>'


def _compare(actual, reference=SVG):
    from check_propagation_figure import compare_svg

    compare_svg(ElementTree.fromstring(reference), ElementTree.fromstring(actual))


@pytest.mark.parametrize("coordinate", ["0", "0.00004", "0.001", "-0.001"])
def test_marker_coordinate_roundoff_within_one_thousandth_point_is_allowed(coordinate):
    _compare(SVG.replace('x="0"', f'x="{coordinate}"'))


@pytest.mark.parametrize("coordinate", ["0.00101", "-0.002", "nan", "inf", "-inf", "bad"])
def test_marker_coordinate_outside_tolerance_or_nonfinite_is_rejected(coordinate):
    with pytest.raises(AssertionError, match="coordinate x"):
        _compare(SVG.replace('x="0"', f'x="{coordinate}"'))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('fill="blue"', 'fill="red"'),
        ("Loss (dB)", "Changed label"),
        ("<use ", "<path "),
        (' x="0"', ""),
        (' fill="blue"', ' fill="blue" opacity="0.5"'),
        ('<use x="0" y="2" fill="blue"/>', ""),
        ("</g><text>", "<text>"),
    ],
)
def test_noncoordinate_figure_changes_are_rejected(old, new):
    actual = SVG.replace(old, new)
    if old == "</g><text>":
        actual = actual.replace("</text>", "</text></g>")
    with pytest.raises(AssertionError):
        _compare(actual)


def test_nonmarker_coordinates_remain_exact():
    reference = SVG.replace("<text>", '<text x="0">')
    with pytest.raises(AssertionError):
        _compare(reference.replace('<text x="0">', '<text x="0.00004">'), reference)


def test_even_matching_nonfinite_marker_coordinates_are_rejected():
    invalid = SVG.replace('x="0"', 'x="nan"')
    with pytest.raises(AssertionError, match="coordinate x"):
        _compare(invalid, invalid)
