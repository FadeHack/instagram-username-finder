"""Generator ordering, laziness, addressing and validity filtering."""

from __future__ import annotations

from itertools import islice

import pytest

from instagram_username_finder.generator import UsernameGenerator, is_valid_username


def usernames(generator: UsernameGenerator, length: int, start: int = 0) -> list[str]:
    return [candidate.username for candidate in generator.generate(length, start)]


class TestOrdering:
    def test_yields_lexicographic_order(self) -> None:
        generator = UsernameGenerator("abc")
        assert usernames(generator, 2)[:4] == ["aa", "ab", "ac", "ba"]

    def test_covers_the_whole_space(self) -> None:
        generator = UsernameGenerator("ab")
        assert usernames(generator, 3) == [
            "aaa",
            "aab",
            "aba",
            "abb",
            "baa",
            "bab",
            "bba",
            "bbb",
        ]

    def test_alphabet_is_sorted_regardless_of_input_order(self) -> None:
        assert UsernameGenerator("cba").alphabet == "abc"
        assert usernames(UsernameGenerator("cba"), 1) == ["a", "b", "c"]

    def test_lengths_are_scanned_shortest_first(self) -> None:
        generator = UsernameGenerator("ab")
        assert list(generator.iter_lengths(3, 5)) == [3, 4, 5]


class TestLength:
    def test_every_candidate_has_the_requested_length(self) -> None:
        generator = UsernameGenerator("ab9")
        assert all(len(name) == 4 for name in usernames(generator, 4))

    def test_space_size_is_base_to_the_power_of_length(self) -> None:
        generator = UsernameGenerator("abcde")
        assert generator.space_size(1) == 5
        assert generator.space_size(3) == 125

    def test_rejects_zero_length(self) -> None:
        with pytest.raises(ValueError):
            UsernameGenerator("ab").space_size(0)


class TestCharsets:
    def test_letters_only(self) -> None:
        generator = UsernameGenerator("abcdefghijklmnopqrstuvwxyz")
        assert generator.space_size(3) == 17_576
        assert next(iter(generator.generate(3))).username == "aaa"

    def test_digits_only(self) -> None:
        generator = UsernameGenerator("0123456789")
        assert usernames(generator, 2)[:3] == ["00", "01", "02"]

    def test_custom_characters(self) -> None:
        generator = UsernameGenerator("abc123")
        assert set("".join(usernames(generator, 1))) == set("abc123")

    def test_duplicate_characters_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            UsernameGenerator("aab")

    def test_empty_alphabet_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            UsernameGenerator("")


class TestValidity:
    @pytest.mark.parametrize("username", ["abc", "a.b", "a_b", "a1_b", "x"])
    def test_accepts_structurally_valid_usernames(self, username: str) -> None:
        assert is_valid_username(username)

    @pytest.mark.parametrize("username", [".ab", "ab.", "a..b", ""])
    def test_rejects_structurally_invalid_usernames(self, username: str) -> None:
        assert not is_valid_username(username)

    def test_invalid_candidates_are_skipped(self) -> None:
        generator = UsernameGenerator("a.")
        produced = usernames(generator, 2)
        assert produced == ["aa"]  # ".a", "a." and ".." are all invalid

    def test_skipping_can_be_disabled(self) -> None:
        generator = UsernameGenerator("a.", skip_invalid=False)
        assert usernames(generator, 2) == ["..", ".a", "a.", "aa"]


class TestAddressing:
    def test_username_at_matches_iteration_order(self) -> None:
        generator = UsernameGenerator("abcdefghij")
        produced = usernames(generator, 3)
        for index in (0, 1, 57, 999):
            assert generator.username_at(3, index) == produced[index]

    def test_start_index_resumes_without_gaps_or_repeats(self) -> None:
        generator = UsernameGenerator("abcd")
        whole = usernames(generator, 3)
        first = usernames(generator, 3)[:20]
        rest = usernames(generator, 3, 20)
        assert first + rest == whole

    def test_start_index_past_the_end_yields_nothing(self) -> None:
        generator = UsernameGenerator("ab")
        assert usernames(generator, 2, 4) == []

    def test_out_of_range_index_raises(self) -> None:
        generator = UsernameGenerator("ab")
        with pytest.raises(IndexError):
            generator.username_at(2, 4)

    def test_negative_start_index_raises(self) -> None:
        with pytest.raises(ValueError):
            list(UsernameGenerator("ab").generate(2, -1))


class TestLaziness:
    def test_does_not_materialise_the_search_space(self) -> None:
        # 26**8 is ~209 billion candidates; taking five must be instant.
        generator = UsernameGenerator("abcdefghijklmnopqrstuvwxyz")
        head = list(islice(generator.generate(8), 5))
        assert [candidate.username for candidate in head] == [
            "aaaaaaaa",
            "aaaaaaab",
            "aaaaaaac",
            "aaaaaaad",
            "aaaaaaae",
        ]

    def test_returns_a_generator_not_a_sequence(self) -> None:
        produced = UsernameGenerator("ab").generate(2)
        assert not isinstance(produced, list)
        assert next(produced).index == 0
