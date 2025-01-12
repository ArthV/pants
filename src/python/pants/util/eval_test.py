# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).


import pytest

from pants.util.eval import parse_expression


class TestParseLiteral:
    def test_success_simple(self) -> None:
        literal = parse_expression("'42'", acceptable_types=str)
        assert "42" == literal

    def test_success_mixed(self) -> None:
        literal = parse_expression("42", acceptable_types=(float, int))
        assert 42 == literal

    def test_success_complex_syntax(self) -> None:
        assert 3 == parse_expression("1+2", acceptable_types=int)

    def test_success_list_concat(self) -> None:
        # This is actually useful in config files.
        assert [1, 2, 3] == parse_expression("[1, 2] + [3]", acceptable_types=list)

    def test_failure_type(self) -> None:
        # Prove there is no syntax error in the raw value.
        literal = parse_expression("1.0", acceptable_types=float)
        assert 1.0 == literal

        # Now we can safely assume the ValueError is raise due to type checking.
        with pytest.raises(ValueError):
            parse_expression("1.0", acceptable_types=int)

    def test_custom_error_type(self) -> None:
        class CustomError(Exception):
            pass

        with pytest.raises(CustomError):
            parse_expression("1.0", acceptable_types=int, raise_type=CustomError)
