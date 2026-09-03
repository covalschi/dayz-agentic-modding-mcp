from dayz_mcp.uigeom import parse_rect


def test_four_numbers_become_a_tuple_of_ints():
    assert parse_rect("100 200 40 20") == (100, 200, 40, 20)


def test_a_float_string_is_tolerated_and_truncated_to_int():
    assert parse_rect("1.0 2 3 4") == (1, 2, 3, 4)


def test_the_wrong_number_of_fields_is_not_a_rectangle():
    assert parse_rect("1 2 3") is None


def test_text_that_is_not_numbers_is_not_a_rectangle():
    assert parse_rect("a b c d") is None
